"""Resolve translation aggregates through their stored, shared project root."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TranslationProject
from .contracts import AccessContext, ProjectAccess, ProjectAction, ProjectNotFound
from .project_policy import require_project_access


def require_translation_project_access(
    session: Session,
    context: AccessContext,
    translation_project_id: str,
    action: ProjectAction = ProjectAction.READ,
) -> ProjectAccess:
    """Check current authority before loading aggregate metadata or receipts.

    The database's project-graph constraints keep source and language sessions
    under this root. Resolve only the ownership pointer before checking access;
    neither the creator nor an instance Administrator receives an exception.
    """
    with session.no_autoflush:
        project_id = session.scalar(
            select(TranslationProject.project_root_id).where(
                TranslationProject.id == translation_project_id
            )
        )
        if project_id is None:
            raise ProjectNotFound()
        return require_project_access(session, context, project_id, action)
