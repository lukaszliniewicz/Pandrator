"""Fresh by-ID workspace authority for managed voice records."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import Voice
from ..voice_queries import voice_in_workspace
from .contracts import AccessContext, AccessDenied, ProjectAction, WorkspaceNotFound
from .project_visibility import workspace_authority
from .workspace_policy import WorkspaceAction, require_workspace_access


def require_current_voice_library_read(session: Session, workspace_id: str) -> None:
    """Prove library access separately from project access before source bodies."""
    actor = current_execution_actor.get()
    if actor is None:
        return
    with session.no_autoflush:
        if type(actor) is UserActor and current_job_lease.get() is None:
            require_workspace_access(session, AccessContext(actor), workspace_id)
            return
        if type(actor) not in {UserActor, HostActor}:
            raise AccessDenied()
        try:
            require_execution_actor_in_session(
                session, actor, required_scopes=("app.read",), allow_host=type(actor) is HostActor,
            )
        except PermissionError:
            raise AccessDenied() from None
        if type(actor) is UserActor and session.scalar(
            select(Workspace.id).where(
                Workspace.id == workspace_id, Workspace.state == "active",
                workspace_authority(actor.user_id, action=ProjectAction.READ),
                credential_workspace_clause(actor),
            )
        ) is None:
            raise WorkspaceNotFound()


def require_voice_access(
    session: Session,
    context: AccessContext,
    voice_id: str,
    *,
    workspace_id: str | None = None,
    action: WorkspaceAction = WorkspaceAction.READ,
) -> Voice:
    with session.no_autoflush:
        stored_workspace = session.scalar(select(Voice.workspace_id).where(Voice.id == voice_id))
        if (
            stored_workspace is None
            or workspace_id is not None
            and workspace_id != stored_workspace
        ):
            raise WorkspaceNotFound()
        proved = require_workspace_access(session, context, stored_workspace, action=action)
        voice = voice_in_workspace(session, voice_id, workspace_id=proved.id)
        if voice is None:
            raise WorkspaceNotFound()
        return voice


def require_voice_run_access(
    session: Session,
    context: AccessContext,
    voice_id: str,
    *,
    workspace_id: str | None = None,
) -> Voice:
    with session.no_autoflush:
        try:
            require_actor_in_session(
                session, context.actor, required_scopes=("app.read", "app.write", "app.run")
            )
        except PermissionError:
            raise AccessDenied() from None
        return require_voice_access(
            session, context, voice_id, workspace_id=workspace_id, action=WorkspaceAction.EDIT
        )
