"""Transactional helpers for the initial personal project identity."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..accounts.identity import active_workspace, initial_personal_workspace
from ..accounts.models import Workspace
from ..model_base import utcnow
from ..models import Job, ResearchCacheEntry, SessionRecord, TranslationProject, UsageEvent
from .models import Project


def _validated_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id!r} does not exist.")
    workspace = session.get(Workspace, project.workspace_id)
    if workspace is None:
        raise ValueError(
            f"Project {project_id!r} references a missing workspace."
        )
    if workspace.state != "active":
        raise ValueError(
            f"Project {project_id!r} belongs to an inactive workspace."
        )
    if project.source_session_id is None:
        raise ValueError(f"Project {project_id!r} has no source session.")
    source = session.get(SessionRecord, project.source_session_id)
    if source is None or source.project_id != project.id:
        raise ValueError(
            f"Project {project_id!r} has an inconsistent source-session assignment."
        )
    return project


def create_session_project(
    session: Session,
    record: SessionRecord,
    *,
    project_id: str | None = None,
    workspace_id: str | None = None,
    legacy: bool = False,
) -> Project:
    """Insert a session with a new owned root or attach it to an existing root."""
    if project_id is not None:
        with session.no_autoflush:
            project = _validated_project(session, project_id)
            if workspace_id is not None:
                workspace = active_workspace(session, workspace_id, "Requested project workspace")
                if workspace.id != project.workspace_id:
                    raise ValueError("The requested workspace does not own this project.")
            if record.project_id is not None and record.project_id != project.id:
                raise ValueError(
                    "The session record is already assigned to another project."
                )
            existing = (
                session.get(SessionRecord, record.id) if record.id is not None else None
            )
            if existing is not None and existing is not record:
                if existing.project_id != project.id:
                    raise ValueError(
                        "The existing session record is not assigned to this project."
                    )
                return project

            record.project_id = project.id
            session.add(record)
            session.flush([record])
        return project

    if record.project_id is not None:
        raise ValueError(
            "Pass the existing project_id explicitly for an assigned session record."
        )
    if record in session.new:
        raise ValueError(
            "Call create_session_project before adding the session record to the session."
        )

    workspace = (
        initial_personal_workspace(session)
        if workspace_id is None
        else active_workspace(session, workspace_id, "Requested project workspace")
    )
    root = Project(
        workspace_id=workspace.id,
        source_session_id=None,
        name=record.name,
        created_by_user_id=None if legacy else workspace.personal_owner_user_id,
    )
    session.add(root)
    session.flush([root])

    record.project_id = root.id
    session.add(record)
    session.flush([record])
    if record.id is None:
        raise RuntimeError("The inserted session record has no identifier.")

    root.source_session_id = record.id
    session.flush([root])
    return root


def session_project(session: Session, session_id: str) -> Project:
    """Resolve one session's assigned project and validate the ownership links."""
    record = session.get(SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    if record.project_id is None:
        raise ValueError(f"Session {session_id!r} has no project assignment.")

    project = session.get(Project, record.project_id)
    if project is None:
        raise KeyError(record.project_id)
    workspace = session.get(Workspace, project.workspace_id)
    if workspace is None:
        raise ValueError(
            f"Project {project.id!r} references a missing workspace."
        )

    if project.source_session_id is None:
        raise ValueError(f"Project {project.id!r} has no source session.")
    source = session.get(SessionRecord, project.source_session_id)
    if source is None or source.project_id != project.id:
        raise ValueError(
            f"Project {project.id!r} has an inconsistent source-session assignment."
        )
    translation = session.scalar(
        select(TranslationProject).where(
            TranslationProject.source_session_id == source.id
        )
    )
    if (
        translation is not None
        and translation.project_root_id is not None
        and translation.project_root_id != project.id
    ):
        raise ValueError(
            f"Project {project.id!r} has an inconsistent translation-root assignment."
        )
    return project


def sync_project_name(session: Session, source_session_id: str) -> None:
    """Refresh the source-root name mirror without changing workflow metadata."""
    source = session.get(SessionRecord, source_session_id)
    if source is None:
        raise KeyError(source_session_id)
    if source.project_id is None:
        return

    project = session.get(Project, source.project_id)
    if project is None:
        raise ValueError(
            f"Session {source_session_id!r} references a missing project."
        )
    if project.source_session_id != source.id:
        return
    if source.project_id != project.id:
        raise ValueError(
            f"Source session {source_session_id!r} is assigned to another project."
        )

    translation = session.scalar(
        select(TranslationProject).where(
            TranslationProject.source_session_id == source.id
        )
    )
    if (
        translation is not None
        and translation.project_root_id is not None
        and translation.project_root_id != project.id
    ):
        raise ValueError(
            f"Translation project for source {source.id!r} references another root."
        )
    next_name = translation.name if translation is not None else source.name
    if project.name != next_name:
        project.name = next_name
        project.revision += 1
        project.updated_at = utcnow()


def delete_empty_project(session: Session, project_id: str | None) -> None:
    """Delete one root after pending session removals if no owner rows remain."""
    if project_id is None:
        return
    session.flush()
    project = session.get(Project, project_id)
    if project is None:
        return

    has_sessions = session.scalar(
        select(SessionRecord.id)
        .where(SessionRecord.project_id == project.id)
        .limit(1)
    )
    if has_sessions is not None:
        return
    has_translation = session.scalar(
        select(TranslationProject.id)
        .where(TranslationProject.project_root_id == project.id)
        .limit(1)
    )
    if has_translation is not None:
        return

    # Derived query data has no value after the last session is purged. Retained
    # job/usage receipts keep the minimal project root without retaining this cache.
    session.execute(delete(ResearchCacheEntry).where(ResearchCacheEntry.project_id == project.id))

    has_retained_work = session.scalar(
        select(Job.id).where(Job.project_id == project.id).limit(1)
    )
    if has_retained_work is not None:
        return
    has_usage = session.scalar(
        select(UsageEvent.id).where(UsageEvent.project_id == project.id).limit(1)
    )
    if has_usage is not None:
        return

    session.delete(project)
    session.flush([project])
