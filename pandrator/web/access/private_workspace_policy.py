"""Personal work authority at strict admission and proved worker execution."""

from __future__ import annotations

from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import UserActor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace, WorkspaceMembership
from .contracts import AccessContext, AccessDenied


class PrivateWorkAction(str, Enum):
    READ = "read"
    RUN = "run"
    CANCEL = "cancel"


def _fresh_actor(session: Session, context: AccessContext, action: PrivateWorkAction) -> UserActor:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(action) is not PrivateWorkAction:
        raise TypeError("A private work action is required.")
    try:
        with session.no_autoflush:
            require_actor_in_session(
                session, context.actor, required_scopes=(f"app.{action.value}",)
            )
    except PermissionError:
        raise AccessDenied() from None
    return context.actor


def _workspace_predicates(actor: UserActor):
    return (
        Workspace.kind == "personal",
        Workspace.state == "active",
        Workspace.personal_owner_user_id == actor.user_id,
        WorkspaceMembership.user_id == actor.user_id,
        WorkspaceMembership.role == "owner",
        credential_workspace_clause(actor),
    )


def _workspace_for_actor(session: Session, actor: UserActor) -> str:
    with session.no_autoflush:
        workspace_id = session.scalar(
            select(Workspace.id)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(*_workspace_predicates(actor))
        )
    if workspace_id is None:
        raise AccessDenied()
    return workspace_id


def require_private_workspace(
    session: Session,
    context: AccessContext,
    action: PrivateWorkAction = PrivateWorkAction.RUN,
) -> str:
    return _workspace_for_actor(session, _fresh_actor(session, context, action))


def require_private_execution_workspace(session: Session, actor: UserActor) -> str:
    """Keep natural expiry/logout separate from current account/resource rights."""
    if type(actor) is not UserActor:
        raise TypeError("An authenticated user actor is required.")
    try:
        with session.no_autoflush:
            require_execution_actor_in_session(session, actor, required_scopes=("app.run",))
    except PermissionError:
        raise AccessDenied() from None
    return _workspace_for_actor(session, actor)


__all__ = [
    "PrivateWorkAction",
    "require_private_execution_workspace",
    "require_private_workspace",
]
