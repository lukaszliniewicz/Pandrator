"""Read-only, current execution authority for an admitted generation run."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, NamedTuple, NoReturn, TypeGuard

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.credential_resources import credential_project_clause
from ..accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_storage,
    current_execution_actor,
)
from ..accounts.job_authorization import require_execution_actor_in_session
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
    SessionRecord,
    WorkflowRun,
)
from ..projects.models import Project
from .contracts import AccessDenied, ProjectAction
from .project_visibility import project_ids_for_user

_INJECTED = frozenset({"_job_id", "_lease_generation"})
_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})
_BRIDGES = frozenset({"workflow.continue", "audiobook.generate_audio", "dubbing.generate_audio"})
_BRIDGE_KEYS = frozenset({"generation_run_id", "operation", "segment_ids"}) | _INJECTED


@dataclass(frozen=True, slots=True)
class GenerationExecutionProof:
    run_id: str
    session_id: str
    project_id: str
    workspace_id: str
    plan_revision_id: str
    job_id: str | None
    job_kind: str | None
    settings_hash: str | None
    legacy_host_access: bool
    host_credentials_allowed: bool


@dataclass(frozen=True, slots=True)
class _GenerationChildScope:
    parent: GenerationExecutionProof
    payload: dict[str, Any]


_generation_child_scope: ContextVar[_GenerationChildScope | None] = ContextVar(
    "pandrator_generation_child_scope",
    default=None,
)


class _RunHeader(NamedTuple):
    run_id: str
    session_id: str
    project_id: str
    workspace_id: str
    plan_revision_id: str
    job_id: str | None
    settings_hash: str | None


class _JobHeader(NamedTuple):
    id: str
    kind: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    workflow_run_id: str | None
    status: str
    lease_owner: str | None
    lease_generation: int
    lease_expires_at: datetime | None


def _deny() -> NoReturn:
    raise AccessDenied() from None


def _identifier(value: object) -> TypeGuard[str]:
    return type(value) is str and bool(value) and value == value.strip()


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


def _read_run(session: Session, run_id: str) -> _RunHeader:
    row = session.execute(
        select(
            GenerationRun.id,
            GenerationRun.session_id,
            Project.id,
            Workspace.id,
            GenerationRun.plan_revision_id,
            GenerationRun.job_id,
            GenerationRun.settings_hash,
        )
        .join(SessionRecord, SessionRecord.id == GenerationRun.session_id)
        .join(Project, Project.id == SessionRecord.project_id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .join(GenerationPlanRevision, GenerationPlanRevision.id == GenerationRun.plan_revision_id)
        .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
        .where(
            GenerationRun.id == run_id,
            GenerationPlan.session_id == SessionRecord.id,
            SessionRecord.trashed_at.is_(None),
            Workspace.state == "active",
        )
    ).one_or_none()
    if row is None:
        _deny()
    return _RunHeader(*row)


def _read_job(session: Session, run: _RunHeader) -> _JobHeader | None:
    if run.job_id is None:
        return None
    row = session.execute(
        select(
            Job.id,
            Job.kind,
            Job.workspace_id,
            Job.project_id,
            Job.session_id,
            Job.workflow_run_id,
            Job.status,
            Job.lease_owner,
            Job.lease_generation,
            Job.lease_expires_at,
        ).where(Job.id == run.job_id)
    ).one_or_none()
    if row is None:
        _deny()
    header = _JobHeader(*row)
    if (
        header.workspace_id != run.workspace_id
        or header.project_id != run.project_id
        or header.session_id != run.session_id
        or header.kind not in _BRIDGES | {
            "generation.run", "generation.repair-existing"
        }
    ):
        _deny()
    if (
        header.workflow_run_id is not None
        and session.scalar(
            select(WorkflowRun.id).where(
                WorkflowRun.id == header.workflow_run_id,
                WorkflowRun.session_id == run.session_id,
            )
        )
        is None
    ):
        _deny()
    return header


def _recorded_actor(session: Session, job_id: str) -> UserActor | HostActor | None:
    row = session.execute(
        select(
            JobActorRecord.user_id, JobActorRecord.auth_revision, JobActorRecord.context_json
        ).where(JobActorRecord.job_id == job_id)
    ).one_or_none()
    if row is None:
        return None
    try:
        actor = actor_from_storage(*row)
    except (AttributeError, TypeError, ValueError):
        _deny()
    # An existing record must encode an actual identity, including for old Host jobs.
    if actor is None:
        _deny()
    return actor


def _active_legacy_lease(header: _JobHeader, owner_id: str, generation: int) -> None:
    expires = header.lease_expires_at
    if type(expires) is not datetime:
        _deny()
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if (
        header.status != "running"
        or header.lease_owner != owner_id
        or type(header.lease_generation) is not int
        or header.lease_generation != generation
        or expires <= utcnow()
    ):
        _deny()


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


@contextmanager
def generation_child_scope(
    parent: GenerationExecutionProof,
    parent_payload: Mapping[str, Any],
) -> Iterator[None]:
    """Carry a trusted parent's dispatch into an internal optional generation pass."""
    if (
        type(parent) is not GenerationExecutionProof
        or not all(
            _identifier(value)
            for value in (
                parent.run_id,
                parent.session_id,
                parent.project_id,
                parent.workspace_id,
                parent.plan_revision_id,
            )
        )
        or (parent.job_id is not None and not _identifier(parent.job_id))
        or (parent.job_kind is not None and not _identifier(parent.job_kind))
        or ((parent.job_id is None) != (parent.job_kind is None))
        or (parent.settings_hash is not None and type(parent.settings_hash) is not str)
        or type(parent.legacy_host_access) is not bool
        or type(parent.host_credentials_allowed) is not bool
        or not isinstance(parent_payload, Mapping)
    ):
        _deny()
    try:
        payload = dict(parent_payload)
        if (
            not _json_value(payload)
            or not _identifier(payload.get("generation_run_id"))
            or payload["generation_run_id"] != parent.run_id
            or (
                "_job_id" in payload
                and (not _identifier(payload["_job_id"]) or payload["_job_id"] != parent.job_id)
            )
            or (
                "_lease_generation" in payload
                and (
                    type(payload["_lease_generation"]) is not int
                    or payload["_lease_generation"] <= 0
                )
            )
        ):
            _deny()
        captured = _GenerationChildScope(parent, deepcopy(payload))
    except (TypeError, ValueError, RecursionError):
        _deny()
    token = _generation_child_scope.set(captured)
    try:
        yield
    finally:
        _generation_child_scope.reset(token)


