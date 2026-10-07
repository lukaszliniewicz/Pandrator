"""Recast admission and undo inside short caller-owned write transactions."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .generation_audio_versions import (
    audio_version_undo_updates,
    initialize_audio_version,
    mark_audio_version_undone,
)
from .generation_edit_audio import capture_selection_guards
from .generation_scheduling import generation_resource_keys
from .generation_selection import collect_row_state
from .generation_start_preparation import snapshot_guard
from .models import GenerationPlan, GenerationRun, utcnow
from .settings_policy import RevisionConflict, stable_hash

if TYPE_CHECKING:
    from .workspace import GenerationService


def start_recast_in_session(
    generation: GenerationService,
    session: Session,
    session_id: str,
    prepared: dict[str, Any],
    expected_selection_hash: str,
) -> dict[str, Any]:
    """Publish one previewed standalone run without compiling under the writer lock."""
    preview = prepared["preview"]
    from .generation_recast import collect_recast_job_guard
    if preview["selection_hash"] != expected_selection_hash:
        raise RevisionConflict("The cast or audio changed. Review a fresh recast preview.")
    if preview.get("blocked_reason"):
        raise ValueError(preview["blocked_reason"])
    targets = list(prepared["target_segment_ids"])
    if not targets:
        raise ValueError("There are no selected recordings to regenerate.")
    revision_id = preview["speech_plan_revision_id"]
    plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
    fresh_settings, _ = generation.settings.resolve(session_id)
    if (plan is None or plan.active_revision_id != revision_id
            or stable_hash(fresh_settings) != prepared["resolved_settings_hash"]
            or snapshot_guard(session, session_id, revision_id) != prepared["snapshot_guard"]
            or collect_recast_job_guard(session, session_id, revision_id) != prepared["active_job_guard"]
            or collect_row_state(session, revision_id) != prepared["row_state"]):
        raise RevisionConflict("The plan, cast, settings or audio changed. Review a fresh recast preview.")
    snapshot = deepcopy(prepared["frozen_snapshot"])
    snapshot["generation_request_segment_ids"] = targets
    snapshot["generation_selection_guards"] = capture_selection_guards(session, revision_id)
    snapshot["generation_recast_controls_revision"] = preview["controls_revision"]
    sequence = int(session.scalar(select(func.max(GenerationRun.sequence_number)).where(
        GenerationRun.session_id == session_id
    )) or 0) + 1
    run = GenerationRun(
        session_id=session_id,
        plan_revision_id=revision_id,
        sequence_number=sequence,
        operation="regenerate",
        status="queued",
        settings_snapshot_json=snapshot,
        settings_hash=stable_hash(snapshot),
    )
    session.add(run)
    session.flush()
    initialize_audio_version(
        session, run, prepared["baseline_take_ids"], targets, prepared["selector_records"]
    )
    job = generation.jobs.enqueue_in_session(
        session,
        "generation.run",
        {"generation_run_id": run.id, "speech_plan_revision_id": revision_id,
         "segment_ids": targets, "operation": "regenerate"},
        session_id=session_id,
        resource_keys=generation_resource_keys(session_id, snapshot),
    )
    run.job_id = job.id
    run.updated_at = utcnow()
    session.flush()
    result = generation._run_payload(session, run)
    assert result is not None
    return result


def undo_recast_in_session(
    generation: GenerationService,
    session: Session,
    run_id: str,
    expected_selection_hash: str,
) -> dict[str, Any]:
    """Restore affected audio choices atomically, keeping current casting intent."""
    updates = audio_version_undo_updates(session, run_id, expected_selection_hash)
    for segment_id, take_id, revision in updates:
        generation.select_take_in_session(session, segment_id, take_id, revision)
    mark_audio_version_undone(session, run_id)
    session.flush()
    run = session.get(GenerationRun, run_id)
    result = generation._run_payload(session, run)
    assert result is not None
    return {"run": result, "restored_count": len(updates)}
