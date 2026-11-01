"""Workspace authorization and bulk input checks for project folders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ProjectFolder, SessionRecord
from ..projects.models import Project
from .contracts import AccessContext
from .workspace_policy import WorkspaceAction, require_workspace_access


class FolderNotFound(KeyError):
    def __init__(self) -> None:
        super().__init__("Folder or session not found.")


@dataclass(frozen=True, slots=True)
class FolderWorkspace:
    id: str
    kind: str


def require_folder_workspace(
    session: Session,
    context: AccessContext,
    workspace_id: str | None = None,
    *,
    write: bool = False,
) -> FolderWorkspace:
    """Resolve one current content scope before metadata, writes or receipts."""
    access = require_workspace_access(
        session, context, workspace_id,
        action=WorkspaceAction.EDIT if write else WorkspaceAction.READ,
    )
    return FolderWorkspace(access.id, access.kind)


def require_folder_inputs(
    session: Session,
    workspace_id: str,
    *,
    folder_ids: Iterable[str] = (),
    session_ids: Iterable[str] = (),
    existing_folder_ids: Iterable[str] = (),
    allow_missing: bool = False,
) -> None:
    """Check identifiers in bulk; never load foreign names or session JSON.

    Completed commands may outlive deleted targets or dependencies. Preflight
    may leave missing IDs to the command/scoped receipt, while existing foreign
    IDs always fail. Legacy unscoped receipts require every retained input.
    """
    required_folders = set(folder_ids)
    existing_folders = set(existing_folder_ids)
    all_folders = required_folders | existing_folders
    with session.no_autoflush:
        if all_folders:
            rows = dict(
                session.execute(
                    select(ProjectFolder.id, ProjectFolder.workspace_id).where(
                        ProjectFolder.id.in_(all_folders)
                    )
                )
                .tuples()
                .all()
            )
            if (not allow_missing and not required_folders <= rows.keys()) or any(
                owner != workspace_id for owner in rows.values()
            ):
                raise FolderNotFound()
        requested_sessions = set(session_ids)
        if requested_sessions:
            owners = dict(
                session.execute(
                    select(SessionRecord.id, Project.workspace_id)
                    .join(Project, Project.id == SessionRecord.project_id)
                    .where(SessionRecord.id.in_(requested_sessions))
                )
                .tuples()
                .all()
            )
            if (not allow_missing and owners.keys() != requested_sessions) or any(
                owner != workspace_id for owner in owners.values()
            ):
                raise FolderNotFound()


def require_folder_replay_scope(
    session: Session, result: Mapping[str, Any], workspace_id: str
) -> None:
    """Validate legacy unscoped receipts; new receipts carry their owning root."""
    recorded_scope = result.get("workspace_id")
    if recorded_scope is not None:
        if recorded_scope != workspace_id:
            raise FolderNotFound()
        return
    # Old responses had no durable workspace marker. Do not disclose a former
    # global snapshot unless every retained resource still proves this scope.
    items = result.get("items")
    memberships = result.get("memberships")
    if not isinstance(items, list) or not isinstance(memberships, list):
        raise FolderNotFound()
    folders: set[str] = set()
    sessions: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise FolderNotFound()
        folders.add(item["id"])
    folder = result.get("folder")
    if folder is not None:
        if not isinstance(folder, dict) or not isinstance(folder.get("id"), str):
            raise FolderNotFound()
        folders.add(folder["id"])
    for member in memberships:
        if (
            not isinstance(member, dict)
            or not isinstance(member.get("folder_id"), str)
            or not isinstance(member.get("project_id"), str)
            or member["folder_id"] not in folders
        ):
            raise FolderNotFound()
        sessions.add(member["project_id"])
    require_folder_inputs(session, workspace_id, folder_ids=folders, session_ids=sessions)
