"""User-role SQL shared by content policy and credential grant validation."""

from __future__ import annotations

from sqlalchemy import Select, and_, exists, false, not_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.models import Workspace, WorkspaceMembership
from ..models import SessionRecord
from ..projects.models import Project, ProjectMembership
from .contracts import ProjectAction
from .project_permissions import (
    GOVERNANCE_ACTIONS,
    PROJECT_ROLES,
    project_roles_for_action,
    validate_project_action,
    workspace_roles_for_action,
)
from .project_permissions import (
    WORKSPACE_ROLES as _WORKSPACE_ROLES,
)

# Compatibility exports for existing consumers.
READABLE_PROJECT_ROLES = PROJECT_ROLES
WORKSPACE_ROLES = _WORKSPACE_ROLES


def shared_project_source_clause() -> ColumnElement[bool]:
    """Project-only viewing stops while the retained source is in the trash."""
    return exists(
        select(SessionRecord.id).where(
            SessionRecord.id == Project.source_session_id,
            SessionRecord.project_id == Project.id,
            SessionRecord.trashed_at.is_(None),
        ).correlate(Project)
    )


def workspace_authority(
    user_id: str,
    *,
    action: ProjectAction = ProjectAction.READ,
) -> ColumnElement[bool]:
    """Return action-specific authority from an active workspace membership."""
    validated_action = validate_project_action(action)
    allowed_workspace_roles = workspace_roles_for_action(validated_action)
    team_authority = (
        exists(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == Workspace.id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role.in_(allowed_workspace_roles),
            )
        )
        if allowed_workspace_roles
        else false()
    )
    personal_authority = exists(
        select(WorkspaceMembership.user_id).where(
            WorkspaceMembership.workspace_id == Workspace.id,
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.role == "owner",
        )
    )
    return or_(
        and_(Workspace.kind == "team", team_authority),
        and_(
            Workspace.kind == "personal",
            Workspace.personal_owner_user_id == user_id,
            personal_authority,
        ),
    )


def project_ids_for_user(
    user_id: str,
    *,
    action: ProjectAction = ProjectAction.READ,
) -> Select[tuple[str]]:
    """Select action-visible roots; this alone is not credential authorization."""
    validated_action = validate_project_action(action)
    project_roles = project_roles_for_action(validated_action)
    project_membership = (
        exists(
            select(ProjectMembership.user_id).where(
                ProjectMembership.project_id == Project.id,
                ProjectMembership.user_id == user_id,
                ProjectMembership.role.in_(project_roles),
                shared_project_source_clause(),
            )
        )
        if project_roles
        else false()
    )
    if validated_action in GOVERNANCE_ACTIONS:
        workspace_member = exists(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == Workspace.id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == "member",
            )
        )
        project_membership = and_(project_membership, not_(workspace_member))
    return (
        select(Project.id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .where(
            Workspace.state == "active",
            or_(
                workspace_authority(user_id, action=validated_action),
                project_membership,
            ),
        )
    )


def creatable_workspace_ids_for_user(user_id: str) -> Select[tuple[str]]:
    """Select active workspace-level role authority without credential scopes."""
    return (
        select(Workspace.id)
        .join(
            WorkspaceMembership,
            and_(
                WorkspaceMembership.workspace_id == Workspace.id,
                WorkspaceMembership.user_id == user_id,
            ),
        )
        .where(
            Workspace.state == "active",
            or_(
                and_(
                    Workspace.kind == "team",
                    WorkspaceMembership.role.in_(WORKSPACE_ROLES),
                ),
                and_(
                    Workspace.kind == "personal",
                    Workspace.personal_owner_user_id == user_id,
                    WorkspaceMembership.role == "owner",
                ),
            ),
        )
    )


__all__ = [
    "READABLE_PROJECT_ROLES",
    "WORKSPACE_ROLES",
    "creatable_workspace_ids_for_user",
    "project_ids_for_user",
    "shared_project_source_clause",
    "workspace_authority",
]
