"""Read-only current-root, captured-actor and lease proof for audio assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, NamedTuple, NoReturn, TypeGuard

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_storage,
    current_execution_actor,
)
from ..accounts.models import Workspace
from ..credential_identity import credential_namespace
from ..job_execution import current_job_lease
from ..model_base import utcnow
from ..models import (
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    JobActorRecord,
    OutputAssembly,
    SessionRecord,
    WorkflowRun,
)
from ..projects.models import Project
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .session_policy import SessionNotFound

_INJECTED = frozenset({"_job_id", "_lease_generation"})
_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})
_BRIDGES = frozenset({"export.variant", "workflow.continue"})


@dataclass(frozen=True, slots=True)
class AssemblyExecutionProof:
    assembly_id: str
    session_id: str
    project_id: str
    workspace_id: str
    generation_run_id: str | None
    assembly_job_id: str | None
    settings_hash: str | None
    job_id: str | None
    job_kind: str | None


class _Root(NamedTuple):
    session_id: str
    project_id: str
    workspace_id: str


class _AssemblyHeader(NamedTuple):
    id: str
    session_id: str
    generation_run_id: str | None
    job_id: str | None
    settings_hash: str | None


class _JobHeader(NamedTuple):
    id: str
    kind: str
    session_id: str | None
    project_id: str | None
    workspace_id: str
    workflow_run_id: str | None
    status: str
    lease_owner: str | None
    lease_generation: int
    lease_expires_at: datetime | None


def _deny() -> NoReturn:
    raise AccessDenied() from None


def _identifier(value: object) -> TypeGuard[str]:
    return type(value) is str and bool(value) and value == value.strip()


def _root(session: Session, session_id: str) -> _Root:
    if not _identifier(session_id):
        _deny()
    row = session.execute(
        select(SessionRecord.id, Project.id, Workspace.id)
        .join(Project, Project.id == SessionRecord.project_id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .where(
            SessionRecord.id == session_id,
            SessionRecord.trashed_at.is_(None),
            Workspace.state == "active",
        )
    ).one_or_none()
    if row is None:
        _deny()
    if not all(_identifier(value) for value in row):
        _deny()
    return _Root(*row)


def _lease_values(lease: object) -> tuple[str, str, int]:
    if (
        type(lease) is not dict
        or lease.keys() != _LEASE_KEYS
        or not _identifier(lease.get("job_id"))
        or not _identifier(lease.get("owner_id"))
        or type(lease.get("lease_generation")) is not int
        or lease["lease_generation"] <= 0
    ):
        _deny()
    return lease["job_id"], lease["owner_id"], lease["lease_generation"]


def _leased_job(session: Session, root: _Root, lease: object) -> _JobHeader:
    job_id, owner_id, generation = _lease_values(lease)
    row = session.execute(
        select(
            Job.id,
            Job.kind,
            Job.session_id,
            Job.project_id,
            Job.workspace_id,
            Job.workflow_run_id,
            Job.status,
            Job.lease_owner,
            Job.lease_generation,
            Job.lease_expires_at,
        ).where(Job.id == job_id)
    ).one_or_none()
    if row is None:
        _deny()
    header = _JobHeader(*row)
    expires = header.lease_expires_at
    if type(expires) is not datetime:
        _deny()
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if (
        header.session_id != root.session_id
        or header.project_id != root.project_id
        or header.workspace_id != root.workspace_id
        or header.status != "running"
        or header.lease_owner != owner_id
        or type(header.lease_generation) is not int
        or header.lease_generation != generation
        or expires <= utcnow()
    ):
        _deny()
    if (
        header.workflow_run_id is not None
        and session.scalar(
            select(WorkflowRun.id).where(
                WorkflowRun.id == header.workflow_run_id,
                WorkflowRun.session_id == root.session_id,
            )
        )
        is None
    ):
        _deny()
    return header


def _authorize(
    session: Session, root: _Root, referenced_job_id: str | None = None
) -> _JobHeader | None:
    actor = current_execution_actor.get()
    lease = current_job_lease.get()
    if (type(actor) is UserActor and lease is None) or (
        actor is not None and type(actor) not in (UserActor, HostActor)
    ):
        _deny()
    if type(actor) is HostActor and (
        type(getattr(actor, "label", None)) is not str or actor.label != "pandrator-cli"
    ):
        _deny()
    header = _leased_job(session, root, lease) if lease is not None else None
    try:
        require_current_session_access(session, root.session_id, ProjectAction.RUN)
    except (PermissionError, SessionNotFound):
        _deny()
    if actor is None:
        try:
            legacy = credential_namespace(session, root.workspace_id).legacy_host_access
        except (KeyError, TypeError, ValueError, RuntimeError):
            _deny()
        if not legacy:
            _deny()
        if (
            header is not None
            and session.scalar(
                select(JobActorRecord.job_id).where(
                    JobActorRecord.job_id == header.id,
                )
            )
            is not None
        ):
            _deny()
    if lease is None and referenced_job_id is not None:
        record = session.execute(
            select(
                JobActorRecord.user_id,
                JobActorRecord.auth_revision,
                JobActorRecord.context_json,
            ).where(JobActorRecord.job_id == referenced_job_id)
        ).one_or_none()
        if record is not None:
            try:
                recorded_actor = actor_from_storage(*record)
            except (AttributeError, TypeError, ValueError):
                _deny()
            if recorded_actor is None or (actor is None and type(recorded_actor) is UserActor):
                _deny()
    return header


def _stored_payload(session: Session, header: _JobHeader) -> dict[str, Any]:
    try:
        stored = session.scalar(select(Job.payload_json).where(Job.id == header.id))
    except JSONDecodeError:
        _deny()
    if type(stored) is not dict:
        _deny()
    return stored


def _bridge_payload(session: Session, root: _Root, header: _JobHeader) -> None:
    if header.kind not in _BRIDGES:
        _deny()
    stored = _stored_payload(session, header)
    if not _identifier(stored.get("session_id")) or stored["session_id"] != root.session_id:
        _deny()


def require_export_assembly_job(session: Session, session_id: str) -> str | None:
    """Prove an export's current root before it inspects or creates an assembly."""
    with session.no_autoflush:
        root = _root(session, session_id)
        header = _authorize(session, root)
        if header is None:
            return None
        _bridge_payload(session, root, header)
        return header.id


