"""Private pronunciation scope, separate from dictionary matching and actor policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .job_identity import job_creation_ownership
from .models import PronunciationEntry, SessionRecord


@dataclass(frozen=True)
class PronunciationOwnership:
    workspace_id: str
    project_id: str | None


def pronunciation_creation_ownership(
    session: Session,
    *,
    scope: str,
    session_id: str | None,
    workspace_id: str | None = None,
    trusted_session: bool = False,
    require_writable: bool = True,
) -> PronunciationOwnership:
    """Resolve an internal selection using current SQL rather than metadata.

    Manual library operations select a workspace explicitly or use the solo
    default. Trusted worker lookups/proposals can derive it from their session.
    Neither route establishes a caller's permission to access that workspace.
    """
    with session.no_autoflush:
        if scope == "global":
            if session_id is not None:
                raise ValueError("A workspace pronunciation cannot reference a session.")
            selected = job_creation_ownership(session, workspace_id=workspace_id)
            return PronunciationOwnership(selected.workspace_id, None)
        if scope != "session" or not isinstance(session_id, str) or not session_id:
            raise ValueError("A session pronunciation requires a valid session.")
        record = session.execute(
            select(SessionRecord.trashed_at, SessionRecord.status).where(
                SessionRecord.id == session_id
            )
        ).one_or_none()
        if record is None:
            raise KeyError(session_id)
        if require_writable and (record.trashed_at is not None or record.status == "purging"):
            raise ValueError("Pronunciations cannot be changed in a trashed or purging session.")
        selected_workspace_id = workspace_id
        if not trusted_session and selected_workspace_id is None:
            selected_workspace_id = job_creation_ownership(session).workspace_id
        owner = job_creation_ownership(
            session, session_id=session_id, workspace_id=selected_workspace_id
        )
        return PronunciationOwnership(owner.workspace_id, owner.project_id)


def pronunciation_ownership(
    session: Session,
    entry: PronunciationEntry,
    *,
    require_writable: bool = False,
) -> PronunciationOwnership:
    """Validate stored scope without adopting missing or retargeted ownership."""
    if not isinstance(entry.workspace_id, str) or not entry.workspace_id:
        raise ValueError("The pronunciation has no stored workspace ownership.")
    owner = pronunciation_creation_ownership(
        session,
        scope=entry.scope,
        session_id=entry.session_id,
        workspace_id=entry.workspace_id,
        trusted_session=True,
        require_writable=require_writable,
    )
    if (entry.workspace_id, entry.project_id) != (owner.workspace_id, owner.project_id):
        raise ValueError("The pronunciation's stored ownership disagrees with its session.")
    return owner


__all__ = [
    "PronunciationOwnership",
    "pronunciation_creation_ownership",
    "pronunciation_ownership",
]
