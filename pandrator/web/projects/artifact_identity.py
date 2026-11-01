"""Persisted ownership checks for generated and library artifacts."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.identity import active_workspace as _active_workspace
from ..models import Artifact, SourceAsset
from .identity import initial_personal_workspace, session_project
from .models import Project


@dataclass(frozen=True)
class ArtifactOwnership:
    workspace_id: str
    project_id: str | None


def _session_ownership(session: Session, session_id: str) -> ArtifactOwnership:
    try:
        project = session_project(session, session_id)
    except KeyError as exc:
        raise ValueError(f"Session {session_id!r} or its project is missing.") from exc
    workspace = _active_workspace(session, project.workspace_id, f"Session {session_id!r} project")
    return ArtifactOwnership(workspace.id, project.id)


def _project_ownership(session: Session, project_id: str) -> ArtifactOwnership:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id!r} does not exist.")
    if project.source_session_id is None:
        raise ValueError(f"Project {project_id!r} has no source session.")
    root_ownership = _session_ownership(session, project.source_session_id)
    if root_ownership.project_id != project.id:
        raise ValueError(f"Project {project_id!r} has an inconsistent source-session assignment.")
    return root_ownership


def _requested_ownership(
    session: Session,
    *,
    session_id: str | None,
    project_id: str | None,
    workspace_id: str | None,
) -> ArtifactOwnership:
    by_session = _session_ownership(session, session_id) if session_id is not None else None
    by_project = _project_ownership(session, project_id) if project_id is not None else None

    if (
        by_session is not None
        and by_project is not None
        and by_session.project_id != by_project.project_id
    ):
        raise ValueError(
            f"Session {session_id!r} and project {project_id!r} have conflicting ownership."
        )

    owner = by_session or by_project
    if workspace_id is not None:
        requested_workspace = _active_workspace(
            session, workspace_id, "Requested artifact ownership"
        )
        if owner is not None and owner.workspace_id != requested_workspace.id:
            raise ValueError(
                f"Requested workspace {workspace_id!r} conflicts with the session or project."
            )
        if owner is None:
            owner = ArtifactOwnership(requested_workspace.id, None)

    if owner is None:
        workspace = initial_personal_workspace(session)
        return ArtifactOwnership(workspace.id, None)
    return owner


def _stored_artifact_ownership(session: Session, artifact: Artifact) -> ArtifactOwnership:
    if artifact.workspace_id is None:
        raise ValueError(f"Artifact {artifact.id!r} has no stored workspace ownership.")
    workspace = _active_workspace(
        session, artifact.workspace_id, f"Artifact {artifact.id!r} ownership"
    )

    session_owner = (
        _session_ownership(session, artifact.session_id)
        if artifact.session_id is not None
        else None
    )
    project_owner = (
        _project_ownership(session, artifact.project_id)
        if artifact.project_id is not None
        else None
    )

    if artifact.session_id is not None and artifact.project_id is None:
        raise ValueError(f"Session-owned artifact {artifact.id!r} has no stored project ownership.")
    if session_owner is not None and project_owner is not None:
        if session_owner.project_id != project_owner.project_id:
            raise ValueError(f"Artifact {artifact.id!r} session and project ownership disagree.")
    project_context = session_owner or project_owner
    if project_context is not None and project_context.workspace_id != workspace.id:
        raise ValueError(f"Artifact {artifact.id!r} project belongs to another workspace.")

    linked_assets = session.execute(
        select(SourceAsset.id, SourceAsset.workspace_id)
        .where(SourceAsset.artifact_id == artifact.id)
        .order_by(SourceAsset.id)
    ).all()
    for asset_id, asset_workspace_id in linked_assets:
        if asset_workspace_id is None:
            raise ValueError(f"Source asset {asset_id!r} has no workspace ownership.")
        asset_workspace = _active_workspace(
            session, asset_workspace_id, f"Source asset {asset_id!r} ownership"
        )
        if asset_workspace.id != workspace.id:
            raise ValueError(
                f"Source asset {asset_id!r} belongs to workspace "
                f"{asset_workspace.id!r}, not artifact workspace {workspace.id!r}."
            )

    return ArtifactOwnership(workspace.id, artifact.project_id)


def resolve_artifact_ownership(
    session: Session,
    *,
    artifact: Artifact | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    workspace_id: str | None = None,
) -> ArtifactOwnership:
    """Resolve and validate persisted artifact ownership without granting access."""
    with session.no_autoflush:
        if artifact is None:
            return _requested_ownership(
                session,
                session_id=session_id,
                project_id=project_id,
                workspace_id=workspace_id,
            )

        stored = _stored_artifact_ownership(session, artifact)
        if session_id is None and project_id is None and workspace_id is None:
            return stored

        requested = _requested_ownership(
            session,
            session_id=session_id,
            project_id=project_id,
            workspace_id=workspace_id,
        )
        if requested.workspace_id != stored.workspace_id:
            raise ValueError(
                f"Artifact {artifact.id!r} belongs to workspace {stored.workspace_id!r}, "
                f"not requested workspace {requested.workspace_id!r}."
            )
        if (session_id is not None or project_id is not None) and (
            requested.project_id != stored.project_id
        ):
            raise ValueError(
                f"Artifact {artifact.id!r} belongs to project {stored.project_id!r}, "
                f"not requested project {requested.project_id!r}."
            )
        return stored


__all__ = ["ArtifactOwnership", "resolve_artifact_ownership"]
