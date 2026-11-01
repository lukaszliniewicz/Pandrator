"""Transaction-scoped session reads protected by project access policy."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import SessionRecord
from ..projects.models import Project
from .contracts import AccessContext
from .session_policy import (
    SessionNotFound,
    require_session_access,
    visible_session_ids_statement,
)


def _validate_pagination(limit: int | None, offset: int) -> None:
    if limit is not None and type(limit) is not int:
        raise TypeError("limit must be an integer or None.")
    if type(offset) is not int:
        raise TypeError("offset must be an integer.")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative.")
    if offset < 0:
        raise ValueError("offset must be non-negative.")


def list_session_records(
    session: Session,
    context: AccessContext,
    *,
    include_trashed: bool = False,
    query: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    workspace_id: str | None = None,
) -> list[SessionRecord]:
    """List sessions visible to ``context`` in this caller-owned transaction."""
    _validate_pagination(limit, offset)
    term = query.strip() if query is not None else ""

    with session.no_autoflush:
        visible_ids = visible_session_ids_statement(session, context)
        statement = select(SessionRecord).where(SessionRecord.id.in_(visible_ids))
        if workspace_id is not None:
            statement = statement.join(Project, Project.id == SessionRecord.project_id).where(Project.workspace_id == workspace_id)
        if not include_trashed:
            statement = statement.where(SessionRecord.trashed_at.is_(None))
        if term:
            pattern = f"%{term}%"
            statement = statement.where(
                or_(
                    SessionRecord.name.ilike(pattern),
                    SessionRecord.id.ilike(pattern),
                    SessionRecord.source_language.ilike(pattern),
                    SessionRecord.target_language.ilike(pattern),
                )
            )
        statement = statement.order_by(SessionRecord.updated_at.desc(), SessionRecord.id.asc())
        if limit is not None:
            statement = statement.limit(limit)
        if offset:
            statement = statement.offset(offset)
        return list(session.scalars(statement).all())


def get_session_record(
    session: Session,
    context: AccessContext,
    session_id: str,
) -> SessionRecord:
    """Return an attached session row only after checking current project access."""
    with session.no_autoflush:
        require_session_access(session, context, session_id)
        record = session.get(SessionRecord, session_id)
        if record is None:
            raise SessionNotFound()
        return record


def find_session_by_name(
    session: Session,
    context: AccessContext,
    name: str,
    *,
    workspace_id: str,
) -> SessionRecord | None:
    """Find the newest visible active session with this exact display name."""
    normalized = str(name or "").strip().casefold()
    if not normalized:
        return None

    with session.no_autoflush:
        visible_ids = visible_session_ids_statement(session, context)
        records = session.scalars(
            select(SessionRecord)
            .join(Project, Project.id == SessionRecord.project_id)
            .where(
                Project.workspace_id == workspace_id,
                SessionRecord.id.in_(visible_ids),
                SessionRecord.trashed_at.is_(None),
            )
            .order_by(SessionRecord.updated_at.desc(), SessionRecord.id.asc())
        ).all()
    return next(
        (
            record
            for record in records
            if str(record.name or "").strip().casefold() == normalized
        ),
        None,
    )


__all__ = ["find_session_by_name", "get_session_record", "list_session_records"]
