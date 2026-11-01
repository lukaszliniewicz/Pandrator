"""Finite current authority for selected-take comparison exports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import Artifact, Job, JobActorRecord, SessionRecord
from ..projects.models import Project
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access

COMPARISON_AUDIO_FIELDS = (
    Artifact.id,
    Artifact.workspace_id,
    Artifact.project_id,
    Artifact.session_id,
    Artifact.relative_path,
    Artifact.content_hash,
    Artifact.state,
    Artifact.size_bytes,
    Artifact.kind,
)


@dataclass(frozen=True, slots=True)
class ComparisonAudioHeader:
    id: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    relative_path: str
    content_hash: str | None
    state: str
    size_bytes: int | None
    kind: str


@dataclass(frozen=True, slots=True)
class ComparisonExportAccess:
    actor: UserActor | HostActor
    lease: tuple[str, str, int] | None
    session_id: str
    project_id: str
    workspace_id: str
    storage_key: str
    sources: tuple[ComparisonAudioHeader, ...]


def _lease_values(value: Any) -> tuple[str, str, int] | None:
    if value is None:
        return None
    if (
        type(value) is not dict
        or value.keys() != {"job_id", "owner_id", "lease_generation"}
        or not all(
            type(value.get(key)) is str and value[key] and value[key] == value[key].strip()
            for key in ("job_id", "owner_id")
        )
        or type(value.get("lease_generation")) is not int
        or value["lease_generation"] <= 0
    ):
        raise AccessDenied()
    return value["job_id"], value["owner_id"], value["lease_generation"]


def require_comparison_export_access(
    unit: Session,
    session_id: str,
    *,
    payload: dict[str, Any] | None = None,
    expected: ComparisonExportAccess | None = None,
) -> ComparisonExportAccess | None:
    actor = current_execution_actor.get()
    lease = _lease_values(current_job_lease.get())
    if expected is not None and (actor != expected.actor or lease != expected.lease):
        raise AccessDenied()
    if actor is None:
        if lease is not None:
            with unit.no_autoflush:
                captured = unit.scalar(
                    select(JobActorRecord.job_id).where(JobActorRecord.job_id == lease[0])
                )
            if captured is not None:
                raise AccessDenied()
        return None
    if type(actor) is HostActor and lease is None:
        require_current_session_access(unit, session_id, ProjectAction.RUN)
        return None
    if type(actor) not in (UserActor, HostActor):
        raise AccessDenied()
    if payload is not None:
        if lease is None:
            if payload.get("_job_id") or payload.get("_lease_generation"):
                raise AccessDenied()
        elif (
            payload.get("_job_id") != lease[0]
            or type(payload.get("_lease_generation")) is not int
            or payload.get("_lease_generation") != lease[2]
        ):
            raise AccessDenied()
    require_current_session_access(unit, session_id, ProjectAction.RUN)
    root = aliased(SessionRecord)
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
                SessionRecord.status != "purging",
                Workspace.state == "active",
                exists(
                    select(root.id).where(
                        root.id == Project.source_session_id,
                        root.project_id == Project.id,
                        root.trashed_at.is_(None),
                        root.status != "purging",
                    )
                ),
            )
        ).one_or_none()
        if target is None:
            raise AccessDenied()
        if lease is not None:
            identifier = unit.scalar(
                select(Job.id).where(
                    Job.id == lease[0],
                    Job.kind == "export.comparison",
                    Job.session_id == target.id,
                    Job.project_id == target.project_id,
                    Job.workspace_id == target.workspace_id,
                )
            )
            if identifier is None:
                raise AccessDenied()
        sources: tuple[ComparisonAudioHeader, ...] = ()
        if payload is not None:
            clips = payload.get("clips")
            if (
                not isinstance(clips, list)
                or not 1 <= len(clips) <= 100
                or any(
                    not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str)
                    for item in clips
                )
            ):
                raise AccessDenied()
            ids = sorted({item["artifact_id"] for item in clips})
            sources = tuple(
                ComparisonAudioHeader(*row)
                for row in unit.execute(
                    select(*COMPARISON_AUDIO_FIELDS)
                    .where(
                        Artifact.id.in_(ids),
                        Artifact.session_id == target.id,
                        Artifact.project_id == target.project_id,
                        Artifact.workspace_id == target.workspace_id,
                        Artifact.kind == "audio",
                        Artifact.state == "current",
                    )
                    .order_by(Artifact.id)
                )
            )
            by_id = {row.id: row for row in sources}
            if len(by_id) != len(ids) or any(
                item["artifact_id"] not in by_id
                or by_id[item["artifact_id"]].relative_path != item.get("artifact_relative_path")
                or by_id[item["artifact_id"]].content_hash != item.get("artifact_content_hash")
                for item in clips
            ):
                raise AccessDenied()
    proof = ComparisonExportAccess(
        actor, lease, target.id, target.project_id, target.workspace_id, target.storage_key, sources
    )
    if expected is not None and proof != expected:
        raise AccessDenied()
    return proof
