"""Fresh authority checks for source URL download and source reuse jobs."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..job_execution import current_job_lease
from ..models import Artifact, Job, SessionRecord
from ..projects.models import Project
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .source_dependency_visibility import current_source_dependency_clause

_SOURCE_INGRESS_JOB_KINDS = frozenset({"source.download_url", "source.reuse"})


def require_source_ingress_access(
    unit: Session,
    session_id: str,
    job_kind: str,
    *,
    artifact_id: str | None = None,
) -> None:
    """Prove current source ingress authority before reading or publishing bytes."""
    if type(job_kind) is not str or job_kind not in _SOURCE_INGRESS_JOB_KINDS:
        raise TypeError("A source.download_url or source.reuse job kind is required.")

    actor = current_execution_actor.get()
    if actor is None:
        return

    require_current_session_access(unit, session_id, ProjectAction.EDIT)
    require_current_session_access(unit, session_id, ProjectAction.RUN)

    lease = current_job_lease.get()
    if lease is not None:
        with unit.no_autoflush:
            job_id = unit.scalar(
                select(Job.id).where(
                    Job.id == lease["job_id"],
                    Job.kind == job_kind,
                    Job.session_id == session_id,
                )
            )
        if job_id is None:
            raise AccessDenied()

    if artifact_id is None or type(actor) is HostActor:
        return
    if type(actor) is not UserActor:
        raise AccessDenied()

    target_workspace_id = (
        select(Project.workspace_id)
        .join(SessionRecord, SessionRecord.project_id == Project.id)
        .where(SessionRecord.id == session_id)
        .scalar_subquery()
    )
    with unit.no_autoflush:
        identifier = unit.scalar(
            select(Artifact.id).where(
                Artifact.id == artifact_id,
                Artifact.state != "deleted",
                Artifact.workspace_id == target_workspace_id,
                or_(
                    resource_visibility_clause(Artifact, actor),
                    current_source_dependency_clause(actor, session_id=session_id),
                ),
            )
        )
    if identifier is None:
        raise KeyError("Source artifact not found.")


__all__ = ["require_source_ingress_access"]
