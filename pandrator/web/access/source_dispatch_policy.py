"""Scalar source-dispatch authority before payloads, native work or replay."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..dispatch import DispatchError
from ..job_execution import current_job_lease
from ..models import Artifact, Job, SourceCleaningDispatchBatch, SourceCleaningDispatchRun
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .session_policy import SessionNotFound
from .source_dependency_visibility import current_source_dependency_clause


def require_source_dispatch_access(
    unit: Session,
    resource: str,
    identifier: str,
    action: ProjectAction = ProjectAction.READ,
) -> None:
    if type(resource) is not str or resource not in {"session", "run", "batch"}:
        raise TypeError("A source-dispatch session, run or batch resource is required.")
    if type(identifier) is not str or not identifier.strip():
        raise TypeError("A source-dispatch identifier is required.")
    if type(action) is not ProjectAction or action not in {ProjectAction.READ, ProjectAction.RUN}:
        raise TypeError("A source-dispatch READ or RUN action is required.")
    if current_execution_actor.get() is None:
        return
    with unit.no_autoflush:
        if resource == "session":
            session_id = identifier
        elif resource == "run":
            session_id = unit.scalar(
                select(SourceCleaningDispatchRun.session_id).where(
                    SourceCleaningDispatchRun.id == identifier,
                )
            )
        else:
            session_id = unit.scalar(
                select(SourceCleaningDispatchRun.session_id)
                .join(
                    SourceCleaningDispatchBatch,
                    SourceCleaningDispatchBatch.dispatch_run_id == SourceCleaningDispatchRun.id,
                )
                .where(SourceCleaningDispatchBatch.id == identifier)
            )
        if session_id is None:
            raise DispatchError("not_found", "Source-cleaning dispatch not found.", 404)
        try:
            require_current_session_access(unit, session_id, action)
        except SessionNotFound:
            raise DispatchError("not_found", "Source-cleaning dispatch not found.", 404) from None


def require_source_preparation_access(unit: Session, run_id: str) -> None:
    if current_execution_actor.get() is None:
        return
    require_source_dispatch_access(unit, "run", run_id, ProjectAction.RUN)
    lease = current_job_lease.get()
    if lease is None:
        return
    with unit.no_autoflush:
        identifier = unit.scalar(
            select(SourceCleaningDispatchRun.id)
            .join(
                Job,
                Job.id == SourceCleaningDispatchRun.job_id,
            )
            .where(
                SourceCleaningDispatchRun.id == run_id,
                SourceCleaningDispatchRun.job_id == lease["job_id"],
                Job.kind == "source.cleaning_dispatch.prepare",
                Job.session_id == SourceCleaningDispatchRun.session_id,
            )
        )
    if identifier is None:
        raise AccessDenied()


def _require_source_input_access(unit: Session, session_id: str, artifact_id: str | None) -> None:
    """Caller proves root and actor lifetime in this same unit before this leaf."""
    actor = current_execution_actor.get()
    if actor is None or type(actor) is HostActor:
        return
    if type(actor) is not UserActor:
        raise AccessDenied()
    with unit.no_autoflush:
        identifier = unit.scalar(
            select(Artifact.id).where(
                Artifact.id == artifact_id,
                or_(
                    resource_visibility_clause(Artifact, actor),
                    current_source_dependency_clause(actor, session_id=session_id),
                ),
            )
        )
    if identifier is None:
        raise DispatchError("source_not_found", "Source artifact not found.", 404)
