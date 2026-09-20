"""Prepare, select and review speech plans without starting speech synthesis."""

from __future__ import annotations

import json
from typing import Any

import regex
from sqlalchemy import select

from . import models as m
from .source_management import assert_session_idle
from .workspace import RevisionConflict, adapt_runtime_settings, stable_hash

SIGNATURE_FIELDS = (
    "ordinal",
    "text",
    "optimized_text",
    "node_kind",
    "speaker",
    "language",
    "voice",
    "voice_id",
    "silence_after_ms",
    "removed",
    "source_segment_ids_json",
    "speech_block_provenance_json",
    "speech_plan_json",
    "paragraph_break_after",
)


def plan_signature(session, revision_id: str) -> str:
    columns = [getattr(m.GenerationSegment, key) for key in SIGNATURE_FIELDS]
    rows = session.execute(
        select(*columns)
        .where(m.GenerationSegment.plan_revision_id == revision_id)
        .order_by(m.GenerationSegment.ordinal)
    )
    return stable_hash([dict(zip(SIGNATURE_FIELDS, row, strict=True)) for row in rows])


def freeze_speech_snapshot(
    session, revision_id: str, snapshot: dict[str, Any], *, explicit: bool = False
) -> None:
    revision = session.get(m.GenerationPlanRevision, revision_id)
    if revision is not None and (revision.operation_json or {}).get("draft"):
        raise RevisionConflict("Adopt this resegmentation draft with a topology restore before generation.")
    performance_frozen = bool(
        revision is not None
        and freeze_generation_performance_snapshot(session, revision_id, snapshot)
    )
    from .speech_boundaries import freeze_boundaries
    freeze_boundaries(session, revision_id, snapshot)
    if revision is not None and (
        explicit or performance_frozen or session.get(m.SpeechPlanReview, revision_id)
        or (revision.settings_json or {}).get("_prepared_for_review")
    ):
        snapshot["speech_plan_frozen"] = True
        snapshot["speech_plan_signature"] = plan_signature(session, revision_id)
        snapshot["text"] = {
            **dict(snapshot.get("text") or {}),
            "llm_tts_optimization": False,
            "use_existing_speech_plans": True,
        }


def prepare_segment_edit_targets(
    service, session, segments: dict[str, Any], updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Copy reviewed/historical prepared plans before editorial content changes.

    Selection and synthesis do not copy a plan. Editing a frozen version creates
    exactly one descendant for a whole batch, retaining the old text and takes.
    """
    editorial = set(SIGNATURE_FIELDS) - {"ordinal"}
    editorial.update({"speech_plan", "source_segment_ids", "speech_block_provenance"})
    edited = [
        segments[str(item["id"])]
        for item in updates
        if editorial.intersection(item["changes"])
    ]
    if not edited:
        return segments
    revision_ids = {item.plan_revision_id for item in edited}
    revisions = {
        value: session.get(m.GenerationPlanRevision, value) for value in revision_ids
    }
    frozen = []
    for revision in revisions.values():
        if revision is None:
            continue
        approved = session.get(m.SpeechPlanReview, revision.id) is not None
        plan = session.get(m.GenerationPlan, revision.plan_id)
        if plan.active_revision_id != revision.id:
            raise RevisionConflict(
                "Select this historical speech plan before editing it; its saved content was not changed."
            )
        latest = session.scalar(
            select(m.GenerationPlanRevision.id)
            .where(m.GenerationPlanRevision.plan_id == plan.id)
            .order_by(m.GenerationPlanRevision.revision_number.desc())
            .limit(1)
        )
        used = (
            session.scalar(
                select(m.GenerationRun.id)
                .where(m.GenerationRun.plan_revision_id == revision.id)
                .limit(1)
            )
            is not None
        )
        if approved or used or latest != revision.id:
            frozen.append((plan, revision))
    if not frozen:
        return segments
    if len({segment.plan_revision_id for segment in segments.values()}) != 1:
        raise ValueError("Edit one speech-plan revision at a time.")
    plan, revision = frozen[0]
    from .generation_edit_audio import inherit_edit_copy_audio

    inherit_edit_copy_audio(session, revision.id)
    result = service.revise_topology_in_session(
        session,
        plan.session_id,
        revision.id,
        {
            "action": "restore",
            "target_revision_id": revision.id,
            "reason": "edit_copy",
        },
    )
    return {
        old: session.get(m.GenerationSegment, result["lineage"][old][0])
        for old in segments
    }


def performance_runtime_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Use run-local alternate settings with the same precedence as synthesis."""
    settings = {**dict(snapshot.get("audio") or {}), **dict(snapshot.get("tts") or {})}
    override = snapshot.get("selected_segment_override") or {}
    settings.update(dict(override.get("tts") or {}))
    return settings


def semantic_context_units(session, revision_id: str) -> list[dict[str, Any]]:
    """Read the full accepted plan, even for single-block regeneration."""
    return [
        {
            "id": item.id, "text": item.text, "speaker": item.speaker or "",
            "language": item.language or "", "node_kind": item.node_kind,
            "section_id": str((item.speech_block_provenance_json or {}).get("section_id") or ""),
        }
        for item in session.scalars(
            select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == revision_id, m.GenerationSegment.removed.is_(False))
            .order_by(m.GenerationSegment.ordinal)
        )
    ]


