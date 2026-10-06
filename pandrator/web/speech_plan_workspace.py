"""Prepare, select and review speech plans without starting speech synthesis."""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any, cast

from sqlalchemy import select

from . import models as m
from .generation_performance_snapshot import (
    freeze_generation_performance_snapshot as freeze_generation_performance_snapshot,
)
from .generation_performance_snapshot import (
    frozen_semantic_contexts as frozen_semantic_contexts,
)
from .generation_performance_snapshot import (
    performance_runtime_settings as performance_runtime_settings,
)
from .generation_performance_snapshot import (
    segment_performance_settings as segment_performance_settings,
)
from .settings_policy import RevisionConflict, adapt_runtime_settings, stable_hash
from .source_management import ACTIVE_JOB, DISPATCH_MODELS, TERMINAL, assert_session_idle
from .speech_plan_context import (
    SIGNATURE_FIELDS as SIGNATURE_FIELDS,
)
from .speech_plan_context import (
    plan_signature as plan_signature,
)
from .speech_plan_context import (
    semantic_context_units as semantic_context_units,
)
from .speech_plan_context import (
    semantic_context_window as semantic_context_window,
)


def freeze_speech_snapshot(
    session,
    revision_id: str,
    snapshot: dict[str, Any],
    *,
    explicit: bool = False,
    segment_ids: Collection[str] | None = None,
) -> None:
    """Freeze plan metadata and language bindings for all or selected segments.

    ``segment_ids=None`` validates every active segment; an explicit collection
    restricts only the language-binding records to those requested for synthesis.
    """
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
    if revision is not None:
        effective_tts = performance_runtime_settings(snapshot)
        selected_override: Any = snapshot.get("selected_segment_override") or {}
        selected_tts_override_raw = (
            selected_override.get("tts")
            if isinstance(selected_override, dict)
            else None
        )
        selected_tts_override: dict[str, Any] = (
            cast(dict[str, Any], selected_tts_override_raw)
            if isinstance(selected_tts_override_raw, dict)
            else {}
        )
        alternate_language = str(
            selected_tts_override.get("language")
            or selected_tts_override.get("target_language")
            or ""
        ).strip()
        from pandrator.logic.tts_language_preflight import validate_tts_language

        records: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        bindings: dict[str, dict[str, Any]] = {}
        service_config_cache: dict = {}
        segment_query = select(m.GenerationSegment).where(
            m.GenerationSegment.plan_revision_id == revision_id,
            m.GenerationSegment.removed.is_(False),
        )
        if segment_ids is not None:
            segment_query = segment_query.where(
                m.GenerationSegment.id.in_(tuple(segment_ids))
            )
        segments = session.scalars(segment_query.order_by(m.GenerationSegment.ordinal))
        for segment in segments:
            segment_text = segment.optimized_text or segment.text
            segment_settings = segment_performance_settings(
                effective_tts, snapshot, segment.id, segment_text
            )
            segment_language = str(segment.language or "").strip()
            if (
                not alternate_language
                and segment_language.lower() not in {"", "auto", "und", "unknown"}
            ):
                segment_settings.update(
                    language=segment_language,
                    target_language=segment_language,
                )
            validation = validate_tts_language(
                segment_settings,
                _service_config_cache=service_config_cache,
            )
            support = validation["language_support"]
            if isinstance(support, dict):
                provider_id = str(support.get("provider_id") or "")
                model_id = str(support.get("model_id") or "")
                language = str(validation["language"])
                operation = str(support.get("operation") or "tts")
                identity = (provider_id, model_id, language, operation)
                if identity not in records:
                    records[identity] = {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "language": language,
                        "operation": operation,
                        "decision": validation["decision"],
                        "native_language": validation["native_language"],
                        "language_support": support,
                    }
                record_key = stable_hash(list(identity))
                bindings[str(segment.id)] = {
                    "record_key": record_key,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "language": language,
                    "operation": operation,
                    "decision": validation["decision"],
                    "unresolved_model": False,
                }
            else:
                bindings[str(segment.id)] = {
                    "language": str(validation["language"]),
                    "operation": "tts",
                    "decision": "unverified",
                    "unresolved_model": True,
                }
        snapshot["tts_language_snapshot"] = {
            "schema_version": 1,
            "records": list(records.values()),
            "bindings": bindings,
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


def selected_text(services, session_id: str) -> dict[str, Any] | None:
    workflow = services.workflows.snapshot(session_id)
    stage = next(
        (item for item in workflow["stages"] if item["key"] == "generate_audio"), None
    )
    return (
        dict(stage["resolved_input"]) if stage and stage.get("resolved_input") else None
    )


def planning_settings(services, session_id: str, *, final_optimization: bool = False) -> dict[str, Any]:
    resolved, _ = services.workspace_settings.resolve(session_id)
    settings = {}
    for section in ("text", "subtitles", "tts", "audio", "rvc", "output"):
        settings.update(
            adapt_runtime_settings(section, dict(resolved.get(section) or {}))
        )
    # Preparation is deterministic. Optional LLM speech rewriting must have
    # produced the selected text revision BEFORE this review boundary.
    settings["llm_tts_optimization"] = bool(
        final_optimization and settings.get("llm_tts_optimization")
    )
    settings["_prepared_for_review"] = True
    return settings


def speech_plan_status(services, session_id: str, *, summary: bool = False) -> dict[str, Any]:
    source = selected_text(services, session_id)
    from .repair_batches import grouped_revision_history

    history = grouped_revision_history(
        services.database, session_id, limit=100,
        include_audio_reuse=not summary, include_undo_eligibility=not summary,
    )
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
    services, session_id: str, source_artifact_id: str, *,
    frozen_settings: dict[str, Any] | None = None,
    expected_guard: str | None = None,
) -> dict[str, Any]:
    """Resolve settings and compute blocks before taking the database write lock."""
    from .artifacts import sha256_file

    settings = dict(frozen_settings) if frozen_settings is not None else planning_settings(services, session_id)
    with services.database.session() as session:
        guard = preparation_guard(session, session_id)
    if expected_guard is not None and guard != expected_guard:
        raise RevisionConflict("The selected text or settings changed before preparation. Refresh and try again.")
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
    if expected_guard is not None:
        if sha256_file(path) != source.content_hash:
            raise RevisionConflict("The selected text file changed after it was registered. Import it again.")
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
    if expected_guard is not None:
        if sha256_file(path) != source.content_hash:
            raise RevisionConflict("The selected text file changed during preparation. Import it again.")
        with services.database.session() as session:
            if preparation_guard(session, session_id) != expected_guard:
                raise RevisionConflict("The selected text or settings changed during preparation. Refresh and try again.")
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
    owning_job_id: str | None = None,
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
    if owning_job_id is None:
        assert_session_idle(session, session_id)
    else:
        _assert_only_preparation_job_active(session, session_id, owning_job_id)
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


def _assert_only_preparation_job_active(session, session_id: str, owning_job_id: str) -> None:
    """Allow the committing worker itself while preserving all other idle fences."""
    owner = session.get(m.Job, owning_job_id)
    if owner is None or owner.session_id != session_id or owner.kind != "speech.prepare" or owner.status != "running":
        raise RevisionConflict("The owning speech preparation job is no longer running.")
    if session.scalar(select(m.Job.id).where(
        m.Job.session_id == session_id, m.Job.id != owning_job_id,
        m.Job.status.in_(ACTIVE_JOB),
    ).limit(1)):
        raise RevisionConflict("Stop or cancel active session work before changing its sources.")
    if session.scalar(select(m.GenerationRun.id).where(
        m.GenerationRun.session_id == session_id,
        m.GenerationRun.status.in_({"queued", "running", "pausing", "cancel_requested"}),
    ).limit(1)):
        raise RevisionConflict("Stop or cancel audio generation before changing its sources.")
    for model in DISPATCH_MODELS:
        if session.scalar(select(model.id).where(
            model.session_id == session_id, model.status.not_in(TERMINAL),
        ).limit(1)):
            raise RevisionConflict("Finish or cancel the session's open editing dispatch before changing its sources.")


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
