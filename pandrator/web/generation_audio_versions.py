"""Pinned recast audio and guarded undo in a caller-owned transaction.

Baseline takes are references owned by the recast run, sharing the original
artifact. No media files are copied and historical manifests never follow the
mutable active selection.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    utcnow,
)
from .settings_policy import RevisionConflict, stable_hash

_TERMINAL_RUN_STATUSES = frozenset({"completed", "partial", "failed", "canceled"})
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "canceled", "interrupted"})


def _manifest(run: GenerationRun) -> dict[str, Any] | None:
    value = run.audio_version_json
    return value if value and value.get("version") == 1 and value.get("kind") == "recast" else None


def _available_artifact(session: Session, take: AudioTake | None) -> Artifact | None:
    artifact = session.get(Artifact, take.artifact_id) if take and take.artifact_id else None
    if (take is None or take.status not in {"completed", "stale"}
            or artifact is None or artifact.state != "current" or not artifact.relative_path):
        return None
    return artifact


def initialize_audio_version(
    session: Session,
    run: GenerationRun,
    baseline_take_ids: dict[str, str],
    target_segment_ids: list[str],
    selectors: list[dict[str, Any]],
) -> None:
    """Pin the exact chosen baseline before starting a standalone recast."""
    if run.audio_version_json is not None or run.output_generation_run_id is not None:
        raise ValueError("Audio versions require a new standalone generation run.")
    if len(set(target_segment_ids)) != len(target_segment_ids):
        raise ValueError("Recast target segments must be unique.")
    for segment_id in target_segment_ids:
        segment = session.get(GenerationSegment, segment_id)
        if segment is None or segment.removed or segment.plan_revision_id != run.plan_revision_id:
            raise ValueError("Every recast target must belong to this speech plan.")
    sources: list[tuple[str, AudioTake]] = []
    for segment_id, take_id in baseline_take_ids.items():
        segment = session.get(GenerationSegment, segment_id)
        take = session.get(AudioTake, take_id)
        if (segment is None or segment.removed or segment.plan_revision_id != run.plan_revision_id
                or take is None or take.generation_segment_id != segment_id
                or _available_artifact(session, take) is None):
            raise ValueError("The selected baseline take is no longer available on this speech plan.")
        sources.append((segment_id, take))
    session.flush()
    before: dict[str, str] = {}
    for segment_id, source in sources:
        clone = AudioTake(
            generation_segment_id=segment_id,
            generation_run_id=run.id,
            artifact_id=source.artifact_id,
            parent_take_id=source.id,
            kind=source.kind,
            status=source.status,
            settings_hash=source.settings_hash,
            duration_ms=source.duration_ms,
            is_active=False,
        )
        session.add(clone)
        session.flush()
        before[segment_id] = clone.id
    run.audio_version_json = {
        "version": 1,
        "kind": "recast",
        "selectors": [{"key": item["key"], "label": item["label"]} for item in selectors],
        "target_segment_ids": list(target_segment_ids),
        "before_take_ids": before,
        "after_take_ids": dict(before),
        "generated_take_ids": {},
        "applied_take_ids": {},
        "applied_take_revisions": {},
        "undone": False,
    }


def audio_version_take_ids(run: GenerationRun, side: str = "after") -> dict[str, str] | None:
    if side not in {"before", "after"}:
        raise ValueError("Audio version side must be before or after.")
    manifest = _manifest(run)
    return dict(manifest[f"{side}_take_ids"]) if manifest else None


def audio_version_summary(run: GenerationRun) -> dict[str, Any] | None:
    manifest = _manifest(run)
    if manifest is None:
        return None
    targets = set(manifest["target_segment_ids"])
    generated = set(manifest["generated_take_ids"])
    return {
        "kind": "recast",
        "selectors": deepcopy(manifest["selectors"]),
        "target_count": len(targets),
        "generated_count": len(targets & generated),
        "applied_count": len(manifest["applied_take_ids"]),
        "remaining_count": len(targets - generated),
        "undone": bool(manifest["undone"]),
        "before_available": bool(manifest["before_take_ids"]),
    }


def record_audio_version_take(
    session: Session, run_id: str, take: AudioTake, applied: bool,
) -> None:
    """Record only this recast executor's output; later executors are ignored."""
    run = session.get(GenerationRun, run_id)
    manifest = _manifest(run) if run else None
    job = session.get(Job, run.job_id) if run and run.job_id else None
    frozen = bool(run and (
        run.status in {"completed", "partial", "canceled"}
        or (run.status == "failed" and (job is None or job.status in _TERMINAL_JOB_STATUSES))
        or (job is not None and job.status in _TERMINAL_JOB_STATUSES)
    ))
    if (manifest is None or run is None or run.output_generation_run_id is not None
            or take.generation_run_id != run_id
            or frozen or manifest["undone"]):
        return
    segment_id = take.generation_segment_id
    if segment_id not in manifest["target_segment_ids"]:
        raise ValueError("The produced take is outside the recast targets.")
    if take.status != "completed" or _available_artifact(session, take) is None:
        raise ValueError("Only successfully published audio belongs in a recast version.")
    session.flush()
    value = deepcopy(manifest)
    value["generated_take_ids"][segment_id] = take.id
    value["after_take_ids"][segment_id] = take.id
    if applied:
        value["applied_take_ids"][segment_id] = take.id
        value["applied_take_revisions"][segment_id] = take.revision
    run.audio_version_json = value