def semantic_context_window(units: list[dict[str, Any]], settings: dict[str, Any], *, target_ids: set[str] | None = None) -> dict[str, dict[str, str]]:
    """Bounded context without audio conditioning or whitespace tokenization."""
    sizes = {}
    for name, default, maximum in (("before", 2, 20), ("after", 1, 20), ("max_chars", 4000, 16000)):
        value = settings.get(f"performance_context_{name}", default)
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"Context {name} must be an integer between 0 and {maximum}.")
        sizes[name] = value
    mode = str(settings.get("tts_context_mode") or "both")
    if mode not in {"off", "before", "both"}:
        raise ValueError("tts_context_mode must be off, before, or both.")
    if mode == "off":
        return {}
    if mode == "before":
        sizes["after"] = 0
    section_kinds = {"heading", "title", "chapter", "chapter_title", "section", "section_title"}

    def boundary(left, right):
        return (
            (left.get("language") and right.get("language") and left["language"] != right["language"])
            or (left.get("section_id") and right.get("section_id") and left["section_id"] != right["section_id"])
            or str(left.get("node_kind") or "").lower() in section_kinds
            or str(right.get("node_kind") or "").lower() in section_kinds
        )

    def context_text(other, current):
        text = str(other.get("text") or "")
        if other.get("speaker") and other.get("speaker") != current.get("speaker"):
            # This is semantic evidence, not a voice reference. A short answer
            # may need another speaker's question to make sense.
            return f"[Other speaker: {str(other['speaker'])[:160]}] {text}"
        return text

    def trim_graphemes(value: str, limit: int, *, from_end: bool) -> str:
        if limit <= 0:
            return ""
        graphemes = [match.group(0) for match in regex.finditer(r"\X", value)]
        if from_end:
            result = []
            size = 0
            for item in reversed(graphemes):
                if size + len(item) > limit:
                    break
                result.append(item)
                size += len(item)
            return "".join(reversed(result))
        result = []
        size = 0
        for item in graphemes:
            if size + len(item) > limit:
                break
            result.append(item)
            size += len(item)
        return "".join(result)

    def marker_and_text(value: str) -> tuple[str, str]:
        prefix = "[Other speaker: "
        if value.startswith(prefix):
            marker_end = value.find("] ", len(prefix))
            if marker_end >= 0:
                marker_end += 2
                return value[:marker_end], value[marker_end:]
        return "", value

    def trim_context(value: str, limit: int, *, from_end: bool) -> str:
        marker, text = marker_and_text(value)
        if not marker:
            return trim_graphemes(value, limit, from_end=from_end)
        if len(marker) > limit:
            # Omitting the unit is preferable to exposing another speaker's
            # words without the marker that makes their meaning safe.
            return ""
        return marker + trim_graphemes(
            text, limit - len(marker), from_end=from_end
        )

    def fit_side(items: list[str], budget: int, *, nearest_is_last: bool) -> str:
        selected: list[str] = []
        remaining = budget
        candidates = list(reversed(items)) if nearest_is_last else list(items)
        for item in candidates:
            separator = 1 if selected else 0
            allowance = remaining - separator
            if allowance <= 0:
                break
            if len(item) <= allowance:
                selected.append(item)
                remaining -= separator + len(item)
                continue
            shortened = trim_context(
                item, allowance, from_end=nearest_is_last
            )
            if shortened:
                selected.append(shortened)
            break
        if nearest_is_last:
            selected.reverse()
        return "\n".join(selected)

    result = {}
    for index, unit in enumerate(units):
        if target_ids is not None and str(unit["id"]) not in target_ids:
            continue
        before, after = [], []
        for offset in range(1, sizes["before"] + 1):
            previous = index - offset
            if previous < 0 or boundary(units[previous], units[previous + 1]):
                break
            before.append(context_text(units[previous], unit))
        for offset in range(1, sizes["after"] + 1):
            following = index + offset
            if following >= len(units) or boundary(units[following - 1], units[following]):
                break
            after.append(context_text(units[following], unit))
        previous_text = "\n".join(reversed(before))
        following_text = "\n".join(after)
        limit = sizes["max_chars"]
        if len(previous_text) + len(following_text) > limit:
            before_budget = min(len(previous_text), limit // 2)
            after_budget = min(len(following_text), limit - before_budget)
            before_budget = min(len(previous_text), limit - after_budget)
            previous_text = fit_side(
                list(reversed(before)), before_budget, nearest_is_last=True
            )
            following_text = fit_side(after, after_budget, nearest_is_last=False)
        result[str(unit["id"])] = {"before": previous_text, "after": following_text}
    return result


def frozen_semantic_contexts(snapshot: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = snapshot.get("semantic_context_snapshot") or {}
    if not raw:
        return {}
    if raw.get("schema_version") != 1:
        raise ValueError("Unsupported semantic context snapshot. Start a new generation run.")
    return semantic_context_window(raw.get("units") or [], dict(raw.get("settings") or {}))


def freeze_generation_performance_snapshot(session, revision_id: str, snapshot: dict[str, Any]) -> bool:
    """Bind a new run to current adoption and immutable semantic source text.

    Resume/retry consumes the existing run snapshot rather than reselecting it.
    The text is stored once, not repeated for every context window in a book.
    """
    settings = performance_runtime_settings(snapshot)
    mode = str(settings.get("tts_context_mode") or "off")
    if mode not in {"off", "before", "both"}:
        raise ValueError("tts_context_mode must be off, before, or both.")
    snapshot.pop("performance_snapshot", None)
    snapshot.pop("semantic_context_snapshot", None)
    snapshot.pop("generation_control_snapshot", None)
    if settings.get("performance_enabled"):
        from .performance_plans import freeze_performance_snapshot

        freeze_performance_snapshot(session, revision_id, snapshot)
    elif settings.get("casting_enabled"):
        from .performance_plans import freeze_performance_snapshot
        from .models import PerformancePlan
        if session.scalar(select(PerformancePlan.id).where(
            PerformancePlan.plan_revision_id == revision_id,
            PerformancePlan.status == "adopted",
        )):
            freeze_performance_snapshot(session, revision_id, snapshot)
    if settings.get("performance_enabled") or settings.get("casting_enabled"):
        from .generation_cast_runtime import freeze_cast_snapshot
        freeze_cast_snapshot(session, revision_id, snapshot, settings)
    if mode != "off":
        context_settings = {
            key: settings[key]
            for key in ("tts_context_mode", "performance_context_before", "performance_context_after", "performance_context_max_chars")
            if key in settings
        }
        semantic_context_window([], context_settings)
        snapshot["semantic_context_snapshot"] = {
            "schema_version": 1, "plan_revision_id": revision_id,
            "settings": context_settings,
            "units": semantic_context_units(session, revision_id),
        }
    return bool(settings.get("performance_enabled") or settings.get("casting_enabled")) or mode != "off"


def segment_performance_settings(
    settings: dict[str, Any], snapshot: dict[str, Any], segment_id: str, text: str,
    *, contexts: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Supply request-only metadata without changing spoken or alignment text."""
    from copy import deepcopy

    result = dict(settings)
    result.pop("_performance", None)
    result.pop("_semantic_context", None)
    if bool(settings.get("performance_enabled")):
        from .performance_plans import performance_for_segment

        annotation = performance_for_segment(snapshot, segment_id, text)
        if annotation is not None:
            result["_performance"] = deepcopy(annotation)
    if str(settings.get("tts_context_mode") or "off") != "off":
        lookup = contexts if contexts is not None else frozen_semantic_contexts(snapshot)
        if segment_id in lookup:
            result["_semantic_context"] = dict(lookup[segment_id])
    return result


def selected_text(services, session_id: str) -> dict[str, Any] | None:
    workflow = services.workflows.snapshot(session_id)
    stage = next(
        (item for item in workflow["stages"] if item["key"] == "generate_audio"), None
    )
    return (
        dict(stage["resolved_input"]) if stage and stage.get("resolved_input") else None
    )


def planning_settings(services, session_id: str) -> dict[str, Any]:
    resolved, _ = services.workspace_settings.resolve(session_id)
    settings = {}
    for section in ("text", "subtitles", "tts", "audio", "rvc", "output"):
        settings.update(
            adapt_runtime_settings(section, dict(resolved.get(section) or {}))
        )
    # Preparation is deterministic. Optional LLM speech rewriting must have
    # produced the selected text revision BEFORE this review boundary.
    settings["llm_tts_optimization"] = False
    settings["_prepared_for_review"] = True
    return settings


def speech_plan_status(services, session_id: str) -> dict[str, Any]:
    source = selected_text(services, session_id)
    from .repair_batches import grouped_revision_history

    history = grouped_revision_history(services.database, session_id, limit=100)
    # An all-rejected batch is an operation, not an additional selectable plan.
    # Explicitly selected internal checkpoints remain in the grouped result.
    seen: set[str] = set()
    selectable = []
    for item in history["items"]:
        batch = item.get("repair_batch")
        if batch and not batch["applied_count"] and item["id"] == batch["base_revision_id"]:
            continue
        if item["id"] not in seen:
            selectable.append(item)
            seen.add(item["id"])
    history["items"] = selectable
    settings = planning_settings(services, session_id)
    with services.database.session() as session:
        record = session.get(m.SessionRecord, session_id)
        if record is None:
            raise KeyError(session_id)
        reviews = {
            row.revision_id: row
            for row in session.scalars(
                select(m.SpeechPlanReview).where(
                    m.SpeechPlanReview.revision_id.in_(
                        [r["id"] for r in history["items"]]
                    )
                )
            )
        }
        active = (
            session.get(m.GenerationPlanRevision, history["active_revision_id"])
            if history["active_revision_id"]
            else None
        )
        signature = plan_signature(session, active.id) if active else None
        source_artifact = (
            session.get(m.Artifact, source["artifact_id"]) if source else None
        )
        selected_matches = bool(
            active
            and source_artifact
            and (
                not active.settings_json.get("_source_artifact_id")
                or (
                    active.settings_json.get("_source_artifact_id")
                    == source_artifact.id
                    and active.settings_json.get(
                        "_source_content_hash", source_artifact.content_hash
                    )
                    == source_artifact.content_hash
                )
            )
        )
        items = []
        for item in history["items"]:
            review = reviews.get(item["id"])
            reviewed = bool(
                review
                and (
                    item["id"] != history["active_revision_id"]
                    or review.content_hash == signature
                )
            )
            items.append(
                {
                    **item,
                    "reviewed": reviewed,
                    "reviewed_at": review.reviewed_at.isoformat() if reviewed else None,
                    "compatible": bool(
                        source
                        and (
                            not item["source_artifact_id"]
                            or item["source_artifact_id"] == source["artifact_id"]
                        )
                    ),
                }
            )
        blocked = None
        try:
            assert_session_idle(session, session_id)
        except RevisionConflict as error:
            blocked = str(error)
        generation_blocked = None
        try:
            # Synthesis consumes a frozen plan. An external edit of upstream
            # text does not mutate that plan and must not disable a new run.
            assert_session_idle(session, session_id, include_editing_dispatches=False)
        except RevisionConflict as error:
            generation_blocked = str(error)
        longest = max(
            (
                len(row.optimized_text or row.text)
                for row in session.scalars(
                    select(m.GenerationSegment).where(
                        m.GenerationSegment.plan_revision_id
                        == (active.id if active else ""),
                        m.GenerationSegment.removed.is_(False),
                    )
                )
            ),
            default=0,
        )
        limit = (
            int(settings.get("speech_block_max_chars") or 220)
            if record.workflow_kind == "voiceover"
            else 0
        )
        warning = None
        if active and not selected_matches:
            warning = "This plan was prepared from a different text version. Select matching text or prepare a new plan."
        elif limit and longest > limit:
            warning = f"A speech block has {longest} characters, exceeding the configured {limit}-character limit. Review or rebuild the plan."
        return {
            **history,
            "items": items,
            "session_id": session_id,
            "session_revision": record.revision,
            "current_input": source,
            "selected_revision_id": history["active_revision_id"],
            "latest_revision_id": items[0]["id"] if items else None,
            "content_signature": signature,
            "can_prepare": bool(
                source_artifact and source_artifact.kind in {"srt", "json"}
            ),
            "can_generate": bool(selected_matches and not warning and not generation_blocked),
            "generation_blocked_reason": generation_blocked,
            "blocked_reason": blocked,
            "warning": warning,
        }


def preparation_guard(session, session_id: str) -> str:
    choices = [
        tuple(row)
        for row in session.execute(
            select(
                m.SessionStageSelection.stage_key,
                m.SessionStageSelection.artifact_id,
                m.SessionStageSelection.revision,
            ).where(m.SessionStageSelection.session_id == session_id)
        )
    ]
    sources = [
        tuple(row)
        for row in session.execute(
            select(
                m.SessionSource.source_asset_id,
                m.SessionSource.role,
                m.SessionSource.is_current,
                m.SessionSource.revision,
            ).where(m.SessionSource.session_id == session_id)
        )
    ]
    settings = [
        tuple(row)
        for row in session.execute(
            select(m.SessionSetting.section, m.SessionSetting.revision).where(
                m.SessionSetting.session_id == session_id
            )
        )
    ]
    artifacts = [
        tuple(row)
        for row in session.execute(
            select(
                m.Artifact.id,
                m.Artifact.state,
                m.Artifact.content_hash,
                m.Artifact.updated_at,
            ).where(m.Artifact.session_id == session_id)
        )
    ]
    return stable_hash(
        [
            sorted(choices, key=str),
            sorted(sources, key=str),
            sorted(settings, key=str),
            sorted(artifacts, key=str),
        ]
    )


def prepare_speech_plan_data(
    services, session_id: str, source_artifact_id: str
) -> dict[str, Any]:
    """Resolve settings and compute blocks before taking the database write lock."""
    settings = planning_settings(services, session_id)
    with services.database.session() as session:
        guard = preparation_guard(session, session_id)
    current = selected_text(services, session_id)
    if not current or current["artifact_id"] != source_artifact_id:
        raise RevisionConflict(
            "The selected text changed. Review its version before preparing a plan."
        )
    source, path = services.workflow_handlers._resolve_input(source_artifact_id)
    if source.session_id != session_id:
        raise ValueError("The planning input must belong to this session.")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("This planning input exceeds the 64 MiB safety limit.")
    settings["_source_content_hash"] = source.content_hash
    language = services.workflow_handlers._generation_language(
        session_id, source, settings
    )
    settings.update(language=language, target_language=language)
    revision_id = (source.metadata_json or {}).get("revision_id")
    if path.suffix.lower() == ".srt":
        records, revision_id, _ = (
            services.workflow_handlers._subtitle_generation_records(
                source, path, settings, language
            )
        )
    elif path.suffix.lower() == ".json":
        records = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(records, list) or any(
            not isinstance(row, dict) for row in records
        ):
            raise ValueError(
                "Prepared speech text must contain an array of speech units."
            )
    else:
        raise ValueError(
            "Prepare the document text first, or register the SRT/VTT as timed subtitles."
        )
    if not records or len(records) > 100_000:
        raise ValueError(
            "A speech plan must contain between 1 and 100,000 speech units."
        )
    return {
        "guard": guard,
        "records": records,
        "settings": settings,
        "source_artifact_id": source.id,
        "source_revision_id": revision_id,
    }


def prepare_speech_plan(
    services,
    session,
    session_id: str,
    *,
    expected_revision: int,
    expected_plan_revision_id: str | None,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    record = session.get(m.SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    if (
        record.revision != expected_revision
        or preparation_guard(session, session_id) != prepared["guard"]
    ):
        raise RevisionConflict(
            "The selected text or settings changed during preparation. Refresh and try again."
        )
    assert_session_idle(session, session_id)
    plan = session.scalar(
        select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id)
    )
    if (plan.active_revision_id if plan else None) != expected_plan_revision_id:
        raise RevisionConflict(
            "The selected speech plan changed. Refresh before rebuilding it."
        )
    new_id, segment_ids = services.workflow_handlers._store_generation_plan(
        session_id,
        prepared["records"],
        settings=prepared["settings"],
        source_revision_id=prepared["source_revision_id"],
        source_artifact_id=prepared["source_artifact_id"],
        db_session=session,
        force_new=True,
    )
    return {
        "session_id": session_id,
        "selected_revision_id": new_id,
        "segment_count": len(segment_ids),
        "content_signature": plan_signature(session, new_id),
        "synthesis_started": False,
    }


def select_speech_plan(
    session, session_id: str, *, revision_id: str, expected_plan_revision_id: str | None
) -> dict[str, Any]:
    assert_session_idle(session, session_id)
    plan = session.scalar(
        select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id)
    )
    revision = session.get(m.GenerationPlanRevision, revision_id)
    if plan is None or revision is None or revision.plan_id != plan.id:
        raise KeyError(revision_id)
    if plan.active_revision_id != expected_plan_revision_id:
        raise RevisionConflict(
            "The selected plan changed. Refresh before selecting another revision."
        )
    if (revision.operation_json or {}).get("draft"):
        raise RevisionConflict("Adopt this resegmentation draft with a topology restore so its source revision is checked.")
    plan.active_revision_id = revision.id
    plan.updated_at = m.utcnow()
    session.flush()
    return {
        "session_id": session_id,
        "selected_revision_id": revision.id,
        "revision_number": revision.revision_number,
        "content_signature": plan_signature(session, revision.id),
        "created_revision": False,
    }


def review_speech_plan(
    session, session_id: str, *, revision_id: str, content_signature: str
) -> dict[str, Any]:
    plan = session.scalar(
        select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id)
    )
    if plan is None or plan.active_revision_id != revision_id:
        raise RevisionConflict(
            "Review applies only to the currently selected speech plan."
        )
    actual = plan_signature(session, revision_id)
    if actual != content_signature:
        raise RevisionConflict(
            "The speech plan changed while it was being reviewed. Inspect the new content first."
        )
    review = session.get(m.SpeechPlanReview, revision_id)
    if review is None:
        review = m.SpeechPlanReview(revision_id=revision_id, content_hash=actual)
        session.add(review)
    review.content_hash = actual
    review.reviewed_at = m.utcnow()
    session.flush()
    return {
        "session_id": session_id,
        "selected_revision_id": revision_id,
        "reviewed": True,
        "content_signature": actual,
        "reviewed_at": review.reviewed_at.isoformat(),
    }