def _require_child_lineage(
    session: Session,
    child: GenerationExecutionProof,
    parent: GenerationExecutionProof,
) -> None:
    def headers():
        return (
            select(GenerationRun.id, GenerationRun.source_generation_run_id)
            .join(
                GenerationPlanRevision, GenerationPlanRevision.id == GenerationRun.plan_revision_id
            )
            .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
            .where(
                GenerationRun.job_id == parent.job_id,
                GenerationRun.session_id == parent.session_id,
                GenerationPlan.session_id == GenerationRun.session_id,
            )
        )

    lineage = (
        headers()
        .where(GenerationRun.id == child.run_id)
        .cte(
            "generation_child_lineage",
            recursive=True,
        )
    )
    lineage = lineage.union(
        headers()
        .join(lineage, GenerationRun.id == lineage.c.source_generation_run_id)
        .where(lineage.c.id != parent.run_id)
    )
    if session.scalar(select(lineage.c.id).where(lineage.c.id == parent.run_id).limit(1)) is None:
        _deny()


def _require_child_dispatch(
    session: Session,
    payload: Mapping[str, Any],
    child: GenerationExecutionProof,
    scope: _GenerationChildScope,
) -> None:
    parent = scope.parent
    if (
        payload.keys() - _INJECTED != {"generation_run_id", "operation", "segment_ids"}
        or type(payload.get("operation")) is not str
        or payload["operation"] != "resume"
        or type(payload.get("segment_ids")) is not list
        or payload["segment_ids"] != []
        or parent.job_id != child.job_id
        or parent.job_kind != child.job_kind
        or parent.session_id != child.session_id
        or parent.project_id != child.project_id
        or parent.workspace_id != child.workspace_id
    ):
        _deny()
    _require_child_lineage(session, child, parent)
    # Parent == scoped parent takes the ordinary strict path, without recursion.
    require_generation_execution(session, scope.payload, expected=parent)


