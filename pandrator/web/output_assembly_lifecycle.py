"""Output assembly invalidation within a caller-owned transaction."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .jobs import JobQueue
from .models import Artifact, GenerationRun, Job, OutputAssembly, utcnow


def mark_output_assemblies_stale(
    session: Session,
    session_id: str,
    *,
    generation_run_id: str | None = None,
    include_later_runs: bool = False,
    cancel_active: bool = False,
    jobs: JobQueue | None = None,
) -> None:
    """Invalidate assemblies and their exports after audio-plan changes.

    Assemblies are scoped to the run whose takes were produced or replaced.
    ``include_later_runs`` additionally invalidates same-plan run-scoped
    assemblies whose chronological fallback can inherit a changed output run.
    ``cancel_active`` additionally stops queued/running current-selection work:
    historical assemblies of other completed runs remain previewable.  When no
    run is given (segment edits, take selection), only current-selection
    assemblies without a run are affected, because run-scoped assemblies keep
    reproducing the immutable takes of their own run.
    """
    from .artifacts import ArtifactService

    statuses = (
        ("completed", "queued", "running", "cancel_requested")
        if cancel_active
        else ("completed",)
    )
    filters = [
        OutputAssembly.session_id == session_id,
        OutputAssembly.status.in_(statuses),
    ]
    if generation_run_id is None:
        filters.append(OutputAssembly.generation_run_id.is_(None))
    else:
        run_filter = OutputAssembly.generation_run_id == generation_run_id
        if include_later_runs:
            selected_run = session.get(GenerationRun, generation_run_id)
            if selected_run is None or selected_run.session_id != session_id:
                raise KeyError(generation_run_id)
            later_run_ids = select(GenerationRun.id).where(
                GenerationRun.session_id == session_id,
                GenerationRun.plan_revision_id == selected_run.plan_revision_id,
                GenerationRun.sequence_number >= selected_run.sequence_number,
            )
            run_filter = OutputAssembly.generation_run_id.in_(later_run_ids)
        filters.append((OutputAssembly.generation_run_id.is_(None)) | run_filter)
    records = list(session.scalars(select(OutputAssembly).where(*filters)).all())
    for record in records:
        job = session.get(Job, record.job_id) if record.job_id else None
        if (
            cancel_active
            and job is not None
            and job.status
            in {
                "queued",
                "running",
                "cancel_requested",
            }
        ):
            if jobs is None:
                raise RuntimeError(
                    "A job queue is required to cancel active assemblies."
                )
            jobs.request_cancel_in_session(session, job.id)
            record.status = (
                "canceled" if job.status == "canceled" else "cancel_requested"
            )
        else:
            record.status = "stale"
        record.updated_at = utcnow()
        if record.artifact_id:
            artifact = session.get(Artifact, record.artifact_id)
            if artifact is not None:
                artifact.state = "stale"
                ArtifactService._mark_descendants_stale(session, artifact.id)

