"""Fresh session and input authority for narration text preparation."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, aliased

from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import AgentRun, Artifact, Job, SessionRecord
from ..projects.models import Project
from .artifact_visibility import artifact_roots_clause, resource_visibility_clause
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .delivery_execution_policy import DeliveryExecutionProof, require_delivery_execution
from .project_visibility import workspace_authority
from .source_dependency_visibility import current_source_dependency_clause

_SOURCE_PROCESSING_CHILD_KINDS = frozenset({"source.clean", "text.prepare", "text.optimize_tts"})
_SOURCE_PROCESSING_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})


@dataclass(frozen=True, slots=True)
class _SourceProcessingChildFrame:
    parent: DeliveryExecutionProof
    job_kind: str
    session_id: str
    source_artifact_id: str


_source_processing_child: ContextVar[_SourceProcessingChildFrame | None] = ContextVar(
    "pandrator_source_processing_child",
    default=None,
)


def _source_processing_identifier(value: object) -> TypeGuard[str]:
    return type(value) is str and bool(value) and value == value.strip()


def _source_processing_lease_values(lease: object) -> tuple[str, str, int]:
    if (
        type(lease) is not dict
        or lease.keys() != _SOURCE_PROCESSING_LEASE_KEYS
        or not _source_processing_identifier(lease.get("job_id"))
        or not _source_processing_identifier(lease.get("owner_id"))
        or type(lease.get("lease_generation")) is not int
        or lease["lease_generation"] <= 0
    ):
        raise AccessDenied()
    return lease["job_id"], lease["owner_id"], lease["lease_generation"]


@contextmanager
def source_processing_child_scope(
    parent: DeliveryExecutionProof,
    job_kind: str,
    payload: Mapping[str, Any],
) -> Iterator[None]:
    """Bind one inline source child to a captured workflow continuation proof."""
    if type(job_kind) is not str or job_kind not in _SOURCE_PROCESSING_CHILD_KINDS:
        raise AccessDenied()

    lease = current_job_lease.get()
    if current_execution_actor.get() is None or lease is None:
        yield
        return

    job_id, _owner_id, generation = _source_processing_lease_values(lease)
    if (
        type(parent) is not DeliveryExecutionProof
        or parent.job_kind != "workflow.continue"
        or parent.job_id != job_id
        or not _source_processing_identifier(parent.session_id)
        or not isinstance(payload, Mapping)
    ):
        raise AccessDenied()
    session_id = payload.get("session_id")
    source_artifact_id = payload.get("source_artifact_id")
    if (
        session_id != parent.session_id
        or not _source_processing_identifier(session_id)
        or not _source_processing_identifier(source_artifact_id)
        or not _source_processing_identifier(payload.get("_job_id"))
        or payload.get("_job_id") != job_id
        or type(payload.get("_lease_generation")) is not int
        or payload.get("_lease_generation") != generation
    ):
        raise AccessDenied()

    frame = _SourceProcessingChildFrame(
        parent=parent,
        job_kind=job_kind,
        session_id=session_id,
        source_artifact_id=source_artifact_id,
    )
    token = _source_processing_child.set(frame)
    try:
        yield
    finally:
        _source_processing_child.reset(token)


def _require_source_processing_access(
    unit: Session,
    session_id: str,
    source_artifact_id: str,
    *,
    job_kind: str,
    require_metadata: bool = False,
) -> bool:
    """Prove current preparation authority and return ordinary input visibility."""
    actor = current_execution_actor.get()
    if actor is None:
        return True

    lease = current_job_lease.get()
    frame = _source_processing_child.get()
    if frame is not None:
        if (
            lease is None
            or frame.job_kind != job_kind
            or frame.session_id != session_id
            or frame.source_artifact_id != source_artifact_id
        ):
            raise AccessDenied()
        job_id, _owner_id, _generation = _source_processing_lease_values(lease)
        if frame.parent.job_id != job_id:
            raise AccessDenied()
        require_delivery_execution(unit, frame.parent)
        bound_job_kind = "workflow.continue"
    else:
        require_current_session_access(unit, session_id, ProjectAction.RUN)
        bound_job_kind = job_kind

    if lease is not None:
        job_id, _owner_id, _generation = _source_processing_lease_values(lease)
        with unit.no_autoflush:
            job_id = unit.scalar(
                select(Job.id).where(
                    Job.id == job_id,
                    Job.kind == bound_job_kind,
                    Job.session_id == session_id,
                )
            )
        if job_id is None:
            raise AccessDenied()

    target_workspace_id = (
        select(Project.workspace_id)
        .join(SessionRecord, SessionRecord.project_id == Project.id)
        .where(SessionRecord.id == session_id)
        .scalar_subquery()
    )
    header_filters = (
        Artifact.id == source_artifact_id,
        Artifact.state != "deleted",
        Artifact.workspace_id == target_workspace_id,
    )

    if type(actor) is HostActor:
        with unit.no_autoflush:
            identifier = unit.scalar(select(Artifact.id).where(*header_filters))
        if identifier is None:
            raise KeyError("Source artifact not found.")
        return True
    if type(actor) is not UserActor:
        raise AccessDenied()

    ordinary_visibility = resource_visibility_clause(Artifact, actor)
    with unit.no_autoflush:
        row = unit.execute(
            select(
                Artifact.id,
                ordinary_visibility.label("ordinary_visibility"),
            ).where(
                *header_filters,
                or_(
                    ordinary_visibility,
                    current_source_dependency_clause(actor, session_id=session_id),
                ),
            )
        ).one_or_none()
    if row is None:
        raise KeyError("Source artifact not found.")
    ordinary_visible = bool(row.ordinary_visibility)
    if require_metadata and not ordinary_visible:
        raise KeyError("Source artifact not found.")
    return ordinary_visible


def require_text_preparation_access(
    unit: Session,
    session_id: str,
    source_artifact_id: str,
    *,
    require_metadata: bool = False,
) -> bool:
    """Prove current preparation authority and return ordinary input visibility."""
    return _require_source_processing_access(
        unit,
        session_id,
        source_artifact_id,
        job_kind="text.prepare",
        require_metadata=require_metadata,
    )


def require_source_cleaning_access(
    unit: Session,
    session_id: str,
    source_artifact_id: str,
    *,
    agent_run_id: str | None = None,
) -> None:
    """Prove current bytes authority and any native-cleaning run binding."""
    if current_execution_actor.get() is None:
        return
    _require_source_processing_access(
        unit,
        session_id,
        source_artifact_id,
        job_kind="source.clean",
    )
    if agent_run_id:
        lease = current_job_lease.get()
        with unit.no_autoflush:
            run_id = unit.scalar(
                select(AgentRun.id).where(
                    AgentRun.id == agent_run_id,
                    AgentRun.session_id == session_id,
                    AgentRun.source_artifact_id == source_artifact_id,
                    AgentRun.kind == "source_cleaning",
                    AgentRun.job_id == lease["job_id"]
                    if lease is not None
                    else AgentRun.job_id.is_(None),
                )
            )
        if run_id is None:
            raise KeyError("Source-cleaning run not found.")


__all__ = [
    "require_text_preparation_access",
    "require_source_cleaning_access",
    "source_processing_child_scope",
]


@dataclass(frozen=True, slots=True)
class SpeechOptimizationSource:
    id: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    relative_path: str
    content_hash: str | None
    size_bytes: int | None
    kind: str
    role: str
    mime_type: str | None
    state: str


@dataclass(frozen=True, slots=True)
class SpeechOptimizationInput:
    session_id: str
    project_id: str
    workspace_id: str
    storage_key: str
    source: SpeechOptimizationSource
    ordinary_visibility: bool = field(compare=False)
    actor: UserActor | HostActor
    lease: tuple[str, str, int] | None
    child: _SourceProcessingChildFrame | None


def require_speech_optimization_access(
    unit: Session, session_id: str, source_artifact_id: str, *,
    payload: Mapping[str, Any] | None = None,
    expected: SpeechOptimizationInput | None = None,
    agent_run_id: str | None = None,
    require_metadata: bool = False,
    require_workspace_pronunciations: bool = False,
) -> SpeechOptimizationInput | None:
    """Capture finite source headers while proving current execution authority."""
    actor = current_execution_actor.get()
    raw_lease = current_job_lease.get()
    lease = _source_processing_lease_values(raw_lease) if raw_lease is not None else None
    child = _source_processing_child.get()
    if expected is not None and (
        actor != expected.actor or lease != expected.lease or child != expected.child
    ):
        raise AccessDenied()
    if payload is not None:
        if lease is None:
            if "_job_id" in payload or "_lease_generation" in payload:
                raise AccessDenied()
        elif (
            payload.get("_job_id") != lease[0]
            or type(payload.get("_lease_generation")) is not int
            or payload.get("_lease_generation") != lease[2]
        ):
            raise AccessDenied()
    if actor is None:
        if lease is not None:
            raise AccessDenied()
        return None
    if type(actor) is HostActor and lease is None:
        require_current_session_access(unit, session_id, ProjectAction.RUN)
        return None
    if type(actor) not in (UserActor, HostActor):
        raise AccessDenied()
    ordinary = _require_source_processing_access(
        unit, session_id, source_artifact_id,
        job_kind="text.optimize_tts", require_metadata=require_metadata,
    )
    pronunciation_filters = (
        (workspace_authority(actor.user_id, action=ProjectAction.READ), credential_workspace_clause(actor))
        if require_workspace_pronunciations and type(actor) is UserActor else ()
    )
    root_session = aliased(SessionRecord)
    with unit.no_autoflush:
        target = unit.execute(
            select(SessionRecord.id, SessionRecord.storage_key,
                   Project.id.label("project_id"), Project.workspace_id)
            .join(Project, Project.id == SessionRecord.project_id)
            .join(Workspace, Workspace.id == Project.workspace_id)
            .where(SessionRecord.id == session_id, SessionRecord.trashed_at.is_(None),
                   SessionRecord.status != "purging", Workspace.state == "active",
                   *pronunciation_filters,
                   exists(select(root_session.id).where(
                       root_session.id == Project.source_session_id,
                       root_session.project_id == Project.id,
                   )))
        ).one_or_none()
        if target is None:
            raise AccessDenied()
        source = unit.execute(select(
            Artifact.id, Artifact.workspace_id, Artifact.project_id, Artifact.session_id,
            Artifact.relative_path, Artifact.content_hash, Artifact.size_bytes,
            Artifact.kind, Artifact.role, Artifact.mime_type, Artifact.state,
        ).where(Artifact.id == source_artifact_id, Artifact.workspace_id == target.workspace_id,
                Artifact.state != "deleted", artifact_roots_clause(Artifact))).one_or_none()
        if source is None:
            raise KeyError("Source artifact not found.")
        requested_run = str((payload or {}).get("_agent_run_id") or "") or None
        if lease is not None:
            bound_kind = "workflow.continue" if child is not None else "text.optimize_tts"
            bound_job = unit.scalar(select(Job.id).where(
                Job.id == lease[0], Job.kind == bound_kind,
                Job.session_id == target.id, Job.project_id == target.project_id,
                Job.workspace_id == target.workspace_id,
            ))
            if bound_job is None:
                raise AccessDenied()
            run_selector = (
                Job.payload_json["_agent_run_ids"]["tts_optimization"].as_string()
                if child is not None else Job.payload_json["_agent_run_id"].as_string()
            )
            selectors = unit.execute(select(
                Job.payload_json["session_id"].as_string().label("session_id"),
                Job.payload_json["source_artifact_id"].as_string().label("source_artifact_id"),
                run_selector.label("run_id"),
            ).where(Job.id == bound_job)).one()
            if selectors.session_id != session_id or (
                child is None and selectors.source_artifact_id != source_artifact_id
            ):
                raise AccessDenied()
            selected_run = str(selectors.run_id or "") or None
            if payload is not None and requested_run != selected_run:
                raise AccessDenied()
        for identifier in dict.fromkeys(value for value in (requested_run, agent_run_id) if value):
            filters = [AgentRun.id == identifier, AgentRun.kind == "tts_optimization",
                       AgentRun.session_id == session_id,
                       AgentRun.source_artifact_id == source_artifact_id]
            if identifier == agent_run_id:
                filters.append(AgentRun.job_id == lease[0] if lease else AgentRun.job_id.is_(None))
            if unit.scalar(select(AgentRun.id).where(*filters)) is None:
                raise AccessDenied()
    captured = SpeechOptimizationInput(
        target.id, target.project_id, target.workspace_id, target.storage_key,
        SpeechOptimizationSource(*source), ordinary, actor, lease, child,
    )
    if expected is not None and captured != expected:
        raise AccessDenied()
    return captured
