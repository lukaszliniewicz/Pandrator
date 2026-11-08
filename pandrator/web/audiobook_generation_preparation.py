"""Explicit, guarded refresh of untouched automatic audiobook narration plans."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select

from pandrator.logic.audiobook_chunking import audiobook_chunk_budget

from . import models as m
from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .settings_policy import RevisionConflict, stable_hash
from .sound_project_models import ProjectSoundCue, ProjectSoundPlan
from .source_management import assert_session_idle
from .speech_plan_context import plan_signature
from .speech_plan_workspace import preparation_guard, selected_text

GUARD_KEY = "_automatic_narration_refresh"
_BUDGET_FIELDS = ("mode", "target_chars", "target_words", "target_tokenizer_bytes")
_SECTIONS = ["text", "audio", "tts"]


def _budget_identity(budget: dict) -> tuple:
    return tuple(budget.get(key) for key in _BUDGET_FIELDS)


def _active_revision(session, session_id: str, expected: str):
    plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id))
    revision = session.get(m.GenerationPlanRevision, expected)
    if (
        plan is None
        or plan.active_revision_id != expected
        or revision is None
        or revision.plan_id != plan.id
    ):
        raise RevisionConflict(
            "The selected speech plan changed. Refresh before preparing narration."
        )
    return revision


def _preservation_reason(session, revision, source) -> str | None:
    if source is None or source.role != "prepared_text" or source.state == "deleted":
        return "This plan was prepared separately. Its existing block boundaries have been kept."
    segmentation = (source.metadata_json or {}).get("segmentation") or {}
    if segmentation.get("policy_applied") is not True or revision.operation_json:
        return (
            "This plan contains annotations or boundary edits. Its existing blocks have been kept."
        )
    rows = list(
        session.scalars(
            select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == revision.id)
        )
    )
    if not rows or any(
        row.revision > 1
        or row.removed
        or row.optimized_text
        or row.speaker
        or row.voice
        or row.voice_id
        or row.language
        or row.marked
        or row.optimization_reviewed
        or row.optimization_status != "not_requested"
        or set(row.speech_plan_json or {}) - {"pause_kind"}
        for row in rows
    ):
        return (
            "This plan contains text, voice, or delivery edits. Its existing blocks have been kept."
        )
    if session.scalar(
        select(m.ProjectSpeechReference.session_id)
        .where(
            m.ProjectSpeechReference.session_id == source.session_id,
            m.ProjectSpeechReference.slot_key.in_([f"segment:{row.id}" for row in rows]),
        )
        .limit(1)
    ):
        return "This plan has individual voice selections. Its existing blocks have been kept."
    if session.scalar(
        select(m.PerformancePlan.id)
        .where(m.PerformancePlan.plan_revision_id == revision.id)
        .limit(1)
    ):
        return "This plan has speaker or delivery annotations. Its existing blocks have been kept."
    if session.scalar(
        select(ProjectSoundCue.id)
        .join(ProjectSoundPlan)
        .where(
            ProjectSoundPlan.speech_plan_revision_id == revision.id,
        )
        .limit(1)
    ):
        return "This plan has sound placements. Its existing blocks have been kept."
    return None


def _settings_fingerprint(settings: dict) -> str:
    return stable_hash({key: settings.get(key, {}) for key in _SECTIONS})


def _capture_guard(services, session, record, revision, source) -> dict:
    resolved, _ = services.workspace_settings.resolve_in_session(session, record.id, _SECTIONS)
    return {
        "revision_id": revision.id,
        "plan_signature": plan_signature(session, revision.id),
        "preparation_guard": preparation_guard(session, record.id),
        "settings_hash": _settings_fingerprint(resolved),
        "session_revision": record.revision,
        "source_language": record.source_language,
        "prepared_artifact_id": source.id,
    }


def validate_automatic_narration_refresh(
    session, session_id: str, payload: dict, settings_service
) -> None:
    """Recheck the queued prerequisite in the worker's caller-owned snapshot."""
    guard = payload.get(GUARD_KEY)
    if guard is None:
        return
    if not isinstance(guard, dict):
        raise RevisionConflict("The narration refresh guard is invalid.")
    require_current_session_access(session, session_id, ProjectAction.EDIT)
    require_current_session_access(session, session_id, ProjectAction.RUN)
    record = session.get(m.SessionRecord, session_id)
    revision = _active_revision(session, session_id, str(guard.get("revision_id") or ""))
    source = session.get(m.Artifact, guard.get("prepared_artifact_id"))
    resolved, _ = settings_service.resolve_in_session(session, session_id, _SECTIONS)
    if (
        record is None
        or record.workflow_kind != "audiobook"
        or record.trashed_at is not None
        or record.status == "purging"
        or record.revision != guard.get("session_revision")
        or record.source_language != guard.get("source_language")
        or plan_signature(session, revision.id) != guard.get("plan_signature")
        or preparation_guard(session, session_id) != guard.get("preparation_guard")
        or _settings_fingerprint(resolved) != guard.get("settings_hash")
        or (revision.settings_json or {}).get("_source_artifact_id")
        != guard.get("prepared_artifact_id")
        or _preservation_reason(session, revision, source)
    ):
        raise RevisionConflict(
            "The selected speech plan, text, or settings changed during narration preparation. Refresh and try again."
        )
    clean_id = str(payload.get("source_artifact_id") or "")
    clean = session.get(m.Artifact, clean_id)
    if (
        clean is None
        or clean.state == "deleted"
        or clean.content_hash != guard.get("source_content_hash")
        or not session.get(m.ArtifactEdge, (clean_id, source.id))
    ):
        raise RevisionConflict("The source text changed during narration preparation.")


