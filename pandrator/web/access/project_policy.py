"""Fresh, transaction-scoped project and workspace access decisions."""

from __future__ import annotations

from typing import cast

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.credential_resources import (
    credential_project_clause,
    credential_workspace_clause,
)
from ..accounts.models import Workspace, WorkspaceMembership
from ..projects.models import Project, ProjectMembership
from .contracts import (
    AccessContext,
    AccessDenied,
    ProjectAccess,
    ProjectAction,
    ProjectNotFound,
    ProjectRole,
    WorkspaceNotFound,
    WorkspaceRole,
)
from .project_permissions import (
    ACTION_SCOPES,
    PROJECT_ROLE_ACTIONS,
    project_actions_for_role,
    validate_project_action,
    workspace_actions_for_role,
)
from .project_visibility import (
    WORKSPACE_ROLES,
    project_ids_for_user,
    shared_project_source_clause,
)

_WORKSPACE_ROLES = frozenset(WORKSPACE_ROLES)
_PROJECT_ROLES = frozenset(PROJECT_ROLE_ACTIONS)


def _validate_context(context: object) -> AccessContext:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    return cast(AccessContext, context)


def _validate_action(action: object) -> ProjectAction:
    return validate_project_action(action)


def _fresh_actor(session: Session, context: object):
    validated = _validate_context(context)
    try:
        with session.no_autoflush:
            require_actor_in_session(
                session,
                validated.actor,
                required_scopes=("app.read",),
            )
    except PermissionError:
        raise AccessDenied() from None
    return validated.actor


def _visible_project_ids_statement(
    user_id: str,
    action: ProjectAction,
) -> Select[tuple[str]]:
    return project_ids_for_user(user_id, action=action)


def _allowed_actions(
    roles: frozenset[ProjectAction],
    scopes: frozenset[str],
) -> frozenset[ProjectAction]:
    return frozenset(action for action in roles if ACTION_SCOPES[action] <= scopes)


def require_project_access(
    session: Session,
    context: AccessContext,
    project_id: str,
    action: ProjectAction = ProjectAction.READ,
) -> ProjectAccess:
    """Return current project access or a fixed, non-disclosing denial."""
    validated_action = _validate_action(action)
    actor = _fresh_actor(session, context)
    with session.no_autoflush:
        row = session.execute(
            select(
                Project.id,
                Project.workspace_id,
                Workspace.kind,
                Workspace.state,
                Workspace.personal_owner_user_id,
                WorkspaceMembership.role.label("workspace_membership_role"),
                ProjectMembership.role.label("project_membership_role"),
                shared_project_source_clause().label("guest_source_available"),
            )
            .join(Workspace, Workspace.id == Project.workspace_id)
            .outerjoin(
                WorkspaceMembership,
                and_(
                    WorkspaceMembership.workspace_id == Workspace.id,
                    WorkspaceMembership.user_id == actor.user_id,
                ),
            )
            .outerjoin(
                ProjectMembership,
                and_(
                    ProjectMembership.project_id == Project.id,
                    ProjectMembership.user_id == actor.user_id,
                ),
            )
            .where(Project.id == project_id, credential_project_clause(actor))
        ).one_or_none()

    if row is None:
        raise ProjectNotFound()

    stored_project_id, workspace_id, kind, state, owner_id, workspace_role_raw, project_role_raw, guest_source_available = (
        row
    )
    if state != "active":
        raise ProjectNotFound()

    workspace_role: WorkspaceRole | None = None
    if kind == "team" and workspace_role_raw in _WORKSPACE_ROLES:
        workspace_role = cast(WorkspaceRole, workspace_role_raw)
    elif kind == "personal" and owner_id == actor.user_id and workspace_role_raw == "owner":
        workspace_role = "owner"

    project_role: ProjectRole | None = None
    if project_role_raw in _PROJECT_ROLES:
        project_role = cast(ProjectRole, project_role_raw)

    if workspace_role is not None:
        role_actions = workspace_actions_for_role(workspace_role)
    elif project_role is not None:
        if not guest_source_available:
            raise ProjectNotFound()
        role_actions = project_actions_for_role(project_role)
    else:
        raise ProjectNotFound()

    allowed_actions = _allowed_actions(role_actions, actor.scopes)
    if validated_action not in allowed_actions:
        raise AccessDenied()

    return ProjectAccess(
        project_id=stored_project_id,
        workspace_id=workspace_id,
        workspace_role=workspace_role,
        project_role=project_role,
        allowed_actions=allowed_actions,
    )


def visible_project_ids_statement(
    session: Session,
    context: AccessContext,
    action: ProjectAction = ProjectAction.READ,
) -> Select[tuple[str]]:
    """Build a SQL-scoped project visibility query for this transaction."""
    validated_action = _validate_action(action)
    actor = _fresh_actor(session, context)
    if not ACTION_SCOPES[validated_action] <= actor.scopes:
        raise AccessDenied()
    return _visible_project_ids_statement(actor.user_id, validated_action).where(
        credential_project_clause(actor)
    )


def require_project_creation(
    session: Session,
    context: AccessContext,
    workspace_id: str,
) -> str:
    """Require active workspace-level authority to create a project."""
    actor = _fresh_actor(session, context)
    if not {"app.read", "app.write"} <= actor.scopes:
        raise AccessDenied()

    with session.no_autoflush:
        row = session.execute(
            select(
                Workspace.id,
                Workspace.kind,
                Workspace.state,
                Workspace.personal_owner_user_id,
                WorkspaceMembership.role,
            )
            .outerjoin(
                WorkspaceMembership,
                and_(
                    WorkspaceMembership.workspace_id == Workspace.id,
                    WorkspaceMembership.user_id == actor.user_id,
                ),
            )
            .where(Workspace.id == workspace_id, credential_workspace_clause(actor))
        ).one_or_none()

    if row is None:
        raise WorkspaceNotFound()
    stored_id, kind, state, owner_id, role = row
    if state != "active":
        raise WorkspaceNotFound()
    if kind == "team" and role in _WORKSPACE_ROLES:
        return stored_id
    if kind == "personal" and owner_id == actor.user_id and role == "owner":
        return stored_id
    raise WorkspaceNotFound()


def personal_workspace_id(session: Session, context: AccessContext) -> str:
    """Resolve only the current actor's structurally owned active workspace."""
    actor = _fresh_actor(session, context)
    with session.no_autoflush:
        workspace_id = session.scalar(
            select(Workspace.id)
            .join(
                WorkspaceMembership,
                and_(
                    WorkspaceMembership.workspace_id == Workspace.id,
                    WorkspaceMembership.user_id == actor.user_id,
                    WorkspaceMembership.role == "owner",
                ),
            )
            .where(
                Workspace.kind == "personal",
                Workspace.state == "active",
                Workspace.personal_owner_user_id == actor.user_id,
            )
        )
    if workspace_id is None:
        raise WorkspaceNotFound()
    return workspace_id


__all__ = [
    "personal_workspace_id",
    "require_project_access",
    "require_project_creation",
    "visible_project_ids_statement",
]