def _undo_preview(session: Session, run_id: str) -> tuple[dict[str, Any], list[tuple[str, str | None, int]]]:
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise KeyError(run_id)
    manifest = _manifest(run)
    revision = session.get(GenerationPlanRevision, run.plan_revision_id)
    plan = session.get(GenerationPlan, revision.plan_id) if revision else None
    job = session.get(Job, run.job_id) if run.job_id else None
    blocked: str | None = None
    if manifest is None:
        blocked = "This generation run has no recast audio version."
    elif (run.status not in _TERMINAL_RUN_STATUSES
          or (run.job_id and (job is None or job.status not in _TERMINAL_JOB_STATUSES))):
        blocked = "Wait for recast generation to finish before undoing its audio."
    elif plan is None or plan.active_revision_id != run.plan_revision_id:
        blocked = "The active speech plan changed after this recast."
    snapshot: dict[str, Any] = {
        "run_id": run.id, "run_status": run.status,
        "job_id": run.job_id, "job_status": job.status if job else None,
        "plan_id": plan.id if plan else None,
        "active_revision_id": plan.active_revision_id if plan else None,
        "plan_revision_id": run.plan_revision_id,
        "manifest": manifest, "segments": [],
    }
    updates: list[tuple[str, str | None, int]] = []
    if manifest and not manifest["undone"]:
        for segment_id, applied_id in sorted(manifest["applied_take_ids"].items()):
            segment = session.get(GenerationSegment, segment_id)
            active = list(session.scalars(select(AudioTake).where(
                AudioTake.generation_segment_id == segment_id, AudioTake.is_active.is_(True),
            )))
            baseline_id = manifest["before_take_ids"].get(segment_id)
            baseline = session.get(AudioTake, baseline_id) if baseline_id else None
            artifact = session.get(Artifact, baseline.artifact_id) if baseline and baseline.artifact_id else None
            active_evidence = []
            for take in sorted(active, key=lambda item: item.id):
                active_artifact = session.get(Artifact, take.artifact_id) if take.artifact_id else None
                active_evidence.append({
                    "id": take.id, "revision": take.revision,
                    "artifact_id": take.artifact_id, "status": take.status,
                    "artifact_state": active_artifact.state if active_artifact else None,
                })
            snapshot["segments"].append({
                "id": segment_id,
                "revision": segment.revision if segment else None,
                "plan_revision_id": segment.plan_revision_id if segment else None,
                "removed": segment.removed if segment else None,
                "active": active_evidence,
                "baseline_id": baseline_id,
                "baseline_revision": baseline.revision if baseline else None,
                "baseline_artifact_id": artifact.id if artifact else None,
                "baseline_artifact_state": artifact.state if artifact else None,
                "baseline_artifact_path": artifact.relative_path if artifact else None,
                "baseline_status": baseline.status if baseline else None,
            })
            selected = active[0] if len(active) == 1 else None
            if (segment is None or segment.removed or segment.plan_revision_id != run.plan_revision_id
                    or selected is None or selected.id != applied_id
                    or selected.revision != manifest.get("applied_take_revisions", {}).get(segment_id)):
                blocked = blocked or "Audio selection changed after this recast; undo would replace a manual choice."
            elif baseline_id is None:
                updates.append((segment_id, None, segment.revision))
            elif (baseline is None or baseline.generation_segment_id != segment_id
                  or baseline.generation_run_id != run.id or _available_artifact(session, baseline) is None):
                blocked = blocked or "The baseline audio is no longer available."
            else:
                updates.append((segment_id, baseline.id, segment.revision))
    return {
        "generation_run_id": run.id,
        "speech_plan_revision_id": run.plan_revision_id,
        "selection_hash": stable_hash(snapshot),
        "restore_count": len(updates) if blocked is None else 0,
        "blocked_reason": blocked,
    }, updates


def preview_audio_version_undo(session: Session, run_id: str) -> dict[str, Any]:
    return _undo_preview(session, run_id)[0]


def audio_version_undo_updates(
    session: Session, run_id: str, expected_selection_hash: str,
) -> list[tuple[str, str | None, int]]:
    """Recheck the preview before selections are restored in this transaction."""
    preview, updates = _undo_preview(session, run_id)
    if preview["blocked_reason"]:
        raise RevisionConflict(preview["blocked_reason"])
    if preview["selection_hash"] != expected_selection_hash:
        raise RevisionConflict("The audio selection changed after the undo preview. Refresh and try again.")
    return updates


def mark_audio_version_undone(session: Session, run_id: str) -> None:
    """Call after applying every guarded selection update in the same transaction."""
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise KeyError(run_id)
    manifest = _manifest(run)
    if manifest is None:
        raise ValueError("This generation run has no recast audio version.")
    value = deepcopy(manifest)
    value["undone"] = True
    run.audio_version_json = value
    run.updated_at = utcnow()
