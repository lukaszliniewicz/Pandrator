"""Header-only cover access inside an already-authorized assembly unit."""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.credential_resources import credential_project_clause, credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.models import Workspace, WorkspaceMembership
from ..models import Artifact, SessionRecord
from ..projects.models import Project
from .contracts import AccessDenied
from .project_visibility import project_ids_for_user


class AssemblyCoverInput(NamedTuple):
    artifact_id: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    content_hash: str | None


def require_assembly_cover(
    session: Session,
    artifact_id: str,
    *,
    expected: AssemblyCoverInput | None = None,
) -> AssemblyCoverInput:
    """Caller first proves current assembly actor/root in this same unit.

    A project cover requires current project READ, including credential resource
    ceilings. Workspace-owned images require actual workspace membership and a
    workspace credential grant. This never reads a path, metadata or image bytes.
    """
    if type(artifact_id) is not str or not artifact_id or artifact_id != artifact_id.strip():
        raise AccessDenied()
    if expected is not None and type(expected) is not AssemblyCoverInput:
        raise AccessDenied()
    with session.no_autoflush:
        row = session.execute(
            select(
                Artifact.id,
                Artifact.workspace_id,
                Artifact.project_id,
                Artifact.session_id,
                Artifact.content_hash,
                Project.id.label("owner_project"),
                SessionRecord.project_id.label("session_project"),
            )
            .select_from(Artifact)
            .join(Workspace, Workspace.id == Artifact.workspace_id)
            .outerjoin(Project, Project.id == Artifact.project_id)
            .outerjoin(SessionRecord, SessionRecord.id == Artifact.session_id)
            .where(
                Artifact.id == artifact_id,
                Artifact.state == "current",
                Artifact.mime_type.like("image/%"),
                Workspace.state == "active",
            )
        ).one_or_none()
        if (
            row is None
            or (row.project_id is not None and row.owner_project is None)
            or (row.session_id is not None and row.session_project != row.project_id)
        ):
            raise AccessDenied()
        actor = current_execution_actor.get()
        if type(actor) is UserActor:
            if row.project_id is not None:
                allowed = session.scalar(
                    select(Project.id).where(
                        Project.id == row.project_id,
                        Project.workspace_id == row.workspace_id,
                        Project.id.in_(project_ids_for_user(actor.user_id)),
                        credential_project_clause(actor),
                    )
                )
            else:
                allowed = session.scalar(
                    select(Workspace.id)
                    .join(
                        WorkspaceMembership,
                        WorkspaceMembership.workspace_id == Workspace.id,
                    )
                    .where(
                        Workspace.id == row.workspace_id,
                        WorkspaceMembership.user_id == actor.user_id,
                        credential_workspace_clause(actor),
                    )
                )
            if allowed is None:
                raise AccessDenied()
        elif actor is not None and type(actor) is not HostActor:
            raise AccessDenied()
        result = AssemblyCoverInput(*row[:5])
        if expected is not None and result != expected:
            raise AccessDenied()
        return result
