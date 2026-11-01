"""Workspace command failures and the caller-supplied transactional audit port."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from ..access.contracts import AccessContext


class WorkspaceCommandError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


_ERRORS = {
    "invalid_workspace_name": ("Workspace names must contain between 1 and 255 characters.", 400),
    "invalid_workspace_role": ("Workspace role must be manager or member.", 400),
    "invalid_revision": ("A positive workspace revision is required.", 400),
    "workspace_revision_conflict": ("The workspace changed. Refresh it and try again.", 409),
    "personal_workspace_protected": (
        "Personal workspaces cannot be shared, archived, or left.",
        409,
    ),
    "last_workspace_manager": ("Add another active Manager before removing the last Manager.", 409),
    "workspace_manager_recovery_not_needed": (
        "This workspace still has an active Manager.",
        409,
    ),
    "workspace_manager_recovery_target": (
        "Choose an active Member of this workspace.",
        409,
    ),
    "user_not_available": ("User not found.", 404),
    "invitation_not_found": ("Invitation not found.", 404),
    "invitation_unavailable": ("This invitation is no longer available.", 409),
}


def workspace_error(code: str) -> WorkspaceCommandError:
    message, status = _ERRORS[code]
    return WorkspaceCommandError(code, message, status)


class WorkspaceAuditSink(Protocol):
    def __call__(
        self,
        session: Session,
        context: AccessContext,
        *,
        action: str,
        workspace_id: str,
        metadata: dict[str, str | int | bool | None],
    ) -> None: ...
