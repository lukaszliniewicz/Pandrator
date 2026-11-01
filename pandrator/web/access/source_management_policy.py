"""Source-change admission in the caller's current or actual leased unit."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import current_execution_actor
from ..models import SessionRecord
from ..projects.models import Project
from .contracts import ProjectAction
from .current_session_policy import require_current_session_access
from .session_policy import SessionNotFound
from .source_library_policy import require_source_asset_access, require_source_library_workspace


def require_source_change_access(
    unit: Session,
    session_id: str,
    *,
    source_asset_id: str | None = None,
) -> None:
    require_current_session_access(unit, session_id, ProjectAction.EDIT)
    if source_asset_id is not None:
        require_source_asset_access(
            unit,
            source_asset_id,
            ProjectAction.READ,
            project_session_id=session_id,
        )


def require_source_session_creation_access(unit: Session, session_id: str) -> str | None:
    """After source EDIT proof, validate the target's workspace creation ceiling."""
    if current_execution_actor.get() is None:
        return None
    with unit.no_autoflush:
        workspace_id = unit.scalar(
            select(Project.workspace_id)
            .join(SessionRecord, SessionRecord.project_id == Project.id)
            .where(SessionRecord.id == session_id)
        )
    if workspace_id is None:
        raise SessionNotFound()
    return require_source_library_workspace(unit, ProjectAction.EDIT, workspace_id=workspace_id)
