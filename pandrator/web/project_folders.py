"""Transactional project-folder persistence and canonical membership helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    ProjectFolder,
    ProjectFolderMembership,
    SessionPurge,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
    utcnow,
)
from .settings_policy import RevisionConflict


class FolderPromotionConflict(RuntimeError):
    """Deleting a folder would violate sibling-name uniqueness."""


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _folder_payload(folder: ProjectFolder) -> dict[str, Any]:
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "revision": folder.revision,
        "created_at": _timestamp(folder.created_at),
        "updated_at": _timestamp(folder.updated_at),
    }


def project_folder_snapshot(session: Session) -> dict[str, Any]:
    """Return every folder and membership in deterministic order."""
    folders = list(session.scalars(select(ProjectFolder)).all())
    folders.sort(
        key=lambda folder: (
            folder.parent_id or "",
            folder.name.casefold(),
            folder.id,
        )
    )
    memberships = list(session.scalars(select(ProjectFolderMembership)).all())
    memberships.sort(
        key=lambda member: (
            member.folder_id,
            member.position,
            member.project_session_id,
        )
    )
    return {
        "items": [_folder_payload(folder) for folder in folders],
        "memberships": [
            {
                "folder_id": member.folder_id,
                "project_id": member.project_session_id,
                "position": member.position,
            }
            for member in memberships
        ],
    }


def _normalized_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Folder name cannot be empty.")
    if len(normalized) > 128:
        raise ValueError("Folder name cannot exceed 128 characters.")
    return normalized


def _assert_unique_sibling_name(
    session: Session,
    name: str,
    parent_id: str | None,
    *,
    excluding_id: str | None = None,
) -> None:
    statement = select(ProjectFolder).where(
        ProjectFolder.parent_id.is_(None)
        if parent_id is None
        else ProjectFolder.parent_id == parent_id
    )
    for sibling in session.scalars(statement):
        if (
            sibling.id != excluding_id
            and sibling.name.casefold() == name.casefold()
        ):
            raise ValueError("A sibling folder already has this name.")


def _validate_parent(
    session: Session,
    parent_id: str | None,
    *,
    folder_id: str | None = None,
) -> None:
    if parent_id is None:
        return
    parent = session.get(ProjectFolder, parent_id)
    if parent is None:
        raise KeyError(parent_id)
    seen: set[str] = set()
    current: ProjectFolder | None = parent
    while current is not None:
        if current.id == folder_id:
            raise ValueError("A folder cannot be moved beneath itself or a descendant.")
        if current.id in seen:
            raise ValueError("The folder hierarchy already contains a cycle.")
        seen.add(current.id)
        current = (
            session.get(ProjectFolder, current.parent_id)
            if current.parent_id is not None
            else None
        )


def create_project_folder(
    session: Session, name: str, parent_id: str | None = None
) -> dict[str, Any]:
    """Create a folder inside the caller-owned write transaction."""
    normalized = _normalized_name(name)
    _validate_parent(session, parent_id)
    _assert_unique_sibling_name(session, normalized, parent_id)
    folder = ProjectFolder(name=normalized, parent_id=parent_id)
    session.add(folder)
    session.flush()
    return {**project_folder_snapshot(session), "folder": _folder_payload(folder)}


def update_project_folder(
    session: Session,
    folder_id: str,
    *,
    expected_revision: int,
    name: str | None = None,
    parent_id: str | None = None,
    change_name: bool = False,
    change_parent: bool = False,
) -> dict[str, Any]:
    """Apply only supplied fields after the caller acquires an immediate lock."""
    folder = session.get(ProjectFolder, folder_id)
    if folder is None:
        raise KeyError(folder_id)
    if folder.revision != expected_revision:
        raise RevisionConflict("The project folder changed in another client.")
    if not change_name and not change_parent:
        raise ValueError("Provide a folder name or parent_id to update.")

    next_name = _normalized_name(name) if change_name and name is not None else folder.name
    if change_name and name is None:
        raise ValueError("Folder name cannot be null.")
    next_parent = parent_id if change_parent else folder.parent_id
    _validate_parent(session, next_parent, folder_id=folder.id)
    _assert_unique_sibling_name(
        session, next_name, next_parent, excluding_id=folder.id
    )
    changed = next_name != folder.name or next_parent != folder.parent_id
    if changed:
        folder.name = next_name
        folder.parent_id = next_parent
        folder.revision += 1
        folder.updated_at = utcnow()
        session.flush()
    return {**project_folder_snapshot(session), "folder": _folder_payload(folder)}


def delete_project_folder(
    session: Session,
    folder_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    """Delete one folder and its memberships, promoting its direct children."""
    folder = session.get(ProjectFolder, folder_id)
    if folder is None:
        raise KeyError(folder_id)
    if folder.revision != expected_revision:
        raise RevisionConflict("The project folder changed in another client.")
    children = session.scalars(
        select(ProjectFolder).where(ProjectFolder.parent_id == folder.id)
    ).all()
    sibling_statement = select(ProjectFolder).where(
        ProjectFolder.parent_id.is_(None)
        if folder.parent_id is None
        else ProjectFolder.parent_id == folder.parent_id
    )
    names = {
        sibling.name.casefold(): sibling.name
        for sibling in session.scalars(sibling_statement)
        if sibling.id != folder.id
    }
    for child in children:
        normalized = child.name.casefold()
        if normalized in names:
            raise FolderPromotionConflict(
                f"Folder {child.name!r} conflicts with sibling {names[normalized]!r}; "
                "rename or move the child before deleting its parent."
            )
        names[normalized] = child.name
    for child in children:
        child.parent_id = folder.parent_id
        child.revision += 1
        child.updated_at = utcnow()
    session.execute(
        delete(ProjectFolderMembership).where(
            ProjectFolderMembership.folder_id == folder.id
        )
    )
    session.flush()
    session.delete(folder)
    session.flush()
    return {**project_folder_snapshot(session), "deleted_folder_id": folder_id}


def _canonical_project_ids(
    session: Session, session_ids: Iterable[str]
) -> list[str]:
    ordered_ids = list(dict.fromkeys(session_ids))
    if not ordered_ids:
        return []
    records = session.scalars(
        select(SessionRecord).where(SessionRecord.id.in_(ordered_ids))
    ).all()
    by_id = {record.id: record for record in records}
    for session_id in ordered_ids:
        if session_id not in by_id:
            raise KeyError(session_id)

    branch_rows = session.execute(
        select(TranslationProjectBranch.session_id, TranslationProjectBranch.project_id)
        .where(TranslationProjectBranch.session_id.in_(ordered_ids))
    ).all()
    project_ids = [row.project_id for row in branch_rows]
    project_sources = {
        project.id: project.source_session_id
        for project in session.scalars(
            select(TranslationProject).where(TranslationProject.id.in_(project_ids))
        ).all()
    }
    branch_sources = {
        row.session_id: project_sources[row.project_id]
        for row in branch_rows
        if row.project_id in project_sources
    }
    canonical_ids = [branch_sources.get(session_id, session_id) for session_id in ordered_ids]
    canonical_records = session.scalars(
        select(SessionRecord).where(SessionRecord.id.in_(canonical_ids))
    ).all()
    canonical_present = {record.id for record in canonical_records}
    for session_id in canonical_ids:
        if session_id not in canonical_present:
            raise KeyError(session_id)

    purge_states = dict(
        session.execute(
            select(SessionPurge.session_id, SessionPurge.state).where(
                SessionPurge.session_id.in_(set(ordered_ids) | set(canonical_ids))
            )
        ).all()
    )
    for session_id in ordered_ids + canonical_ids:
        if purge_states.get(session_id) in {"purging", "failed"}:
            raise ValueError("A session being purged cannot be assigned to a folder.")
    return list(dict.fromkeys(canonical_ids))


def replace_folder_projects(
    session: Session,
    folder_id: str,
    *,
    expected_revision: int,
    project_ids: list[str],
) -> dict[str, Any]:
    """Replace a folder's ordered canonical project memberships."""
    folder = session.get(ProjectFolder, folder_id)
    if folder is None:
        raise KeyError(folder_id)
    if folder.revision != expected_revision:
        raise RevisionConflict("The project folder changed in another client.")
    canonical_ids = _canonical_project_ids(session, project_ids)
    current = session.scalars(
        select(ProjectFolderMembership.project_session_id)
        .where(ProjectFolderMembership.folder_id == folder.id)
        .order_by(
            ProjectFolderMembership.position,
            ProjectFolderMembership.project_session_id,
        )
    ).all()
    if list(current) != canonical_ids:
        session.execute(
            delete(ProjectFolderMembership).where(
                ProjectFolderMembership.folder_id == folder.id
            )
        )
        session.add_all(
            ProjectFolderMembership(
                folder_id=folder.id,
                project_session_id=project_id,
                position=position,
            )
            for position, project_id in enumerate(canonical_ids)
        )
        folder.revision += 1
        folder.updated_at = utcnow()
        session.flush()
    return project_folder_snapshot(session)


