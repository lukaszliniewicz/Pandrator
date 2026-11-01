"""Current role, credential, actor and lease proof for media source reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple, NoReturn, TypeGuard

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..accounts.actor_models import JobActorRecord
from ..accounts.execution_actor import (
    HostActor,
    UserActor,
    current_execution_actor,
)
from ..accounts.job_authorization import require_execution_actor_in_session
from ..job_execution import current_job_lease
from ..job_inputs import artifact_job_ownership
from ..model_base import utcnow
from ..models import Artifact, Job, WorkflowRun
from .artifact_visibility import artifact_roots_clause, resource_visibility_clause
from .contracts import AccessDenied, ProjectAction

_MEDIA_KINDS = frozenset({"audio.preview", "audio.waveform", "video.preview"})
_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})
_CONTROL_KEYS: dict[str, tuple[str, ...]] = {
    "audio.preview": (),
    "audio.waveform": ("max_points", "start_ms", "end_ms"),
    "video.preview": ("source_content_hash", "preview_version"),
}


@dataclass(frozen=True, slots=True)
class MediaSourceSnapshot:
    artifact_id: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    relative_path: str
    content_hash: str | None
    size_bytes: int | None

    @classmethod
    def from_artifact(cls, artifact: Artifact) -> MediaSourceSnapshot:
        if type(artifact) is not Artifact:
            raise TypeError("A stored artifact is required.")
        return cls(
            artifact_id=artifact.id,
            workspace_id=artifact.workspace_id,
            project_id=artifact.project_id,
            session_id=artifact.session_id,
            relative_path=artifact.relative_path,
            content_hash=artifact.content_hash,
            size_bytes=artifact.size_bytes,
        )


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
    raise AccessDenied()


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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _read_job_header(session: Session, job_id: str) -> _JobHeader | None:
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
        ).where(Job.id == job_id)
    ).one_or_none()
    return _JobHeader(*row) if row is not None else None


def _active_legacy_job(
    session: Session,
    lease: object,
    payload: Mapping[str, Any],
) -> _JobHeader:
    job_id, owner_id, generation = _lease_values(lease)
    if not _identifier(payload.get("_job_id")) or payload["_job_id"] != job_id:
        _deny()
    header = _read_job_header(session, job_id)
    if (
        header is None
        or header.status != "running"
        or header.lease_owner != owner_id
        or type(header.lease_generation) is not int
        or header.lease_generation != generation
        or type(header.lease_expires_at) is not datetime
        or _aware(header.lease_expires_at) <= _aware(utcnow())
    ):
        _deny()
    actor_record = session.scalar(
        select(JobActorRecord.job_id).where(JobActorRecord.job_id == job_id)
    )
    if actor_record is not None:
        _deny()
    return header


def _header_source_clause(header: _JobHeader) -> ColumnElement[bool]:
    clauses: list[ColumnElement[bool]] = [
        Artifact.workspace_id == header.workspace_id,
        Artifact.project_id == header.project_id,
    ]
    if header.session_id is not None:
        clauses.append(Artifact.session_id == header.session_id)
    if header.workflow_run_id is not None:
        if header.session_id is None:
            _deny()
        clauses.append(
            exists(
                select(WorkflowRun.id).where(
                    WorkflowRun.id == header.workflow_run_id,
                    WorkflowRun.session_id == header.session_id,
                )
            )
        )
    return and_(*clauses)


def _load_visible_source(
    session: Session,
    source_id: str,
    actor: UserActor | HostActor | None,
    header: _JobHeader | None,
) -> Artifact:
    clauses: list[ColumnElement[bool]] = [
        Artifact.id == source_id,
        Artifact.state != "deleted",
    ]
    if type(actor) is UserActor:
        clauses.append(
            resource_visibility_clause(
                Artifact,
                actor,
                action=ProjectAction.RUN,
            )
        )
    else:
        clauses.append(artifact_roots_clause(Artifact))
    if header is not None:
        clauses.append(_header_source_clause(header))

    artifact = session.scalar(
        select(Artifact).where(*clauses).execution_options(populate_existing=True)
    )
    if artifact is None:
        _deny()
    try:
        owner = artifact_job_ownership(session, [source_id])
    except (KeyError, TypeError, ValueError):
        _deny()
    if owner.workspace_id != artifact.workspace_id or owner.project_id != artifact.project_id:
        _deny()
    return artifact


def _same_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _stored_payload_matches(
    session: Session,
    header: _JobHeader,
    kind: str,
    payload: Mapping[str, Any],
    source_id: str,
    *,
    legacy: bool,
    allow_host_omitted_controls: bool,
) -> bool:
    stored_payload = session.scalar(
        select(Job.payload_json).where(
            Job.id == header.id,
            Job.kind == header.kind,
            Job.workspace_id == header.workspace_id,
            Job.project_id == header.project_id,
            Job.session_id == header.session_id,
            Job.workflow_run_id == header.workflow_run_id,
        )
    )
    if type(stored_payload) is not dict:
        return False
    stored_source_id = stored_payload.get("source_artifact_id")
    if type(stored_source_id) is not str or stored_source_id != source_id:
        return False

    for key in _CONTROL_KEYS[kind]:
        supplied_present = key in payload
        stored_present = key in stored_payload
        if allow_host_omitted_controls and not supplied_present and not stored_present:
            continue
        if allow_host_omitted_controls and supplied_present != stored_present:
            return False
        if not legacy and (not supplied_present or not stored_present):
            return False
        if not legacy and supplied_present != stored_present:
            return False
        if not _same_value(payload.get(key), stored_payload.get(key)):
            return False
    return True


def require_media_execution_artifact(
    session: Session,
    payload: Mapping[str, Any],
    kind: Literal["audio.preview", "audio.waveform", "video.preview"],
    *,
    expected_source: MediaSourceSnapshot | None = None,
) -> Artifact:
    """Prove current execution and source ownership before returning a media source."""
    if not isinstance(payload, Mapping):
        raise TypeError("A media job payload mapping is required.")
    if type(kind) is not str:
        raise TypeError("A media job kind string is required.")
    if kind not in _MEDIA_KINDS:
        raise ValueError("The media job kind is unsupported.")
    if expected_source is not None and type(expected_source) is not MediaSourceSnapshot:
        raise TypeError("A media source snapshot is required.")

    source_id = payload.get("source_artifact_id")
    if not _identifier(source_id):
        _deny()

    actor = current_execution_actor.get()
    lease = current_job_lease.get()
    with session.no_autoflush:
        header: _JobHeader | None = None
        legacy_lease = False
        if type(actor) is UserActor or type(actor) is HostActor:
            if lease is not None:
                job_id, _owner_id, _generation = _lease_values(lease)
                if not _identifier(payload.get("_job_id")) or payload["_job_id"] != job_id:
                    _deny()
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=("app.read", "app.run"),
                    allow_host=type(actor) is HostActor,
                )
            except PermissionError:
                _deny()
            if lease is not None:
                header = _read_job_header(session, job_id)
                if header is None or header.kind != kind:
                    _deny()
        elif actor is None:
            if lease is not None:
                header = _active_legacy_job(session, lease, payload)
                if header.kind != kind:
                    _deny()
                legacy_lease = True
        else:
            _deny()

        artifact = _load_visible_source(session, source_id, actor, header)
        snapshot = MediaSourceSnapshot.from_artifact(artifact)
        if expected_source is not None and snapshot != expected_source:
            _deny()
        if header is not None and not _stored_payload_matches(
            session,
            header,
            kind,
            payload,
            source_id,
            legacy=legacy_lease or type(actor) is HostActor,
            allow_host_omitted_controls=type(actor) is HostActor,
        ):
            _deny()
        return artifact


__all__ = ["MediaSourceSnapshot", "require_media_execution_artifact"]
