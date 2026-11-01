"""Current owner-only private transcription authority, before protected content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import UserActor, current_execution_actor
from ..accounts.private_work_models import QuickTranscriptionOwner
from ..job_execution import current_job_lease
from ..models import Job, JobActorRecord, QuickTranscription, utcnow
from .contracts import AccessDenied
from .private_transcription_policy import PrivateTranscriptionNotFound
from .private_workspace_policy import require_private_execution_workspace


@dataclass(frozen=True, slots=True)
class PrivateTranscriptionInput:
    actor: UserActor
    lease: tuple[str, str, int]
    transcription_id: str
    owner_user_id: str
    workspace_id: str
    job_id: str
    source_suffix: str
    format: str
    size_bytes: int
    sha256: str
    uploaded_bytes: int
    next_chunk_index: int


def _private_lease(value: Any) -> tuple[str, str, int] | None:
    if value is None:
        return None
    if (
        type(value) is not dict
        or value.keys() != {"job_id", "owner_id", "lease_generation"}
        or any(
            type(value.get(key)) is not str or not value[key] or value[key] != value[key].strip()
            for key in ("job_id", "owner_id")
        )
        or type(value.get("lease_generation")) is not int
        or value["lease_generation"] <= 0
    ):
        raise AccessDenied()
    return value["job_id"], value["owner_id"], value["lease_generation"]


def require_private_transcription_execution(
    unit: Session,
    payload: dict[str, Any],
    *,
    expected: PrivateTranscriptionInput | None = None,
    allow_finished: bool = False,
) -> PrivateTranscriptionInput | None:
    actor = current_execution_actor.get()
    lease = _private_lease(current_job_lease.get())
    if expected is not None and (actor != expected.actor or lease != expected.lease):
        raise AccessDenied()
    if allow_finished and expected is None:
        raise AccessDenied()
    identifier = str(payload.get("transcription_id") or "")
    if actor is None:
        # Missing task Context must not turn any captured private job into legacy work.
        with unit.no_autoflush:
            bound_job = unit.scalar(
                select(QuickTranscription.job_id).where(QuickTranscription.id == identifier)
            )
            ids = {
                value
                for value in (payload.get("_job_id"), bound_job, lease[0] if lease else None)
                if isinstance(value, str) and value
            }
            captured = (
                unit.scalar(
                    select(JobActorRecord.job_id).where(JobActorRecord.job_id.in_(ids)).limit(1)
                )
                if ids
                else None
            )
        if captured is not None:
            raise AccessDenied()
        return None
    if type(actor) is not UserActor or lease is None:
        raise AccessDenied()
    if (
        payload.get("_job_id") != lease[0]
        or type(payload.get("_lease_generation")) is not int
        or payload.get("_lease_generation") != lease[2]
    ):
        raise AccessDenied()
    workspace_id = require_private_execution_workspace(unit, actor)
    with unit.no_autoflush:
        job_id = unit.scalar(
            select(Job.id).where(
                Job.id == lease[0],
                Job.kind == "transcription.quick",
                Job.workspace_id == workspace_id,
                Job.project_id.is_(None),
                Job.session_id.is_(None),
                Job.workflow_run_id.is_(None),
            )
        )
        if job_id is None:
            raise AccessDenied()
        selector = unit.scalar(
            select(Job.payload_json["transcription_id"].as_string()).where(Job.id == job_id)
        )
        if selector != identifier:
            raise AccessDenied()
        row = unit.execute(
            select(
                QuickTranscription.id,
                QuickTranscriptionOwner.user_id,
                QuickTranscriptionOwner.workspace_id,
                QuickTranscription.job_id,
                QuickTranscription.source_suffix,
                QuickTranscription.format,
                QuickTranscription.size_bytes,
                QuickTranscription.sha256,
                QuickTranscription.uploaded_bytes,
                QuickTranscription.next_chunk_index,
            )
            .join(
                QuickTranscriptionOwner,
                QuickTranscriptionOwner.transcription_id == QuickTranscription.id,
            )
            .where(
                QuickTranscription.id == identifier,
                QuickTranscriptionOwner.user_id == actor.user_id,
                QuickTranscriptionOwner.workspace_id == workspace_id,
                QuickTranscription.job_id == job_id,
                QuickTranscription.state.in_(
                    ("submitted", "finished") if allow_finished else ("submitted",)
                ),
                QuickTranscription.expires_at > utcnow(),
                QuickTranscription.size_bytes > 0,
                QuickTranscription.uploaded_bytes == QuickTranscription.size_bytes,
            )
        ).one_or_none()
        if row is None:
            raise PrivateTranscriptionNotFound()
    proof = PrivateTranscriptionInput(actor, lease, *row)
    if expected is not None and proof != expected:
        raise AccessDenied()
    return proof