def set_session_project_folders(
    session: Session,
    session_id: str,
    *,
    folder_ids: list[str],
    expected_revisions: Mapping[str, int],
) -> dict[str, Any]:
    """Atomically set one canonical project's folder memberships."""
    if len(folder_ids) != len(set(folder_ids)):
        raise ValueError("folder_ids cannot contain duplicates.")
    project_id = _canonical_project_ids(session, [session_id])[0]
    current_rows = session.scalars(
        select(ProjectFolderMembership)
        .where(ProjectFolderMembership.project_session_id == project_id)
        .order_by(ProjectFolderMembership.folder_id)
    ).all()
    current_ids = {row.folder_id for row in current_rows}
    requested_ids = set(folder_ids)
    union_ids = current_ids | requested_ids
    folders = {
        folder.id: folder
        for folder in session.scalars(
            select(ProjectFolder).where(ProjectFolder.id.in_(union_ids))
        ).all()
    } if union_ids else {}
    for folder_id in folder_ids:
        if folder_id not in folders:
            raise KeyError(folder_id)
    if set(expected_revisions) != union_ids:
        raise RevisionConflict(
            "Project folder memberships changed in another client. "
            "Reload saved memberships."
        )
    for folder_id in sorted(union_ids):
        folder = folders.get(folder_id)
        if folder is None:
            raise KeyError(folder_id)
        if folder_id not in expected_revisions:
            raise RevisionConflict(f"A revision fence is required for folder {folder_id}.")
        if folder.revision != expected_revisions[folder_id]:
            raise RevisionConflict("A project folder changed in another client.")

    changed_ids = current_ids ^ requested_ids
    if changed_ids:
        for folder_id in changed_ids:
            folder = folders[folder_id]
            rows = list(
                session.scalars(
                    select(ProjectFolderMembership)
                    .where(ProjectFolderMembership.folder_id == folder_id)
                    .order_by(
                        ProjectFolderMembership.position,
                        ProjectFolderMembership.project_session_id,
                    )
                ).all()
            )
            if folder_id in requested_ids:
                rows.append(
                    ProjectFolderMembership(
                        folder_id=folder_id,
                        project_session_id=project_id,
                        position=max((row.position for row in rows), default=-1) + 1,
                    )
                )
                session.add(rows[-1])
            else:
                session.delete(
                    next(
                        row for row in rows
                        if row.project_session_id == project_id
                    )
                )
                remaining = [
                    row for row in rows if row.project_session_id != project_id
                ]
                for position, row in enumerate(remaining):
                    row.position = position
            folder.revision += 1
            folder.updated_at = utcnow()
        session.flush()
    return {**project_folder_snapshot(session), "project_id": project_id}
