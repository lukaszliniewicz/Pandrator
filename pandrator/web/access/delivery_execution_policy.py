"""Capture immutable delivery dispatch once; reprove current authority by headers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple, NoReturn, TypeGuard, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.models import Workspace
from ..credential_identity import credential_namespace
from ..job_execution import current_job_lease
from ..model_base import utcnow
from ..models import Job, JobActorRecord, SessionRecord, WorkflowRun
from ..projects.models import Project
from .contracts import AccessDenied, ProjectAction
from .current_session_policy import require_current_session_access
from .session_policy import SessionNotFound

DeliveryEntry = Literal["export", "export_variant", "continue_workflow"]
_PRIMARY: dict[DeliveryEntry, str] = {
    "export": "export.create",
    "export_variant": "export.variant",
    "continue_workflow": "workflow.continue",
}
_KINDS = frozenset(_PRIMARY.values())
_CHILD_KINDS = {
    "export_variant": frozenset({"workflow.continue"}),
    "export": frozenset({"export.variant", "workflow.continue"}),
}
_INJECTED = frozenset({"_job_id", "_lease_generation"})
_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})


@dataclass(frozen=True, slots=True)
class DeliveryExecutionProof:
    session_id: str
    project_id: str
    workspace_id: str
    job_id: str | None
    job_kind: str | None


@dataclass(frozen=True, slots=True)
class _DeliveryChildScope:
    proof: DeliveryExecutionProof
    entry: DeliveryEntry
    digest: str


_delivery_child: ContextVar[_DeliveryChildScope | None] = ContextVar(
    "pandrator_delivery_child",
    default=None,
)


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


def _entry(value: object) -> DeliveryEntry:
    if type(value) is not str or value not in _PRIMARY:
        _deny()
    return cast(DeliveryEntry, value)


def _validate_proof(proof: object) -> TypeGuard[DeliveryExecutionProof]:
    if (
        type(proof) is not DeliveryExecutionProof
        or not all(
            _identifier(value)
            for value in (
                proof.session_id,
                proof.project_id,
                proof.workspace_id,
            )
        )
        or ((proof.job_id is None) != (proof.job_kind is None))
        or (proof.job_id is not None and not _identifier(proof.job_id))
        or (
            proof.job_kind is not None
            and (type(proof.job_kind) is not str or proof.job_kind not in _KINDS)
        )
    ):
        _deny()
    return True


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


def _injected(payload: Mapping[str, Any], job_id: str, generation: int) -> None:
    if (
        not _INJECTED <= payload.keys()
        or not _identifier(payload["_job_id"])
        or payload["_job_id"] != job_id
        or type(payload["_lease_generation"]) is not int
        or payload["_lease_generation"] != generation
    ):
        _deny()


def _prove_headers(session: Session, session_id: str) -> DeliveryExecutionProof:
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
    if row is None or not all(_identifier(value) for value in row):
        _deny()
    current_session_id, project_id, workspace_id = row
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
    header: _JobHeader | None = None
    if lease is not None:
        job_id, owner_id, generation = _lease_values(lease)
        job = session.execute(
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
        if job is None:
            _deny()
        header = _JobHeader(*job)
        expires = header.lease_expires_at
        if type(expires) is not datetime:
            _deny()
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if (
            header.kind not in _KINDS
            or header.session_id != current_session_id
            or header.project_id != project_id
            or header.workspace_id != workspace_id
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
                    WorkflowRun.session_id == current_session_id,
                )
            )
            is None
        ):
            _deny()
    try:
        require_current_session_access(session, current_session_id, ProjectAction.RUN)
    except (PermissionError, SessionNotFound):
        _deny()
    if actor is None:
        try:
            legacy = credential_namespace(session, workspace_id).legacy_host_access
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
    return DeliveryExecutionProof(
        current_session_id,
        project_id,
        workspace_id,
        header.id if header is not None else None,
        header.kind if header is not None else None,
    )


def _json_value(value: object) -> bool:
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    return False


def _same_json(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict and type(right) is dict:
        return left.keys() == right.keys() and all(
            _same_json(value, right[key]) for key, value in left.items()
        )
    if type(left) is list and type(right) is list:
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _digest(payload: Mapping[str, Any]) -> str:
    try:
        value = dict(payload)
        if not _json_value(value):
            _deny()
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _deny()
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_delivery_execution(session: Session, proof: DeliveryExecutionProof) -> None:
    """Reprove only current roots, actor and active lease; never reread delivery bodies."""
    _validate_proof(proof)
    with session.no_autoflush:
        if _prove_headers(session, proof.session_id) != proof:
            _deny()


def capture_delivery_execution(
    session: Session,
    payload: Mapping[str, Any],
    *,
    entry: DeliveryEntry,
) -> DeliveryExecutionProof:
    """Authorize first, then validate one immutable queue dispatch or sealed child."""
    entry = _entry(entry)
    if not isinstance(payload, Mapping) or not _identifier(payload.get("session_id")):
        _deny()
    with session.no_autoflush:
        proof = _prove_headers(session, payload["session_id"])
        lease = current_job_lease.get()
        if lease is None:
            if payload.keys() & _INJECTED:
                _deny()
            return proof
        job_id, _owner_id, generation = _lease_values(lease)
        _injected(payload, job_id, generation)
        child = _delivery_child.get()
        if child is not None and child.entry == entry:
            if child.proof != proof or child.digest != _digest(payload):
                _deny()
        elif proof.job_kind == _PRIMARY[entry]:
            try:
                stored = session.scalar(select(Job.payload_json).where(Job.id == job_id))
                supplied = {key: value for key, value in payload.items() if key not in _INJECTED}
                valid = (
                    type(stored) is dict
                    and _json_value(stored)
                    and _json_value(supplied)
                    and _same_json(supplied, stored)
                )
            except (TypeError, ValueError, RecursionError):
                _deny()
            if not valid:
                _deny()
        else:
            _deny()
        return proof


@contextmanager
def delivery_child_scope(
    proof: DeliveryExecutionProof,
    entry: DeliveryEntry,
    payload: Mapping[str, Any],
) -> Iterator[None]:
    """Seal only a trusted inline continuation/variant child, without retaining its body."""
    _validate_proof(proof)
    entry = _entry(entry)
    if (
        entry not in _CHILD_KINDS
        or not isinstance(payload, Mapping)
        or (not _identifier(payload.get("session_id")) or payload["session_id"] != proof.session_id)
    ):
        _deny()
    lease = current_job_lease.get()
    if lease is None:
        actor = current_execution_actor.get()
        if (
            proof.job_id is not None
            or payload.keys() & _INJECTED
            or (actor is not None and type(actor) is not HostActor)
            or (type(actor) is HostActor and getattr(actor, "label", None) != "pandrator-cli")
        ):
            _deny()
        del payload
        yield
        return
    job_id, _owner_id, generation = _lease_values(lease)
    if proof.job_id != job_id or proof.job_kind not in _CHILD_KINDS[entry]:
        _deny()
    _injected(payload, job_id, generation)
    digest = _digest(payload)
    sealed = _DeliveryChildScope(proof, entry, digest)
    del payload
    token = _delivery_child.set(sealed)
    try:
        yield
    finally:
        _delivery_child.reset(token)


__all__ = [
    "DeliveryEntry",
    "DeliveryExecutionProof",
    "capture_delivery_execution",
    "require_delivery_execution",
    "delivery_child_scope",
]
