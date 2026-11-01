"""Current workspace authority for importing a source bundle."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import literal, or_, select
from sqlalchemy.orm import Session

from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import Artifact, Job, SessionRecord
from ..projects.models import Project
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .project_permissions import ACTION_SCOPES
from .project_visibility import workspace_authority
from .session_policy import SessionNotFound
from .source_dependency_visibility import current_source_dependency_clause


def require_bundle_import_creation_access(
    unit: Session,
    source_artifact_id: str,
) -> str | None:
    """Return the current accessible source workspace without loading its body."""
    actor = current_execution_actor.get()
    if actor is None:
        return None
    if type(actor) not in (UserActor, HostActor):
        raise AccessDenied()

    with unit.no_autoflush:
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=(
                    ACTION_SCOPES[ProjectAction.EDIT] | ACTION_SCOPES[ProjectAction.RUN]
                ),
                allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None

        statement = (
            select(Artifact.workspace_id)
            .join(Workspace, Workspace.id == Artifact.workspace_id)
            .where(
                Artifact.id == source_artifact_id,
                Artifact.state != "deleted",
                Workspace.state == "active",
            )
        )
        if type(actor) is UserActor:
            statement = statement.where(
                resource_visibility_clause(
                    Artifact,
                    actor,
                    action=ProjectAction.READ,
                ),
                workspace_authority(actor.user_id, action=ProjectAction.EDIT),
                credential_workspace_clause(actor),
            )
        workspace_id = unit.scalar(statement)

    if workspace_id is None:
        raise KeyError("Bundle artifact not found.")
    return workspace_id


@dataclass(frozen=True, slots=True)
class BundleExportInput:
    artifact_id: str
    relative_path: str
    content_hash: str | None
    requires_metadata: bool


def require_bundle_export_access(
    unit: Session,
    session_id: str,
    *,
    inputs: tuple[BundleExportInput, ...] = (),
) -> None:
    """Prove current export authority and the captured source data lineage."""
    actor = current_execution_actor.get()
    if actor is None:
        return
    require_current_session_access(unit, session_id, ProjectAction.RUN)
    lease = current_job_lease.get()
    with unit.no_autoflush:
        target = (
            select(Project.workspace_id)
            .join(
                SessionRecord,
                SessionRecord.project_id == Project.id,
            )
            .join(Workspace, Workspace.id == Project.workspace_id)
            .where(
                SessionRecord.id == session_id,
            )
        )
        if type(actor) is UserActor or lease is not None:
            target = target.where(SessionRecord.trashed_at.is_(None), Workspace.state == "active")
        workspace_id = unit.scalar(target)
        if workspace_id is None:
            raise SessionNotFound()
        if lease is not None:
            if (
                type(lease) is not dict
                or lease.keys() != {"job_id", "owner_id", "lease_generation"}
                or any(
                    type(lease[key]) is not str or not lease[key] for key in ("job_id", "owner_id")
                )
                or type(lease["lease_generation"]) is not int
                or lease["lease_generation"] <= 0
            ):
                raise AccessDenied()
            if (
                unit.scalar(
                    select(Job.id).where(
                        Job.id == lease["job_id"],
                        Job.kind == "session.bundle.export",
                        Job.session_id == session_id,
                    )
                )
                is None
            ):
                raise AccessDenied()
        visibility = (
            resource_visibility_clause(Artifact, actor)
            if type(actor) is UserActor
            else literal(True)
        )
        for offset in range(0, len(inputs), 500):
            batch = inputs[offset : offset + 500]
            statement = select(
                Artifact.id,
                Artifact.relative_path,
                Artifact.content_hash,
                visibility.label("ordinary_visibility"),
            ).where(
                Artifact.id.in_([item.artifact_id for item in batch]),
                Artifact.state != "deleted",
                Artifact.workspace_id == workspace_id,
            )
            if type(actor) is UserActor:
                statement = statement.where(
                    or_(
                        visibility,
                        current_source_dependency_clause(actor, session_id=session_id),
                    )
                )
            rows = {row.id: row for row in unit.execute(statement)}
            for item in batch:
                row = rows.get(item.artifact_id)
                if (
                    row is None
                    or row.relative_path != item.relative_path
                    or row.content_hash != item.content_hash
                    or (item.requires_metadata and not row.ordinary_visibility)
                ):
                    raise KeyError("Source artifact not found.")


@dataclass(frozen=True, slots=True)
class BundleImportInput:
    artifact_id: str
    workspace_id: str
    relative_path: str
    content_hash: str | None


def require_bundle_import_execution_access(
    unit: Session,
    source_artifact_id: str,
    *,
    expected: BundleImportInput | None = None,
) -> BundleImportInput | None:
    """Prove current ordinary source/creation authority and actual import lease."""
    if current_execution_actor.get() is None:
        return None
    workspace_id = require_bundle_import_creation_access(unit, source_artifact_id)
    lease = current_job_lease.get()
    with unit.no_autoflush:
        if lease is not None:
            if (
                type(lease) is not dict
                or lease.keys() != {"job_id", "owner_id", "lease_generation"}
                or any(
                    type(lease[key]) is not str or not lease[key] for key in ("job_id", "owner_id")
                )
                or type(lease["lease_generation"]) is not int
                or lease["lease_generation"] <= 0
            ):
                raise AccessDenied()
            if (
                unit.scalar(
                    select(Job.id).where(
                        Job.id == lease["job_id"],
                        Job.kind == "session.bundle.import",
                        Job.workspace_id == workspace_id,
                        Job.session_id.is_(None),
                        Job.project_id.is_(None),
                        Job.payload_json["source_artifact_id"].as_string() == source_artifact_id,
                    )
                )
                is None
            ):
                raise AccessDenied()
        row = unit.execute(
            select(
                Artifact.id,
                Artifact.workspace_id,
                Artifact.relative_path,
                Artifact.content_hash,
            ).where(
                Artifact.id == source_artifact_id,
                Artifact.workspace_id == workspace_id,
                Artifact.state != "deleted",
            )
        ).one_or_none()
        if row is None:
            raise KeyError("Bundle artifact not found.")
        captured = BundleImportInput(row.id, row.workspace_id, row.relative_path, row.content_hash)
        if expected is not None and expected != captured:
            raise KeyError("Bundle artifact not found.")
        return captured


__all__ = [
    "BundleExportInput",
    "BundleImportInput",
    "require_bundle_export_access",
    "require_bundle_import_creation_access",
    "require_bundle_import_execution_access",
]
