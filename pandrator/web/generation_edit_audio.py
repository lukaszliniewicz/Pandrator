"""Carry audio through explicit editorial copies without replacing user edits.

Generation runs remain bound to their original immutable plan. A late result
may also become available in its active edit-copy descendant, but only an
unchanged speech input can make it a current take there.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import Artifact, AudioTake, GenerationPlan, GenerationPlanRevision, GenerationRun, GenerationSegment, utcnow


def edit_copy_ancestors(session: Any, revision: GenerationPlanRevision) -> list[str]:
    """Stop at any actual split, merge, restore, or unrelated plan boundary."""
    result: list[str] = []
    seen = {revision.id}
    for _ in range(256):
        operation = revision.operation_json or {}
        parent_id = revision.parent_revision_id
        if (operation.get("reason") != "edit_copy"
                or operation.get("action") != "restore"
                or not parent_id or parent_id in seen
                or operation.get("target_revision_id") != parent_id):
            break
        parent = session.get(GenerationPlanRevision, parent_id)
        if parent is None or parent.plan_id != revision.plan_id:
            break
        result.append(parent.id)
        seen.add(parent.id)
        revision = parent
    return result


def interrupted_run_id(run: GenerationRun) -> str | None:
    """Scheduling ownership is distinct from the output's immutable revision."""
    return (run.settings_snapshot_json or {}).get(
        "interrupted_generation_run_id"
    ) or run.source_generation_run_id


def running_edit_ancestor(session: Any, revision_id: str) -> GenerationRun | None:
    """Find the active full run behind an editorial copy, never an unrelated run."""
    revision = session.get(GenerationPlanRevision, revision_id)
    if revision is None:
        return None
    ancestors = edit_copy_ancestors(session, revision)
    if not ancestors:
        return None
    plan = session.get(GenerationPlan, revision.plan_id)
    if plan is None or plan.active_revision_id != revision_id:
        return None
    candidates = list(session.scalars(select(GenerationRun).where(
        GenerationRun.session_id == plan.session_id,
        GenerationRun.plan_revision_id.in_(ancestors),
        GenerationRun.operation == "generate",
        GenerationRun.output_generation_run_id.is_(None),
        GenerationRun.status.in_(("running", "queued", "pausing", "paused")),
    ).order_by(GenerationRun.sequence_number.desc())))
    from .workspace import GenerationService

    for candidate in candidates:
        if candidate.cancel_requested:
            continue
        if candidate.status in {"paused", "pausing"} and not (
            GenerationService._regeneration_baton_descendants(session, candidate.id)
        ):
            continue  # An explicit user pause must remain an explicit pause.
        return candidate
    return None


def resume_segment_ids(session: Any, run: GenerationRun) -> list[str]:
    """Resume the original request, not every row of a targeted output plan."""
    from .models import Job

    snapshot = run.settings_snapshot_json or {}
    if "generation_request_segment_ids" in snapshot:
        return list(snapshot["generation_request_segment_ids"] or [])
    job = session.get(Job, run.job_id) if run.job_id else None
    return list((job.payload_json or {}).get("segment_ids") or []) if job else []


def release_interrupted_run(session: Any, jobs: Any, child: GenerationRun) -> str | None:
    """Release a temporary pause, even if a queued replacement is canceled.

    This is called inside a short write transaction. User pauses revoke the
    permission flag, so neither completion nor cancellation can undo them.
    """
    if not child.resume_source_on_completion:
        return None
    source_id = interrupted_run_id(child)
    source = session.get(GenerationRun, source_id) if source_id else None
    if source is None or source.session_id != child.session_id:
        return None
    from .workspace import GenerationService
    from .models import Job

    GenerationService._clear_regeneration_baton(session, child, source.id)
    if source.cancel_requested or not source.pause_requested:
        return None
    # If the last queued replacement is canceled, an earlier replacement may
    # still be waiting. Transfer the resume responsibility instead of reviving
    # the full run ahead of it.
    sibling = session.scalar(select(GenerationRun).where(
        GenerationRun.id != child.id,
        GenerationRun.session_id == source.session_id,
        GenerationRun.operation == "regenerate",
        GenerationRun.cancel_requested.is_(False),
        GenerationRun.status.in_(("queued", "running")),
        (GenerationRun.source_generation_run_id == source.id)
        | (GenerationRun.settings_snapshot_json["interrupted_generation_run_id"].as_string() == source.id),
    ).order_by(GenerationRun.sequence_number.desc()))
    if sibling is not None and child.status in {"canceled", "cancelled"}:
        sibling.resume_source_on_completion = True
        sibling_job = session.get(Job, sibling.job_id) if sibling.job_id else None
        if sibling_job is not None:
            sibling_job.payload_json = {
                **(sibling_job.payload_json or {}),
                "auto_resume_source_generation_run_id": source.id,
            }
        return None
    if source.status == "pausing":
        source_job = session.get(Job, source.job_id) if source.job_id else None
        if source_job is not None and source_job.status in {"running", "queued"}:
            source.pause_requested = False
            source.status = source_job.status
            source.updated_at = utcnow()
        return None
    if source.status != "paused":
        return None
    source.pause_requested = False
    source.status = "queued"
    source.updated_at = utcnow()
    job = jobs.enqueue_in_session(
        session, "generation.run",
        {"generation_run_id": source.id, "segment_ids": resume_segment_ids(session, source), "operation": "resume"},
        session_id=source.session_id,
        resource_keys=GenerationService._resource_keys(source.session_id, dict(source.settings_snapshot_json or {})),
    )
    source.job_id = job.id
    return job.id


