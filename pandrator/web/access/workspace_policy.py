"""Fresh workspace authority shared by metadata, commands and folder access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.models import Workspace, WorkspaceMembership
from .contracts import AccessContext, AccessDenied, ProjectAction, WorkspaceNotFound
from .project_permissions import workspace_roles_for_action


class WorkspaceAction(str, Enum):
    READ = "read"
    EDIT = "edit"
    MANAGE_READ = "manage_read"
    MANAGE = "manage"


_PROJECT_ACTION = {
    WorkspaceAction.READ: ProjectAction.READ,
    WorkspaceAction.EDIT: ProjectAction.EDIT,
    WorkspaceAction.MANAGE_READ: ProjectAction.MANAGE_MEMBERS,
    WorkspaceAction.MANAGE: ProjectAction.MANAGE_MEMBERS,
}


@dataclass(frozen=True, slots=True)
class WorkspaceAccess:
    id: str
    kind: str
    name: str
    state: str
    revision: int
    role: str
    allowed_actions: frozenset[WorkspaceAction]
    created_at: datetime
    updated_at: datetime


def require_workspace_actor(
    session: Session, context: AccessContext, *, write: bool = False, enroll: bool = False
) -> None:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    scopes = ["app.read"]
    if write or enroll:
        scopes.append("app.write")
    if enroll:
        scopes.append("app.workspaces.enroll")
    try:
        with session.no_autoflush:
            require_actor_in_session(session, context.actor, required_scopes=scopes)
    except PermissionError:
        raise AccessDenied() from None
    if enroll and context.actor.principal_kind not in {
        "owner_session",
        "api_token",
        "automation_client",
    }:
        raise AccessDenied()


def _workspace_statement(
    context: AccessContext,
    *,
    action: WorkspaceAction = WorkspaceAction.READ,
    allow_archived: bool = False,
) -> Select[Any]:
    if type(action) is not WorkspaceAction:
        raise TypeError("A valid workspace action is required.")
    if allow_archived and action not in {WorkspaceAction.MANAGE_READ, WorkspaceAction.MANAGE}:
        raise AccessDenied()
    roles = workspace_roles_for_action(_PROJECT_ACTION[action])
    return (
        select(
            Workspace.id,
            Workspace.kind,
            Workspace.name,
            Workspace.state,
            Workspace.revision,
            WorkspaceMembership.role,
            Workspace.created_at,
            Workspace.updated_at,
        )
        .join(
            WorkspaceMembership,
            and_(
                WorkspaceMembership.workspace_id == Workspace.id,
                WorkspaceMembership.user_id == context.actor.user_id,
            ),
        )
        .where(
            or_(
                and_(Workspace.kind == "team", WorkspaceMembership.role.in_(roles)),
                and_(
                    Workspace.kind == "personal",
                    Workspace.personal_owner_user_id == context.actor.user_id,
                    WorkspaceMembership.role == "owner",
                ),
            ),
            credential_workspace_clause(context.actor),
            Workspace.state.in_(("active", "archived"))
            if allow_archived
            else Workspace.state == "active",
        )
    )


def _workspace_access(row: Any, context: AccessContext) -> WorkspaceAccess:
    allowed = frozenset(
        action
        for action in WorkspaceAction
        if row.role in workspace_roles_for_action(_PROJECT_ACTION[action])
        and "app.read" in context.actor.scopes
        and (
            action in {WorkspaceAction.READ, WorkspaceAction.MANAGE_READ}
            or "app.write" in context.actor.scopes
        )
        and (
            row.state == "active" or action in {WorkspaceAction.MANAGE_READ, WorkspaceAction.MANAGE}
        )
    )
    return WorkspaceAccess(
        row.id,
        row.kind,
        row.name,
        row.state,
        row.revision,
        row.role,
        allowed,
        row.created_at,
        row.updated_at,
    )


def require_workspace_access(
    session: Session,
    context: AccessContext,
    workspace_id: str | None = None,
    *,
    action: WorkspaceAction = WorkspaceAction.READ,
    allow_archived: bool = False,
) -> WorkspaceAccess:
    if type(action) is not WorkspaceAction:
        raise TypeError("A valid workspace action is required.")
    require_workspace_actor(
        session, context, write=action in {WorkspaceAction.EDIT, WorkspaceAction.MANAGE}
    )
    statement = _workspace_statement(context, action=action, allow_archived=allow_archived)
    if workspace_id is None:
        statement = statement.where(
            Workspace.kind == "personal", Workspace.personal_owner_user_id == context.actor.user_id
        )
    else:
        statement = statement.where(Workspace.id == workspace_id)
    with session.no_autoflush:
        row = session.execute(statement).one_or_none()
    if row is None:
        raise WorkspaceNotFound()
    return _workspace_access(row, context)


def workspace_payload(access: WorkspaceAccess) -> dict[str, Any]:
    return {
        "id": access.id,
        "kind": access.kind,
        "name": access.name,
        "state": access.state,
        "revision": access.revision,
        "role": "owner" if access.kind == "personal" else public_workspace_role(access.role),
        "allowed_actions": [
            action.value
            for action in (WorkspaceAction.READ, WorkspaceAction.EDIT, WorkspaceAction.MANAGE)
            if action in access.allowed_actions
        ],
        "created_at": access.created_at.isoformat(),
        "updated_at": access.updated_at.isoformat(),
    }


def public_workspace_role(role: str) -> str:
    return "manager" if role in {"owner", "admin"} else "member"