def require_assembly_execution(
    session: Session,
    payload: Mapping[str, Any],
    *,
    expected: AssemblyExecutionProof | None = None,
) -> AssemblyExecutionProof:
    """Return current assembly execution proof without opening protected settings."""
    if not isinstance(payload, Mapping) or (
        expected is not None and type(expected) is not AssemblyExecutionProof
    ):
        _deny()
    assembly_id = payload.get("output_assembly_id")
    if not _identifier(assembly_id):
        _deny()
    lease = current_job_lease.get()
    if lease is None and payload.keys() & _INJECTED:
        _deny()
    with session.no_autoflush:
        row = session.execute(
            select(
                OutputAssembly.id,
                OutputAssembly.session_id,
                OutputAssembly.generation_run_id,
                OutputAssembly.job_id,
                OutputAssembly.settings_hash,
            ).where(OutputAssembly.id == assembly_id)
        ).one_or_none()
        if row is None:
            _deny()
        assembly = _AssemblyHeader(*row)
        if (
            (assembly.generation_run_id is not None and not _identifier(assembly.generation_run_id))
            or (assembly.job_id is not None and not _identifier(assembly.job_id))
            or (assembly.settings_hash is not None and type(assembly.settings_hash) is not str)
        ):
            _deny()
        root = _root(session, assembly.session_id)
        if (
            assembly.generation_run_id is not None
            and session.scalar(
                select(GenerationRun.id)
                .join(SessionRecord, SessionRecord.id == GenerationRun.session_id)
                .join(
                    GenerationPlanRevision,
                    GenerationPlanRevision.id == GenerationRun.plan_revision_id,
                )
                .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                .where(
                    GenerationRun.id == assembly.generation_run_id,
                    GenerationRun.session_id == root.session_id,
                    GenerationPlan.session_id == root.session_id,
                )
            )
            is None
        ):
            _deny()
        header = _authorize(session, root, assembly.job_id)
        if header is not None:
            if payload.keys() - _INJECTED != {"output_assembly_id"}:
                _deny()
            if assembly.job_id != header.id or header.kind not in _BRIDGES | {
                "generation.assemble"
            }:
                _deny()
            _job_id, _owner_id, generation = _lease_values(lease)
            if (
                (
                    "_job_id" in payload
                    and (not _identifier(payload["_job_id"]) or payload["_job_id"] != header.id)
                )
                or (
                    "_lease_generation" in payload
                    and (
                        type(payload["_lease_generation"]) is not int
                        or payload["_lease_generation"] != generation
                    )
                )
                or (header.kind == "generation.assemble" and not _INJECTED <= payload.keys())
                or (header.kind in _BRIDGES and assembly.generation_run_id is None)
            ):
                _deny()
        proof = AssemblyExecutionProof(
            assembly.id,
            *root,
            assembly.generation_run_id,
            assembly.job_id,
            assembly.settings_hash,
            header.id if header is not None else None,
            header.kind if header is not None else None,
        )
        if expected is not None and expected != proof:
            _deny()
        if header is not None:
            if header.kind in _BRIDGES:
                _bridge_payload(session, root, header)
            else:
                stored = _stored_payload(session, header)
                if (
                    stored.keys() != {"output_assembly_id"}
                    or not _identifier(stored.get("output_assembly_id"))
                    or stored["output_assembly_id"] != assembly.id
                ):
                    _deny()
        return proof


__all__ = ["AssemblyExecutionProof", "require_assembly_execution", "require_export_assembly_job"]
