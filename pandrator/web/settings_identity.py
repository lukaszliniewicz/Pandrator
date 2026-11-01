"""Settings namespaces; selecting an owner does not establish actor permission."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .job_identity import job_creation_ownership
from .models import WorkspaceSetting

HOST_SETTING_KEYS = frozenset({"system.identity", "session.trash_policy", "web.preferences"})


def settings_workspace_id(
    session: Session,
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Resolve current typed roots, without accepting an owner in settings JSON."""
    with session.no_autoflush:
        return job_creation_ownership(
            session, session_id=session_id, workspace_id=workspace_id
        ).workspace_id


def workspace_setting_in_workspace(
    session: Session, workspace_id: str, key: str
) -> WorkspaceSetting | None:
    """Read a key within an enclosing operation's already validated namespace."""
    with session.no_autoflush:
        return session.scalar(
            select(WorkspaceSetting)
            .where(
                WorkspaceSetting.workspace_id == workspace_id,
                WorkspaceSetting.key == key,
            )
            .execution_options(populate_existing=True)
        )


__all__ = [
    "HOST_SETTING_KEYS",
    "settings_workspace_id",
    "workspace_setting_in_workspace",
]
