"""Finite current authority for soundtrack preview source and native dubbing."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import literal, or_, select
from sqlalchemy.orm import Session

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
from .session_policy import SessionNotFound
from .source_dependency_visibility import current_source_dependency_clause


@dataclass(frozen=True, slots=True)
class MixPreviewSource:
    id: str
    project_id: str | None
    session_id: str | None
    relative_path: str
    content_hash: str | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class MixPreviewInput:
    session_id: str
    project_id: str
    workspace_id: str
    storage_key: str
    source: MixPreviewSource
    dubbing: MixPreviewSource


def require_output_mix_access(
    unit: Session,
    session_id: str,
    source_artifact_id: str,
    dubbing_artifact_id: str,
    *,
    expected: MixPreviewInput | None = None,
) -> MixPreviewInput | None:
    """Prove current RUN, source bytes and target-owned ordinary dubbing READ."""
    actor = current_execution_actor.get()
    if actor is None:
        return None
    lease = current_job_lease.get()
    if type(actor) is HostActor and lease is None:
        try:
            require_execution_actor_in_session(
                unit,
                actor,
                required_scopes=ACTION_SCOPES[ProjectAction.RUN],
                allow_host=True,
            )
        except PermissionError:
            raise AccessDenied() from None
        return None
    if type(actor) not in (UserActor, HostActor):
        raise AccessDenied()
    if not session_id:
        raise SessionNotFound()
    require_current_session_access(unit, session_id, ProjectAction.RUN)
    with unit.no_autoflush:
        target = unit.execute(
            select(
                SessionRecord.id,
                SessionRecord.storage_key,
                Project.id.label("project_id"),
                Project.workspace_id,
            )
            .join(Project, Project.id == SessionRecord.project_id)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .where(
                SessionRecord.id == session_id,
                SessionRecord.trashed_at.is_(None),
                Workspace.state == "active",
            )
        ).one_or_none()
        if target is None:
            raise SessionNotFound()
        ordinary = (
            resource_visibility_clause(Artifact, actor)
            if type(actor) is UserActor
            else literal(True)
        )
        rows = {
            row.id: row
            for row in unit.execute(
                select(
                    Artifact.id,
                    Artifact.project_id,
                    Artifact.session_id,
                    Artifact.relative_path,
                    Artifact.content_hash,
                    Artifact.size_bytes,
                    ordinary.label("ordinary_visibility"),
                ).where(
                    Artifact.id.in_([source_artifact_id, dubbing_artifact_id]),
                    Artifact.state != "deleted",
                    Artifact.workspace_id == target.workspace_id,
                    or_(ordinary, current_source_dependency_clause(actor, session_id=session_id))
                    if type(actor) is UserActor
                    else literal(True),
                )
            )
        }
        source = rows.get(source_artifact_id)
        dubbing = rows.get(dubbing_artifact_id)
        if (
            source is None
            or dubbing is None
            or not dubbing.ordinary_visibility
            or dubbing.session_id != target.id
            or dubbing.project_id != target.project_id
        ):
            raise KeyError("Mix preview input not found.")
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
                        Job.kind == "output.mix_preview",
                        Job.session_id == target.id,
                        Job.project_id == target.project_id,
                        Job.workspace_id == target.workspace_id,
                        Job.payload_json["session_id"].as_string() == session_id,
                        Job.payload_json["source_artifact_id"].as_string() == source_artifact_id,
                        Job.payload_json["dubbing_artifact_id"].as_string() == dubbing_artifact_id,
                    )
                )
                is None
            ):
                raise AccessDenied()
        captured = MixPreviewInput(
            target.id,
            target.project_id,
            target.workspace_id,
            target.storage_key,
            MixPreviewSource(
                source.id,
                source.project_id,
                source.session_id,
                source.relative_path,
                source.content_hash,
                source.size_bytes,
            ),
            MixPreviewSource(
                dubbing.id,
                dubbing.project_id,
                dubbing.session_id,
                dubbing.relative_path,
                dubbing.content_hash,
                dubbing.size_bytes,
            ),
        )
        if expected is not None and captured != expected:
            raise KeyError("Mix preview input not found.")
        return captured


__all__ = ["MixPreviewInput", "MixPreviewSource", "require_output_mix_access"]
