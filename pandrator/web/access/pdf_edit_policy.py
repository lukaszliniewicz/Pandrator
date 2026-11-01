"""Current target and selected source byte authority for derived PDF editing."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import Artifact, Job, SessionRecord
from ..projects.models import Project
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .project_permissions import ACTION_SCOPES
from .session_policy import SessionNotFound
from .source_dependency_visibility import current_source_dependency_clause


@dataclass(frozen=True, slots=True)
class PdfEditInput:
    target_session_id: str
    target_project_id: str
    workspace_id: str
    storage_key: str
    source_artifact_id: str
    source_project_id: str | None
    source_session_id: str | None
    relative_path: str
    content_hash: str | None


def require_pdf_edit_access(
    unit: Session,
    session_id: str | None,
    source_artifact_id: str,
    *,
    expected: PdfEditInput | None = None,
) -> PdfEditInput | None:
    """Prove current RUN/input authority without hydrating source metadata."""
    actor = current_execution_actor.get()
    if actor is None:
        return None
    lease = current_job_lease.get()
    if type(actor) is HostActor and lease is None:
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=ACTION_SCOPES[ProjectAction.RUN],
                allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None
        return None
    if type(actor) not in (UserActor, HostActor):
        raise AccessDenied()
    if not session_id:
        raise SessionNotFound()
    require_current_session_access(unit, session_id, ProjectAction.RUN)
    with unit.no_autoflush:
        target = unit.execute(
            select(
                SessionRecord.id,
                SessionRecord.storage_key,
                Project.id.label("project_id"),
                Project.workspace_id,
            )
            .join(Project, Project.id == SessionRecord.project_id)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .where(
                SessionRecord.id == session_id,
                SessionRecord.trashed_at.is_(None),
                Workspace.state == "active",
            )
        ).one_or_none()
        if target is None:
            raise SessionNotFound()
        source_statement = select(
            Artifact.id,
            Artifact.workspace_id,
            Artifact.project_id,
            Artifact.session_id,
            Artifact.relative_path,
            Artifact.content_hash,
        ).where(
            Artifact.id == source_artifact_id,
            Artifact.state != "deleted",
            Artifact.workspace_id == target.workspace_id,
        )
        if type(actor) is UserActor:
            source_statement = source_statement.where(
                or_(
                    resource_visibility_clause(Artifact, actor),
                    current_source_dependency_clause(actor, session_id=session_id),
                )
            )
        source = unit.execute(source_statement).one_or_none()
        if source is None:
            raise KeyError("Source artifact not found.")
        if lease is not None:
            if (
                type(lease) is not dict
                or lease.keys() != {"job_id", "owner_id", "lease_generation"}
                or any(
                    type(lease[key]) is not str or not lease[key] for key in ("job_id", "owner_id")
                )
                or type(lease["lease_generation"]) is not int
                or lease["lease_generation"] <= 0
            ):
                raise AccessDenied()
            if (
                unit.scalar(
                    select(Job.id).where(
                        Job.id == lease["job_id"],
                        Job.kind == "pdf.apply_edits",
                        Job.session_id == target.id,
                        Job.project_id == target.project_id,
                        Job.workspace_id == target.workspace_id,
                        Job.payload_json["session_id"].as_string() == session_id,
                        Job.payload_json["source_artifact_id"].as_string() == source_artifact_id,
                    )
                )
                is None
            ):
                raise AccessDenied()
        captured = PdfEditInput(
            target.id,
            target.project_id,
            target.workspace_id,
            target.storage_key,
            source.id,
            source.project_id,
            source.session_id,
            source.relative_path,
            source.content_hash,
        )
        if expected is not None and expected != captured:
            raise KeyError("Source artifact not found.")
        return captured


__all__ = ["PdfEditInput", "require_pdf_edit_access"]
