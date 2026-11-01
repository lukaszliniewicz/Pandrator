"""Persisted ownership and result consistency for private upload operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .accounts.models import Workspace
from .models import Artifact, SessionRecord, SourceAsset, UploadSessionRecord
from .projects.artifact_identity import resolve_artifact_ownership
from .projects.identity import session_project
from .projects.models import Project


@dataclass(frozen=True)
class UploadOwnership:
    workspace_id: str
    project_id: str | None


def upload_creation_ownership(
    session: Session,
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> UploadOwnership:
    """Resolve active construction context without granting caller access."""
    with session.no_autoflush:
        if session_id is not None and session.get(SessionRecord, session_id) is None:
            raise KeyError(session_id)
        owner = resolve_artifact_ownership(
            session, session_id=session_id, workspace_id=workspace_id
        )
        return UploadOwnership(owner.workspace_id, owner.project_id)


def upload_ownership(
    session: Session,
    record: UploadSessionRecord,
    *,
    workspace_id: str | None = None,
    allow_archived: bool = False,
) -> UploadOwnership:
    """Validate retained scope; archived scope is reserved for trusted cleanup.

    Session writability, actor policy and creation context are separate checks.
    A missing stored owner is never repaired using the solo creation default.
    """
    with session.no_autoflush:
        if not isinstance(record.workspace_id, str) or not record.workspace_id:
            raise ValueError("The upload has no stored workspace ownership.")
        workspace = session.get(Workspace, record.workspace_id)
        if workspace is None:
            raise ValueError("The upload references a missing workspace.")
        if workspace.state != "active" and not (allow_archived and workspace.state == "archived"):
            raise ValueError("The upload belongs to an inactive workspace.")
        if workspace_id is not None and workspace_id != workspace.id:
            raise ValueError("The upload belongs to another workspace.")

        project: Project | None = None
        if record.session_id is not None:
            project = session_project(session, record.session_id)
            if record.project_id is None or record.project_id != project.id:
                raise ValueError("The upload session and stored project ownership disagree.")
        elif record.project_id is not None:
            project = session.get(Project, record.project_id)
            if project is None or project.source_session_id is None:
                raise ValueError("The upload references a missing or incomplete project.")
            if session_project(session, project.source_session_id).id != project.id:
                raise ValueError("The upload project has an inconsistent source root.")
        if project is not None and project.workspace_id != workspace.id:
            raise ValueError("The upload project belongs to another workspace.")
        if type(record.attach_to_session) is not bool or (
            not record.attach_to_session
            and (record.session_id is None or record.project_id is None)
        ):
            raise ValueError("The upload attachment mode has no valid project session.")
        return UploadOwnership(workspace.id, record.project_id)


def _result_id(result: Mapping[str, Any], field: str) -> str | None:
    if field not in result:
        return None
    value = result[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"The stored upload result has an invalid {field}.")
    return value


def validate_upload_result(
    session: Session,
    record: UploadSessionRecord,
    *,
    workspace_id: str | None = None,
) -> None:
    """Check known result links before status/replay projection, without media I/O."""
    with session.no_autoflush:
        owner = upload_ownership(session, record, workspace_id=workspace_id)
        staged = not record.attach_to_session
        result = record.result_json
        if result is None:
            return
        if not isinstance(result, Mapping):
            raise ValueError("The stored upload result is not an object.")
        upload_id = _result_id(result, "upload_id")
        if upload_id is not None and upload_id != record.id:
            raise ValueError("The stored upload result references another upload.")
        artifact_id = _result_id(result, "artifact_id")
        source_asset_id = _result_id(result, "source_asset_id")
        if staged:
            if (
                artifact_id is None
                or source_asset_id is None
                or "attachment" not in result
                or result["attachment"] is not None
            ):
                raise ValueError(
                    "The staged upload result must retain its source and null attachment."
                )
        elif (
            record.session_id is not None
            and "attachment" in result
            and result["attachment"] is None
        ):
            raise ValueError("The attached upload result has no attachment.")

        def validate_artifact(identifier: str) -> Artifact:
            artifact = session.get(Artifact, identifier)
            if artifact is None:
                raise ValueError("The stored upload result artifact is missing.")
            if staged and artifact.session_id not in {record.session_id, None}:
                raise ValueError("The staged upload result artifact belongs to another session.")
            artifact_owner = resolve_artifact_ownership(session, artifact=artifact)
            if artifact_owner.workspace_id != owner.workspace_id:
                raise ValueError("The stored upload result artifact belongs to another workspace.")
            if owner.project_id is not None and artifact_owner.project_id != owner.project_id:
                raise ValueError("The stored upload result artifact belongs to another project.")
            return artifact

        if artifact_id is not None:
            validate_artifact(artifact_id)
        if source_asset_id is not None:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise ValueError("The stored upload result source is missing.")
            if asset.workspace_id != owner.workspace_id:
                raise ValueError("The stored upload result source belongs to another workspace.")
            if staged and asset.artifact_id != artifact_id:
                raise ValueError("The staged upload result source and artifact disagree.")
            if asset.artifact_id is not None:
                if artifact_id is not None and asset.artifact_id != artifact_id:
                    raise ValueError("The stored upload result source and artifact disagree.")
                validate_artifact(asset.artifact_id)


__all__ = [
    "UploadOwnership",
    "upload_creation_ownership",
    "upload_ownership",
    "validate_upload_result",
]
