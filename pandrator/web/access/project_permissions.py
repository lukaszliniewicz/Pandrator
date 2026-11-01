"""Canonical project and workspace role, action, and scope permissions."""

from __future__ import annotations

from typing import Final, cast

from .contracts import ProjectAction, ProjectRole, WorkspaceRole

PROJECT_ROLES: Final[tuple[ProjectRole, ...]] = ("manager", "editor", "reviewer", "viewer")
WORKSPACE_ROLES: Final[tuple[WorkspaceRole, ...]] = ("owner", "admin", "member")

CONTENT_ACTIONS: Final[frozenset[ProjectAction]] = frozenset(
    {
        ProjectAction.READ,
        ProjectAction.REVIEW,
        ProjectAction.EDIT,
        ProjectAction.RUN,
        ProjectAction.CANCEL_OWN,
        ProjectAction.CANCEL_ANY,
        ProjectAction.TRASH,
        ProjectAction.RESTORE,
    }
)
GOVERNANCE_ACTIONS: Final[frozenset[ProjectAction]] = frozenset(
    {ProjectAction.MANAGE_MEMBERS, ProjectAction.PURGE}
)

# Project grants are viewing exceptions. Keep old stored names readable while
# removing the parallel editing hierarchy; collaboration belongs to workspaces.
PROJECT_ROLE_ACTIONS: Final[dict[ProjectRole, frozenset[ProjectAction]]] = {
    role: frozenset({ProjectAction.READ}) for role in PROJECT_ROLES
}

WORKSPACE_ROLE_ACTIONS: Final[dict[WorkspaceRole, frozenset[ProjectAction]]] = {
    "owner": frozenset(ProjectAction),
    "admin": frozenset(ProjectAction),
    "member": CONTENT_ACTIONS,
}

ACTION_SCOPES: Final[dict[ProjectAction, frozenset[str]]] = {
    ProjectAction.READ: frozenset({"app.read"}),
    ProjectAction.REVIEW: frozenset({"app.read", "app.write"}),
    ProjectAction.EDIT: frozenset({"app.read", "app.write"}),
    ProjectAction.RUN: frozenset({"app.read", "app.run"}),
    ProjectAction.CANCEL_OWN: frozenset({"app.read", "app.cancel"}),
    ProjectAction.CANCEL_ANY: frozenset({"app.read", "app.cancel"}),
    ProjectAction.MANAGE_MEMBERS: frozenset({"app.read", "app.write"}),
    ProjectAction.TRASH: frozenset({"app.read", "app.write"}),
    ProjectAction.RESTORE: frozenset({"app.read", "app.write"}),
    ProjectAction.PURGE: frozenset({"app.read", "app.write"}),
}


def validate_project_action(action: object) -> ProjectAction:
    """Require an actual ProjectAction enum member, not a string lookalike."""
    if type(action) is not ProjectAction:
        raise TypeError("A valid project action is required.")
    return cast(ProjectAction, action)


def project_actions_for_role(role: object) -> frozenset[ProjectAction]:
    if type(role) is not str or role not in PROJECT_ROLE_ACTIONS:
        raise TypeError("A valid project role is required.")
    return PROJECT_ROLE_ACTIONS[cast(ProjectRole, role)]


def workspace_actions_for_role(role: object) -> frozenset[ProjectAction]:
    if type(role) is not str or role not in WORKSPACE_ROLE_ACTIONS:
        raise TypeError("A valid workspace role is required.")
    return WORKSPACE_ROLE_ACTIONS[cast(WorkspaceRole, role)]


def project_roles_for_action(action: object) -> tuple[ProjectRole, ...]:
    validated_action = validate_project_action(action)
    return tuple(role for role in PROJECT_ROLES if validated_action in PROJECT_ROLE_ACTIONS[role])


def workspace_roles_for_action(action: object) -> tuple[WorkspaceRole, ...]:
    validated_action = validate_project_action(action)
    return tuple(
        role for role in WORKSPACE_ROLES if validated_action in WORKSPACE_ROLE_ACTIONS[role]
    )


__all__ = [
    "ACTION_SCOPES",
    "CONTENT_ACTIONS",
    "GOVERNANCE_ACTIONS",
    "PROJECT_ROLE_ACTIONS",
    "PROJECT_ROLES",
    "WORKSPACE_ROLE_ACTIONS",
    "WORKSPACE_ROLES",
    "project_actions_for_role",
    "project_roles_for_action",
    "validate_project_action",
    "workspace_actions_for_role",
    "workspace_roles_for_action",
]
