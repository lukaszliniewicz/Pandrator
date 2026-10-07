"""Database-only validation of immutable recast assembly selections."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .generation_audio_versions import audio_version_take_ids
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
)
from .settings_policy import RevisionConflict, stable_hash
from .speech_plan_context import SIGNATURE_FIELDS


def audio_version_assembly_selection(
    session: Session, session_id: str, run: GenerationRun | None, side: str,
) -> tuple[dict[str, Any] | None, list[tuple[GenerationSegment, AudioTake, Artifact]]]:
    """Read a bounded exact selection; never compile speech or inspect files."""
    if side not in {"before", "after"}:
        raise ValueError("Audio version side must be before or after.")
    take_ids = audio_version_take_ids(run, side) if run else None
    if take_ids is None or run is None:
        if side == "before":
            raise ValueError("Before audio is available only for a recast generation run.")
        return None, []
    if run.session_id != session_id or run.output_generation_run_id is not None:
        raise ValueError("The recast audio version does not belong to this session.")
    if side == "after":
        job = session.get(Job, run.job_id) if run.job_id else None
        if (run.status not in {"completed", "partial", "failed", "canceled"}
                or (job is None or job.status not in {
                    "succeeded", "failed", "canceled", "interrupted",
                })):
            raise ValueError("Wait for recast generation and its job to finish before assembling after audio.")
    revision = session.get(GenerationPlanRevision, run.plan_revision_id)
    plan = session.get(GenerationPlan, revision.plan_id) if revision else None
    if plan is None or plan.session_id != session_id:
        raise ValueError("The recast speech plan is no longer available.")
    segments = list(session.scalars(select(GenerationSegment).where(
        GenerationSegment.plan_revision_id == run.plan_revision_id,
        GenerationSegment.removed.is_(False),
    ).order_by(GenerationSegment.ordinal, GenerationSegment.id)))
    if not segments or set(take_ids) != {item.id for item in segments}:
        raise ValueError("The recast audio version has no complete baseline for this speech plan.")
    ids = list(take_ids.values())
    takes: dict[str, tuple[AudioTake, Artifact | None]] = {}
    for offset in range(0, len(ids), 500):
        for take, artifact in session.execute(select(AudioTake, Artifact).outerjoin(
            Artifact, Artifact.id == AudioTake.artifact_id,
        ).where(AudioTake.id.in_(ids[offset:offset + 500]))):
            takes[take.id] = take, artifact
    rows: list[tuple[GenerationSegment, AudioTake, Artifact]] = []
    evidence = []
    for segment in segments:
        take, artifact = takes.get(take_ids[segment.id], (None, None))
        if (take is None or take.generation_segment_id != segment.id
                or take.generation_run_id != run.id or take.status not in {"completed", "stale"}
                or artifact is None or artifact.state != "current" or not artifact.relative_path
                or artifact.session_id != session_id):
            raise ValueError(f"Segment {segment.ordinal + 1} has no available pinned recast audio take.")
        rows.append((segment, take, artifact))
        evidence.append({
            "segment_id": segment.id,
            "speech": {key: getattr(segment, key) for key in SIGNATURE_FIELDS},
            "take_id": take.id, "artifact_id": artifact.id,
            "take_status": take.status, "take_kind": take.kind,
            "duration_ms": take.duration_ms, "take_settings_hash": take.settings_hash,
            "artifact_path": artifact.relative_path, "artifact_hash": artifact.content_hash,
            "artifact_state": artifact.state,
        })
    selection = {
        "generation_run_id": run.id, "plan_revision_id": run.plan_revision_id,
        "audio_version_side": side, "audio_version_take_ids": take_ids,
        "audio_version_selection_hash": stable_hash({
            "run_id": run.id, "plan_revision_id": run.plan_revision_id,
            "side": side, "takes": evidence,
        }),
    }
    return selection, rows


def revalidate_audio_version_assembly(
    session: Session, session_id: str, run: GenerationRun | None,
    frozen: dict[str, Any],
) -> list[tuple[GenerationSegment, AudioTake, Artifact]]:
    """Require the queued/previewed pin to match, regardless of active takes."""
    try:
        selection, rows = audio_version_assembly_selection(
            session, session_id, run, frozen.get("audio_version_side", "after"),
        )
    except (ValueError, KeyError) as error:
        raise RevisionConflict(str(error)) from error
    keys = ("generation_run_id", "plan_revision_id", "audio_version_side",
            "audio_version_take_ids", "audio_version_selection_hash")
    if selection is None or any(selection[key] != frozen.get(key) for key in keys):
        raise RevisionConflict("The pinned recast audio version changed. Refresh and try again.")
    return rows