def _same_input(source: GenerationSegment, target: GenerationSegment) -> bool:
    from .speech_plan_workspace import SIGNATURE_FIELDS

    return all(getattr(source, field) == getattr(target, field)
               for field in SIGNATURE_FIELDS if field != "ordinal")


def selection_guard(take: AudioTake | None) -> dict[str, Any]:
    return {"artifact_id": take.artifact_id if take else None,
            "revision": take.revision if take else None}


def selection_is_unchanged(take: AudioTake | None, expected: dict[str, Any] | None) -> bool:
    if expected is None or selection_guard(take) == expected:
        return True
    # A worker may have filled an empty edit-copy row while this replacement
    # waited. A cloned take with revision 1 has not been explicitly selected.
    return bool(expected.get("artifact_id") is None and take is not None
                and take.parent_take_id and take.revision == 1)


def capture_selection_guards(session: Any, revision_id: str) -> dict[str, Any]:
    rows = list(session.scalars(select(GenerationSegment).where(
        GenerationSegment.plan_revision_id == revision_id
    )))
    guards = {row.id: selection_guard(None) for row in rows}
    for take in session.scalars(select(AudioTake).join(
        GenerationSegment, GenerationSegment.id == AudioTake.generation_segment_id
    ).where(GenerationSegment.plan_revision_id == revision_id, AudioTake.is_active.is_(True))):
        guards[take.generation_segment_id] = selection_guard(take)
    return guards


def _copy_take(
    session: Any, source: GenerationSegment, target: GenerationSegment,
    take: AudioTake, artifact: Artifact, existing: list[AudioTake],
    expected_selection: dict[str, Any] | None = None,
) -> bool:
    if target.removed or source.source_segment_ids_json != target.source_segment_ids_json:
        return False
    if any(item.artifact_id == take.artifact_id for item in existing):
        return False
    metadata = artifact.metadata_json or {}
    matches = (
        take.status == "completed" and _same_input(source, target)
        and metadata.get("synthesized_text") == (target.optimized_text or target.text)
    )
    # An already selected, current take is an editorial choice, not an invitation
    # for a later ancestor result to overwrite it.
    current = next((item for item in existing if item.is_active), None)
    replace_current = (
        selection_is_unchanged(current, expected_selection)
        if expected_selection is not None
        else current is not None and current.status != "completed"
    )
    activate = current is None or (matches and replace_current)
    if activate:
        for item in existing:
            item.is_active = False
    clone = AudioTake(
        generation_segment_id=target.id,
        generation_run_id=None,
        artifact_id=take.artifact_id,
        parent_take_id=take.id,
        kind=take.kind,
        status="completed" if matches else "stale",
        settings_hash=take.settings_hash,
        duration_ms=take.duration_ms,
        is_active=activate,
        revision=take.revision,
    )
    session.add(clone)
    existing.append(clone)
    if activate:
        target.status = "completed" if matches else "stale"
        target.updated_at = utcnow()
    return True


def inherit_edit_copy_audio(session: Any, revision_id: str) -> int:
    """Reconcile older late arrivals at an explicit edit/generation mutation.

    This does not run from GET handlers. It repairs pre-fix edit copies as well
    as protecting a subsequent edit from dropping audio completed meanwhile.
    """
    revision = session.get(GenerationPlanRevision, revision_id)
    if revision is None:
        return 0
    ancestors = edit_copy_ancestors(session, revision)
    if not ancestors:
        return 0
    targets = {row.ordinal: row for row in session.scalars(
        select(GenerationSegment).where(GenerationSegment.plan_revision_id == revision_id)
    )}
    existing_by_id: dict[str, list[AudioTake]] = {}
    for take in session.scalars(select(AudioTake).join(
        GenerationSegment, GenerationSegment.id == AudioTake.generation_segment_id
    ).where(GenerationSegment.plan_revision_id == revision_id)):
        existing_by_id.setdefault(take.generation_segment_id, []).append(take)
    rows = session.execute(
        select(GenerationSegment, AudioTake, Artifact)
        .join(AudioTake, AudioTake.generation_segment_id == GenerationSegment.id)
        .join(Artifact, Artifact.id == AudioTake.artifact_id)
        .where(GenerationSegment.plan_revision_id.in_(ancestors),
               AudioTake.status.in_(("completed", "stale")))
        .order_by(AudioTake.created_at.desc(), AudioTake.id)
    ).all()
    changed = 0
    for source, take, artifact in rows:
        target = targets.get(source.ordinal)
        if target is not None:
            changed += _copy_take(session, source, target, take, artifact,
                                  existing_by_id.setdefault(target.id, []))
    if changed:
        session.flush()
    return changed


def publish_to_edit_copy(
    session: Any, source: GenerationSegment, take: AudioTake, artifact: Artifact,
    expected_selection: dict[str, Any] | None = None,
) -> bool:
    """Expose one successfully committed take to the active editorial descendant."""
    revision = session.get(GenerationPlanRevision, source.plan_revision_id)
    plan = session.get(GenerationPlan, revision.plan_id) if revision else None
    if plan is None or plan.active_revision_id == source.plan_revision_id:
        return False
    active = session.get(GenerationPlanRevision, plan.active_revision_id)
    if active is None or source.plan_revision_id not in edit_copy_ancestors(session, active):
        return False
    target = session.scalar(select(GenerationSegment).where(
        GenerationSegment.plan_revision_id == active.id,
        GenerationSegment.ordinal == source.ordinal,
    ))
    if target is None:
        return False
    existing = list(session.scalars(select(AudioTake).where(
        AudioTake.generation_segment_id == target.id
    )))
    return _copy_take(session, source, target, take, artifact, existing, expected_selection)