def _require_repair_child_lineage(
    session: Session,
    child_id: str,
    root_id: str,
    *,
    job_id: str,
    session_id: str,
) -> None:
    """Prove a repair child descends from the stored root under this job.

    The ordinary child path requires every hop to share the parent job. A
    post-run repair has a different job for its staged descendants, so the
    walk terminates at the explicitly stored root and requires every
    intermediate run to be a marker-bearing child of this exact repair job.
    """

    current = child_id
    seen: set[str] = set()
    for _ in range(64):
        if current == root_id:
            return
        if current in seen:
            _deny()
        seen.add(current)
        row = session.execute(
            select(
                GenerationRun.source_generation_run_id,
                GenerationRun.job_id,
                GenerationRun.session_id,
                GenerationRun.settings_snapshot_json,
            ).where(GenerationRun.id == current)
        ).one_or_none()
        if row is None:
            _deny()
        source_id, run_job_id, run_session_id, snapshot = row
        if (
            run_job_id != job_id
            or run_session_id != session_id
            or not isinstance(snapshot, dict)
            or snapshot.get("early_repair_parent_run_id") != root_id
            or not _identifier(source_id)
        ):
            _deny()
        current = source_id
    _deny()


def _check_payload(session: Session, payload: Mapping[str, Any], header: _JobHeader) -> None:
    try:
        stored = session.scalar(select(Job.payload_json).where(Job.id == header.id))
    except JSONDecodeError:
        _deny()
    supplied = {key: value for key, value in payload.items() if key not in _INJECTED}
    try:
        if type(stored) is not dict or not _json_value(stored) or not _json_value(supplied):
            _deny()
        if header.kind == "generation.run":
            if not _same_json(supplied, stored):
                _deny()
        elif header.kind == "generation.repair-existing":
            # The repair job has authority only for its own freshly staged
            # descendants, not arbitrary generation runs in the session.
            repair_session_id = header.session_id
            if (
                set(stored) != {"generation_run_id", "session_id"}
                or not _identifier(stored.get("generation_run_id"))
                or not _identifier(repair_session_id)
                or stored.get("session_id") != repair_session_id
                or set(supplied) != {"generation_run_id", "operation", "segment_ids"}
                or supplied.get("operation") != "resume"
                or type(supplied.get("segment_ids")) is not list
                or supplied["segment_ids"] != []
            ):
                _deny()
            child = session.get(GenerationRun, supplied["generation_run_id"])
            root = session.get(GenerationRun, stored["generation_run_id"])
            if (
                child is None or root is None
                or child.session_id != header.session_id
                or root.session_id != header.session_id
                or child.job_id != header.id
                or child.source_generation_run_id is None
                or not isinstance(child.settings_snapshot_json, dict)
                or child.settings_snapshot_json.get("early_repair_parent_run_id")
                != root.id
            ):
                _deny()
            if root.job_id is not None:
                root_job = session.execute(
                    select(Job.kind, Job.session_id).where(Job.id == root.job_id)
                ).one_or_none()
                if (
                    root_job is None
                    or root_job.kind != "generation.run"
                    or root_job.session_id != header.session_id
                ):
                    _deny()
            _require_repair_child_lineage(
                session,
                child.id,
                root.id,
                job_id=header.id,
                session_id=repair_session_id,
            )
        elif (
            type(stored.get("session_id")) is not str
            or stored["session_id"] != header.session_id
            or not payload.keys() <= _BRIDGE_KEYS
            or payload.get("operation") != "generate"
            or type(payload.get("operation")) is not str
            or (
                "segment_ids" in payload
                and (type(payload["segment_ids"]) is not list or payload["segment_ids"] != [])
            )
        ):
            _deny()
    except RecursionError:
        _deny()


