"""Prepare, select and review speech plans without starting speech synthesis."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from . import models as m
from .generation_review import revision_history
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
    if revision is not None and (
        explicit or session.get(m.SpeechPlanReview, revision_id) or (revision.settings_json or {}).get("_prepared_for_review")
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
        if not approved and not (revision.settings_json or {}).get(
            "_prepared_for_review"
        ):
            continue
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
    history = revision_history(services.database, session_id, limit=100)
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
