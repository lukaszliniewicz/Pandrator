"""Project viewing command errors and the transactional audit port."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from ..access.contracts import AccessContext


class ProjectSharingError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


_ERRORS = {
    "project_revision_conflict": ("The project changed. Refresh it and try again.", 409),
    "invalid_revision": ("A positive project revision is required.", 400),
    "user_not_available": ("User not found.", 404),
    "project_unavailable": ("Restore the project before sharing it.", 409),
    "already_workspace_member": ("This person already has workspace access.", 409),
    "validation_error": ("The project sharing request is invalid.", 422),
}


def project_sharing_error(
    code: str, *, message: str | None = None
) -> ProjectSharingError:
    default_message, status_code = _ERRORS[code]
    return ProjectSharingError(code, message or default_message, status_code)


class ProjectSharingAuditSink(Protocol):
    def __call__(
        self,
        session: Session,
        context: AccessContext,
        *,
        action: str,
        project_id: str,
        metadata: dict[str, str | int],
    ) -> None: ...


__all__ = ["ProjectSharingAuditSink", "ProjectSharingError", "project_sharing_error"]