def require_generation_execution(
    session: Session,
    payload: Mapping[str, Any],
    *,
    expected: GenerationExecutionProof | None = None,
) -> GenerationExecutionProof:
    """Prove current roots and execution authority without reading a settings body."""
    if not isinstance(payload, Mapping) or (
        expected is not None and type(expected) is not GenerationExecutionProof
    ):
        _deny()
    run_id = payload.get("generation_run_id")
    if not _identifier(run_id):
        _deny()
    actor = current_execution_actor.get()
    lease = current_job_lease.get()
    with session.no_autoflush:
        run = _read_run(session, run_id)
        header = _read_job(session, run)
        child_scope = _generation_child_scope.get()
        derived = (
            lease is not None and child_scope is not None and run_id != child_scope.parent.run_id
        )
        if type(actor) is UserActor and lease is None:
            _deny()
        if lease is not None:
            job_id, owner_id, generation = _lease_values(lease)
            if header is None or header.id != job_id:
                _deny()
            if (
                (
                    "_job_id" in payload
                    and (not _identifier(payload["_job_id"]) or payload["_job_id"] != job_id)
                )
                or (
                    "_lease_generation" in payload
                    and (
                        type(payload["_lease_generation"]) is not int
                        or payload["_lease_generation"] != generation
                    )
                )
                or (
                    header.kind == "generation.run"
                    and not derived
                    and not _INJECTED <= payload.keys()
                )
            ):
                _deny()
        elif _INJECTED & payload.keys():
            _deny()

        if type(actor) is UserActor or type(actor) is HostActor:
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=("app.read", "app.run"),
                    allow_host=True,
                )
            except PermissionError:
                _deny()
            if (
                type(actor) is UserActor
                and session.scalar(
                    project_ids_for_user(actor.user_id, action=ProjectAction.RUN).where(
                        Project.id == run.project_id,
                        credential_project_clause(actor),
                    )
                )
                is None
            ):
                _deny()
        elif actor is None:
            if lease is not None:
                assert header is not None
                _job_id, owner_id, generation = _lease_values(lease)
                _active_legacy_lease(header, owner_id, generation)
                if (
                    session.scalar(
                        select(JobActorRecord.job_id).where(JobActorRecord.job_id == header.id)
                    )
                    is not None
                ):
                    _deny()
            elif header is not None and type(_recorded_actor(session, header.id)) is UserActor:
                _deny()
        else:
            _deny()
        if lease is None and header is not None and type(actor) is HostActor:
            _recorded_actor(session, header.id)

        try:
            namespace = credential_namespace(session, run.workspace_id)
            legacy = namespace.legacy_host_access
        except (KeyError, TypeError, ValueError, RuntimeError):
            _deny()
        if actor is None and not legacy:
            _deny()
        proof = GenerationExecutionProof(
            *run[:6],
            header.kind if header is not None else None,
            run.settings_hash,
            legacy,
            namespace.host_credentials_allowed,
        )
        if expected is not None and proof != expected:
            _deny()
        if lease is not None and header is not None:
            if derived:
                assert child_scope is not None
                _require_child_dispatch(session, payload, proof, child_scope)
            else:
                _check_payload(session, payload, header)
        return proof


__all__ = ["GenerationExecutionProof", "generation_child_scope", "require_generation_execution"]