def prepare_audiobook_generation(
    services: Any,
    session_id: str,
    *,
    speech_plan_revision_id: str,
    settings_source_run_id: str | None = None,
) -> dict[str, Any]:
    """Queue deterministic preparation only for an untouched selected automatic plan."""
    response: dict[str, Any] = {
        "status": "current",
        "speech_plan_revision_id": speech_plan_revision_id,
        "message": "The selected narration segmentation is current.",
    }
    with services.database.snapshot_session() as session:
        require_current_session_access(session, session_id, ProjectAction.RUN)
        record = session.get(m.SessionRecord, session_id)
        if record is None or record.trashed_at is not None:
            raise KeyError(session_id)
        if settings_source_run_id:
            run = session.get(m.GenerationRun, settings_source_run_id)
            if run is None or run.session_id != session_id:
                raise ValueError("The selected settings source is unavailable for this session.")
            return {
                **response,
                "message": "Historical settings retain the selected narration segmentation.",
            }
        if record.workflow_kind != "audiobook":
            return response
        revision = _active_revision(session, session_id, speech_plan_revision_id)
        resolved, _ = services.workspace_settings.resolve_in_session(session, session_id, _SECTIONS)
        budget = audiobook_chunk_budget(
            resolved["text"], resolved["tts"], str(record.source_language or "en")
        )
        source_id = (revision.settings_json or {}).get("_source_artifact_id")
        source = session.get(m.Artifact, source_id) if source_id else None
        recorded = (revision.settings_json or {}).get("_audiobook_chunk_budget")
        if not isinstance(recorded, dict) and source is not None:
            recorded = ((source.metadata_json or {}).get("segmentation") or {}).get("budget")
        if (
            not isinstance(recorded, dict)
            or recorded.get("mode") not in {"model", "manual"}
            or type(recorded.get("target_chars")) is not int
        ):
            recorded = None
        response["target_chars"] = budget["target_chars"]
        if isinstance(recorded, dict):
            response["previous_target_chars"] = recorded.get("target_chars")
            if _budget_identity(recorded) == _budget_identity(budget):
                return response
        reason = _preservation_reason(session, revision, source)
        if reason or source is None or not isinstance(recorded, dict):
            return {
                **response,
                "status": "preserved",
                "message": reason
                or "The original block-length setting is unavailable. This plan’s existing blocks have been kept.",
            }
        guard = _capture_guard(services, session, record, revision, source)
        parent_ids = set(
            session.scalars(
                select(m.ArtifactEdge.parent_artifact_id).where(
                    m.ArtifactEdge.child_artifact_id == source.id
                )
            )
        )

    selected = selected_text(services, session_id)
    if not selected or selected.get("artifact_id") != source_id:
        return {
            **response,
            "status": "preserved",
            "message": "The selected generation text differs from this plan's prepared text; the selected plan was preserved.",
        }
    stage = services.workflows.resolve_stage(session_id, "prepare_text")
    if stage.source_artifact_id not in parent_ids:
        return {
            **response,
            "status": "preserved",
            "message": "The selected clean text differs from this plan's source; the selected plan was preserved.",
        }
    if (
        _settings_fingerprint(stage.payload.get("resolved_settings_snapshot") or {})
        != guard["settings_hash"]
    ):
        raise RevisionConflict("Narration settings changed while preparation was being resolved.")
    payload = deepcopy(stage.payload)
    guard["source_content_hash"] = stage.source_content_hash
    payload[GUARD_KEY] = guard
    with services.database.immediate_session() as session:
        validate_automatic_narration_refresh(
            session, session_id, payload, services.workspace_settings
        )
        assert_session_idle(session, session_id)
        job = services.jobs.enqueue_in_session(
            session,
            stage.job_kind,
            payload,
            session_id=session_id,
            resource_keys=list(stage.resource_keys),
        )
        job_id = job.id
    return {
        **response,
        "status": "queued",
        "job_id": job_id,
        "message": "Updating narration blocks for the selected model…",
    }
