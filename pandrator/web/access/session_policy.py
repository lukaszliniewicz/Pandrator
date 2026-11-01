"""Resolve stored source and language sessions through their project policy."""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..models import SessionRecord
from .contracts import AccessContext, ProjectAccess, ProjectAction, ProjectNotFound
from .project_policy import require_project_access, visible_project_ids_statement


class SessionNotFound(KeyError):
    """An unknown, invisible or mismatched session has the same safe result."""

    def __init__(self) -> None:
        super().__init__("Session not found.")


def visible_session_ids_statement(
    session: Session,
    context: AccessContext,
    action: ProjectAction = ProjectAction.READ,
) -> Select[tuple[str]]:
    """Filter in SQL before pagination; execute in the same caller transaction.

    Source and language sessions inherit one project grant. Lifecycle filters
    remain the consuming domain's responsibility. This statement is not a grant
    that can be reused across snapshots or write transactions.
    """
    projects = visible_project_ids_statement(session, context, action)
    return select(SessionRecord.id).where(SessionRecord.project_id.in_(projects))


def require_session_access(
    session: Session,
    context: AccessContext,
    session_id: str,
    action: ProjectAction = ProjectAction.READ,
    *,
    expected_project_id: str | None = None,
) -> ProjectAccess:
    """Resolve the actual root and current authority inside the caller's unit.

    A nested parent's ID is an additional consistency check, never proof of
    membership. An unauthorized action on a readable session remains distinct
    from an invisible session. This function neither flushes nor owns a session.
    """
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(action) is not ProjectAction:
        raise TypeError("A valid project action is required.")
    # Resolve only the stored ownership pointer before loading protected data.
    # Project policy performs one fresh actor/resource decision in this unit.
    with session.no_autoflush:
        project_id = session.scalar(
            select(SessionRecord.project_id).where(SessionRecord.id == session_id)
        )
        if project_id is None or (
            expected_project_id is not None and expected_project_id != project_id
        ):
            raise SessionNotFound()
        try:
            return require_project_access(session, context, project_id, action)
        except ProjectNotFound:
            raise SessionNotFound() from None


__all__ = [
    "SessionNotFound",
    "require_session_access",
    "visible_session_ids_statement",
]
