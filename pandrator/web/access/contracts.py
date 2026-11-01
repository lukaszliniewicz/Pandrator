"""Small immutable contracts shared by resource access policies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from ..accounts.execution_actor import UserActor


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Carry authenticated provenance into one policy check, never a cached grant."""

    actor: UserActor

    def __post_init__(self) -> None:
        if type(self.actor) is not UserActor:
            raise TypeError("Access context requires an authenticated user actor.")


class ProjectAction(str, Enum):
    READ = "read"
    REVIEW = "review"
    EDIT = "edit"
    RUN = "run"
    CANCEL_OWN = "cancel_own"
    CANCEL_ANY = "cancel_any"
    MANAGE_MEMBERS = "manage_members"
    TRASH = "trash"
    RESTORE = "restore"
    PURGE = "purge"


ProjectRole = Literal["manager", "editor", "reviewer", "viewer"]
WorkspaceRole = Literal["owner", "admin", "member"]


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """A read-only view of one project's authority in the current transaction."""

    project_id: str
    workspace_id: str
    workspace_role: WorkspaceRole | None
    project_role: ProjectRole | None
    allowed_actions: frozenset[ProjectAction]


class AccessDenied(PermissionError):
    """A visible resource or operation is outside the current authority."""

    def __init__(self) -> None:
        super().__init__("The requested action is not permitted.")


class ProjectNotFound(KeyError):
    """A project is absent or invisible to the current actor."""

    def __init__(self) -> None:
        super().__init__("Project not found.")


class WorkspaceNotFound(KeyError):
    """A workspace is absent or unavailable for the requested operation."""

    def __init__(self) -> None:
        super().__init__("Workspace not found.")


__all__ = [
    "AccessContext",
    "AccessDenied",
    "ProjectAccess",
    "ProjectAction",
    "ProjectNotFound",
    "ProjectRole",
    "WorkspaceNotFound",
    "WorkspaceRole",
]
