"""Pure training state projection and caller-owned transaction repair."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Job, TrainingRun

TRAINING_ACTIVE_STATUSES = frozenset({"queued", "running", "cancel_requested"})
TRAINING_RETRYABLE_STATUSES = frozenset({"failed", "canceled", "interrupted"})
TRAINING_UNFINISHED_OUTCOME_MESSAGE = (
    "The training job finished without publishing a final training outcome."
)
TRAINING_MISSING_OWNER_MESSAGE = "The training job is missing or no longer owns this training run."


@dataclass(frozen=True, slots=True)
class TrainingState:
    status: str
    error_message: str | None
    updated_at: datetime


def is_training_job_owner(training: TrainingRun, job: Job | None) -> bool:
    """Accept only a job with both the domain link and training payload link."""

    return bool(
        job is not None
        and training.job_id == job.id
        and job.kind == "training.xtts"
        and isinstance(job.payload_json, dict)
        and job.payload_json.get("training_id") == training.id
    )


def effective_training_state(training: TrainingRun, job: Job | None) -> TrainingState:
    """Project an active run without changing records or consulting the clock."""

    existing = TrainingState(training.status, training.error_message, training.updated_at)
    if training.status not in TRAINING_ACTIVE_STATUSES:
        return existing
    if not is_training_job_owner(training, job):
        return TrainingState(
            "canceled" if training.status == "cancel_requested" else "interrupted",
            training.error_message or TRAINING_MISSING_OWNER_MESSAGE,
            training.updated_at,
        )
    # The ownership guard above requires a job, but keep narrowing explicit.
    assert job is not None
    if job.status in TRAINING_RETRYABLE_STATUSES:
        return TrainingState(job.status, job.error_message, job.updated_at)
    if job.status == "cancel_requested":
        return TrainingState("cancel_requested", job.error_message, job.updated_at)
    if job.status == "succeeded":
        return TrainingState("failed", TRAINING_UNFINISHED_OUTCOME_MESSAGE, job.updated_at)
    return existing


def reconcile_training_runs_in_session(
    session: Session, job_ids: Sequence[str] | None = None
) -> None:
    """Persist projections in a serialized writer, optionally for affected jobs."""

    if job_ids is not None and not job_ids:
        return
    statement = (
        select(TrainingRun, Job)
        .outerjoin(Job, TrainingRun.job_id == Job.id)
        .where(TrainingRun.status.in_(TRAINING_ACTIVE_STATUSES))
    )
    if job_ids is not None:
        statement = statement.where(TrainingRun.job_id.in_(job_ids))
    for training, job in session.execute(statement):
        state = effective_training_state(training, job)
        training.status = state.status
        training.error_message = state.error_message
        training.updated_at = state.updated_at
