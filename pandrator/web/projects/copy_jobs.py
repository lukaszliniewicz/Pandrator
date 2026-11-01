"""Durable copy admission and actor/lease-fenced current-state publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
import zlib
from collections.abc import Callable
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .. import models as m
from ..access.artifact_policy import visible_artifact_ids_statement
from ..access.artifact_visibility import resource_visibility_clause
from ..access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAction,
    ProjectNotFound,
    WorkspaceNotFound,
)
from ..access.job_policy import JobNotFound
from ..access.project_policy import require_project_access, require_project_creation
from ..access.project_visibility import creatable_workspace_ids_for_user, project_ids_for_user
from ..access.sound_dependency_visibility import current_sound_dependency_clause
from ..access.workspace_policy import require_workspace_access
from ..accounts.actor_authorization import ActorAuthorizationDenied
from ..accounts.credential_resources import credential_project_clause, credential_workspace_clause
from ..accounts.execution_actor import (
    UserActor,
    actor_from_principal,
    current_execution_actor,
    execution_actor_scope,
)
from ..accounts.installation_mode import InstallationModeError, installation_mode_in_session
from ..accounts.job_authorization import require_execution_actor_in_session
from ..audit import AuditService
from ..auth import Principal
from ..database import Database
from ..idempotency import IdempotencyConflict, IdempotencyInProgress, IdempotencyService
from ..job_execution import current_job_lease
from ..jobs import JobQueue
from .copy_connections import (
    normalize_destination_connections,
    validate_connection_state,
    validate_current_connections,
)
from .copy_contracts import (
    PROJECT_COPY_JOB_KIND,
    SUPPORTED_COPY_STATE_VERSIONS,
    CopyFileSource,
    CopySettingsReader,
    ProjectCopyError,
    ProjectCopySnapshot,
    project_copy_error,
)
from .copy_files import StagedProjectFiles, cleanup_staged_project_files, stage_project_files
from .copy_publication import (
    copy_identity,
    publish_project_copy_rows,
    validate_project_copy_snapshot,
)
from .copy_snapshot import capture_current_project

_MAX_RAW_BYTES = 64 * 1024 * 1024
_MAX_ENCODED_BYTES = 4 * ((_MAX_RAW_BYTES + _MAX_RAW_BYTES // 1000 + 65536 + 2) // 3)
_HEX = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_FILE_HEX = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_HEADERS = (
    "copy_version",
    "copy_id",
    "source_project_id",
    "source_session_id",
    "destination_workspace_id",
    "source_fingerprint",
    "snapshot_sha256",
)
_PAYLOAD = frozenset((*_HEADERS, "snapshot_zlib_b64"))
_ENVELOPE = frozenset(
    {
        "schema_version",
        "source_project_id",
        "destination_workspace_id",
        "name",
        "state_json",
        "fingerprint",
        "files",
        "blockers",
        "active_job_count",
    }
)
_FILE_FIELDS = frozenset(
    {
        "artifact_id",
        "workspace_id",
        "project_id",
        "session_id",
        "relative_path",
        "content_hash",
        "size_bytes",
    }
)
_RESULT = frozenset(
    {
        "project_id",
        "source_session_id",
        "translation_project_id",
        "workspace_id",
        "name",
        "fingerprint",
    }
)
_DETAILS = frozenset({"Preparing project copy", "Copying project files", "Publishing project copy"})
_TERMINAL = frozenset({"succeeded", "failed", "canceled"})
_COPY_ERRORS = (
    "copy_connection_unavailable",
    "copy_source_unavailable",
    "copy_limit_exceeded",
    "copy_graph_invalid",
    "copy_file_unavailable",
    "copy_staging_failed",
    "copy_preview_stale",
    "copy_not_ready",
    "copy_input_invalid",
    "copy_canceled",
    "copy_publication_failed",
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value)


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _name(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip() or len(value.strip()) > 255:
        raise ValueError("Copy name must contain between 1 and 255 characters.")
    return value.strip()


def _context(principal: Principal) -> AccessContext:
    try:
        actor = actor_from_principal(principal)
    except (TypeError, ValueError):
        raise AccessDenied() from None
    if type(actor) is not UserActor:
        raise AccessDenied()
    return AccessContext(actor)


def _mode(session: Session) -> None:
    if not installation_mode_in_session(session).enabled:
        raise InstallationModeError(
            "collaboration_disabled", "Enable collaboration to copy projects."
        )


def _source(session: Session, project_id: str, expected_session_id: str | None = None):
    root = session.execute(
        select(m.Project.workspace_id, m.Project.source_session_id).where(
            m.Project.id == project_id
        )
    ).one_or_none()
    if (
        root is None
        or root.source_session_id is None
        or (expected_session_id is not None and root.source_session_id != expected_session_id)
    ):
        raise project_copy_error("copy_source_unavailable")
    live = session.scalar(
        select(m.SessionRecord.id).where(
            m.SessionRecord.id == root.source_session_id,
            m.SessionRecord.project_id == project_id,
            m.SessionRecord.trashed_at.is_(None),
        )
    )
    if live is None:
        raise project_copy_error("copy_source_unavailable")
    return root


def _admission(
    session: Session, context: AccessContext, project_id: str, destination: str, *, run: bool
):
    _mode(session)
    require_project_access(session, context, project_id, ProjectAction.EDIT)
    require_project_creation(session, context, destination)
    if run and not {"app.read", "app.write", "app.run"} <= context.actor.scopes:
        raise AccessDenied()
    return _source(session, project_id)


def _encode_snapshot(snapshot: ProjectCopySnapshot) -> tuple[str, str]:
    try:
        envelope = {
            "schema_version": 1,
            "source_project_id": snapshot.source_project_id,
            "destination_workspace_id": snapshot.destination_workspace_id,
            "name": snapshot.name,
            "state_json": snapshot.state_json,
            "fingerprint": snapshot.fingerprint,
            "files": [{key: getattr(file, key) for key in _FILE_FIELDS} for file in snapshot.files],
            "blockers": list(snapshot.blockers),
            "active_job_count": snapshot.active_job_count,
        }
        raw = _canonical(envelope).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise project_copy_error("copy_input_invalid") from None
    if len(raw) > _MAX_RAW_BYTES:
        raise project_copy_error("copy_limit_exceeded")
    return hashlib.sha256(raw).hexdigest(), base64.b64encode(zlib.compress(raw, level=6)).decode(
        "ascii"
    )


def _decode_snapshot(encoded: Any, digest: Any) -> ProjectCopySnapshot:
    try:
        if (
            type(encoded) is not str
            or len(encoded) > _MAX_ENCODED_BYTES
            or type(digest) is not str
            or _HEX.fullmatch(digest) is None
        ):
            raise ValueError()
        compressed = base64.b64decode(encoded.encode("ascii"), validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, _MAX_RAW_BYTES + 1)
        if (
            len(raw) > _MAX_RAW_BYTES
            or not inflater.eof
            or inflater.unconsumed_tail
            or inflater.unused_data
        ):
            raise ValueError()
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
            raise ValueError()
        value = json.loads(raw.decode("utf-8"))
        if (
            type(value) is not dict
            or set(value) != _ENVELOPE
            or _canonical(value).encode("utf-8") != raw
        ):
            raise ValueError()
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["blockers"] != []
        ):
            raise ValueError()
        if any(
            not _identifier(value[key])
            for key in ("source_project_id", "destination_workspace_id", "name", "state_json")
        ):
            raise ValueError()
        if (
            _name(value["name"]) != value["name"]
            or type(value["active_job_count"]) is not int
            or value["active_job_count"] < 0
        ):
            raise ValueError()
        fingerprint = value["fingerprint"]
        if (
            type(fingerprint) is not str
            or _HEX.fullmatch(fingerprint) is None
            or not hmac.compare_digest(
                hashlib.sha256(value["state_json"].encode("utf-8")).hexdigest(), fingerprint
            )
        ):
            raise ValueError()
        state = json.loads(value["state_json"])
        if (
            type(state) is not dict
            or type(state["rows"]) is not dict
            or state["binding_fields"] != {}
            or state["unsupported_counts"] != {}
            or _canonical(state) != value["state_json"]
            or type(state.get("schema_version")) is not int
            or state["schema_version"] not in SUPPORTED_COPY_STATE_VERSIONS
        ):
            raise ValueError()
        validate_connection_state(state, state["rows"])
        if type(value["files"]) is not list or len(value["files"]) > 10_000:
            raise ValueError()
        files, seen = [], set()
        for file in value["files"]:
            if type(file) is not dict or set(file) != _FILE_FIELDS:
                raise ValueError()
            if any(
                not _identifier(file[key])
                for key in ("artifact_id", "workspace_id", "relative_path")
            ):
                raise ValueError()
            if any(
                file[key] is not None and not _identifier(file[key])
                for key in ("project_id", "session_id")
            ):
                raise ValueError()
            if (
                type(file["content_hash"]) is not str
                or _FILE_HEX.fullmatch(file["content_hash"]) is None
            ):
                raise ValueError()
            if file["size_bytes"] is not None and (
                type(file["size_bytes"]) is not int or file["size_bytes"] < 0
            ):
                raise ValueError()
            if file["artifact_id"] in seen:
                raise ValueError()
            seen.add(file["artifact_id"])
            files.append(CopyFileSource(**file))
        return ProjectCopySnapshot(
            value["source_project_id"],
            value["destination_workspace_id"],
            value["name"],
            value["state_json"],
            fingerprint,
            tuple(files),
            (),
            value["active_job_count"],
        )
    except (ValueError, TypeError, UnicodeError, zlib.error, binascii.Error, KeyError):
        raise project_copy_error("copy_input_invalid") from None


def _file_headers(
    session: Session, actor: UserActor, snapshot: ProjectCopySnapshot, *, interactive: bool
) -> None:
    sources = {file.artifact_id: file for file in snapshot.files}
    if len(sources) != len(snapshot.files):
        raise project_copy_error("copy_input_invalid")
    ids = sorted(sources)
    sound_ids: set[str] | None = None
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        existing = set(session.scalars(select(m.Artifact.id).where(m.Artifact.id.in_(chunk))))
        if existing != set(chunk):
            raise project_copy_error("copy_file_unavailable")
        statement = (
            visible_artifact_ids_statement(session, AccessContext(actor))
            if interactive
            else select(m.Artifact.id).where(resource_visibility_clause(m.Artifact, actor))
        )
        visible = set(session.scalars(statement.where(m.Artifact.id.in_(chunk))))
        missing = set(chunk) - visible
        if missing:
            if sound_ids is None:
                # Decode only after an ordinary read misses. This is immutable
                # snapshot data, not a cached authorization or credential result.
                sound_ids = set()
                try:
                    state = json.loads(snapshot.state_json)
                    version = state.get("schema_version") if type(state) is dict else None
                    if type(version) is int and version >= 14 and version in SUPPORTED_COPY_STATE_VERSIONS:
                        sound_ids = {row["artifact_id"] for row in state["rows"]["sound_variants"]}
                except (ValueError, TypeError, KeyError) as error:
                    raise project_copy_error("copy_input_invalid") from error
            selected = missing & sound_ids
            if selected:
                visible.update(session.scalars(select(m.Artifact.id).where(
                    m.Artifact.id.in_(selected),
                    current_sound_dependency_clause(actor, project_id=snapshot.source_project_id),
                )))
        if visible != set(chunk):
            raise AccessDenied()
        rows = session.execute(
            select(
                m.Artifact.id,
                m.Artifact.workspace_id,
                m.Artifact.project_id,
                m.Artifact.session_id,
                m.Artifact.relative_path,
                m.Artifact.content_hash,
                m.Artifact.size_bytes,
                m.Artifact.state,
            ).where(m.Artifact.id.in_(chunk))
        )
        for row in rows:
            source = sources[row.id]
            if row.state == "deleted" or any(
                getattr(row, key) != getattr(source, key)
                for key in ("workspace_id", "project_id", "session_id", "relative_path")
            ):
                raise project_copy_error("copy_file_unavailable")
            if (
                type(row.content_hash) is not str
                or not hmac.compare_digest(
                    row.content_hash.casefold(), str(source.content_hash).casefold()
                )
                or (source.size_bytes is not None and source.size_bytes != row.size_bytes)
            ):
                raise project_copy_error("copy_file_unavailable")


def _headers(session: Session, job_id: str):
    return session.execute(
        select(
            m.Job.id,
            m.Job.kind,
            m.Job.workspace_id,
            m.Job.project_id,
            m.Job.session_id,
            m.Job.workflow_run_id,
            m.Job.status,
            *(
                m.Job.payload_json[key].as_integer().label(key)
                if key == "copy_version"
                else m.Job.payload_json[key].as_string().label(key)
                for key in _HEADERS
            ),
        ).where(m.Job.id == job_id)
    ).one_or_none()


def _header_values(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        set(payload) - {"_job_id", "_lease_generation"} != _PAYLOAD
        or type(payload.get("copy_version")) is not int
        or payload["copy_version"] != 1
    ):
        raise project_copy_error("copy_input_invalid")
    if not _uuid(payload["copy_id"]) or any(
        not _identifier(payload[key])
        for key in ("source_project_id", "source_session_id", "destination_workspace_id")
    ):
        raise project_copy_error("copy_input_invalid")
    if (
        any(
            type(payload[key]) is not str or _HEX.fullmatch(payload[key]) is None
            for key in ("source_fingerprint", "snapshot_sha256")
        )
        or type(payload["snapshot_zlib_b64"]) is not str
    ):
        raise project_copy_error("copy_input_invalid")
    return {key: payload[key] for key in _HEADERS}


def _status(job: m.Job, headers) -> dict[str, Any]:
    result = None
    if job.status == "succeeded":
        private = job.result_json
        if type(private) is not dict or not _RESULT <= set(private):
            raise project_copy_error("copy_input_invalid")
        result = {key: private[key] for key in _RESULT}
        if (
            result["project_id"] != copy_identity(job.id, "projects", headers.source_project_id)
            or result["source_session_id"]
            != copy_identity(job.id, "sessions", headers.source_session_id)
            or result["workspace_id"] != headers.destination_workspace_id
            or result["fingerprint"] != headers.source_fingerprint
        ):
            raise project_copy_error("copy_input_invalid")
        if (
            not _identifier(result["name"])
            or _name(result["name"]) != result["name"]
            or (
                result["translation_project_id"] is not None
                and not _uuid(result["translation_project_id"])
            )
        ):
            raise project_copy_error("copy_input_invalid")
    error = None
    if job.status != "succeeded" and (job.error_code or job.error_message):
        code = next(
            (code for code in _COPY_ERRORS if str(project_copy_error(code)) == job.error_message),
            "copy_publication_failed",
        )
        error = {"code": code, "message": str(project_copy_error(code))}
    return {
        "id": job.id,
        "kind": PROJECT_COPY_JOB_KIND,
        "state": job.status,
        "progress": float(job.progress or 0),
        "detail": job.progress_detail if job.progress_detail in _DETAILS else None,
        "error": error,
        "result": result,
    }


def _raise_safe(error: Exception) -> NoReturn:
    if isinstance(error, ProjectCopyError):
        raise project_copy_error(error.code) from None
    if isinstance(error, AccessDenied):
        raise AccessDenied() from None
    if isinstance(error, ActorAuthorizationDenied):
        raise ActorAuthorizationDenied("The execution actor is not currently authorized.") from None
    if isinstance(error, ProjectNotFound):
        raise ProjectNotFound() from None
    if isinstance(error, WorkspaceNotFound):
        raise WorkspaceNotFound() from None
    if isinstance(error, InstallationModeError):
        raise InstallationModeError(error.code, error.message, error.status_code) from None
    if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
        raise type(error)(str(error)) from None
    if type(error) is RuntimeError and str(error) == "Job lease is no longer owned by this worker.":
        raise RuntimeError("Job lease is no longer owned by this worker.") from None
    raise project_copy_error("copy_publication_failed") from None


class ProjectCopyService:
    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        jobs: JobQueue,
        idempotency: IdempotencyService,
        settings_service: CopySettingsReader,
        audit: AuditService,
    ):
        self.database, self.paths, self.jobs = database, paths, jobs
        self.idempotency, self.settings_service, self.audit = idempotency, settings_service, audit

    def preview(
        self,
        context: AccessContext,
        source_project_id: str,
        destination_workspace_id: str,
        *,
        destination_connections: Any = None,
    ) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            _admission(session, context, source_project_id, destination_workspace_id, run=False)
            snapshot = capture_current_project(
                session,
                context,
                source_project_id,
                destination_workspace_id,
                settings_service=self.settings_service,
                destination_connections=destination_connections,
            )
            return {**snapshot.manifest(), "fingerprint": snapshot.fingerprint}

    def _get(self, session: Session, context: AccessContext, job_id: str):
        _mode(session)
        if type(context) is not AccessContext:
            raise TypeError("An access context is required.")
        headers = _headers(session, job_id)
        user_id = session.scalar(
            select(m.JobActorRecord.user_id).where(m.JobActorRecord.job_id == job_id)
        )
        if (
            headers is None
            or headers.kind != PROJECT_COPY_JOB_KIND
            or user_id != context.actor.user_id
        ):
            raise JobNotFound()
        require_project_access(session, context, headers.source_project_id)
        require_workspace_access(session, context, headers.destination_workspace_id)
        if (
            headers.copy_id != job_id
            or headers.project_id != headers.source_project_id
            or headers.session_id is not None
            or headers.workflow_run_id is not None
        ):
            raise project_copy_error("copy_input_invalid")
        job = session.get(m.Job, job_id)
        assert job is not None
        return _status(job, headers), headers

    def get(self, context: AccessContext, job_id: str) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            return self._get(session, context, job_id)[0]

    def _abandon(self, reservation_id: str | None, principal: Principal) -> None:
        if reservation_id is None:
            return
        try:
            with self.database.immediate_session() as session:
                # Parent-approved metadata-only failure cleanup. This internal ID
                # never comes from public input; authority loss must not pin it.
                record = session.scalar(
                    select(m.ApiIdempotency).where(
                        m.ApiIdempotency.id == reservation_id,
                        m.ApiIdempotency.principal_subject == principal.subject,
                        m.ApiIdempotency.operation_id == "copyCurrentProject",
                        m.ApiIdempotency.state == "in_progress",
                        m.ApiIdempotency.resource_id.is_(None),
                    )
                )
                if record is not None:
                    session.delete(record)
        except Exception:
            # Leave uncertainty for normal reservation expiry, not a new error.
            pass

    def request(
        self,
        principal: Principal,
        source_project_id: str,
        destination_workspace_id: str,
        *,
        expected_fingerprint: str,
        name: str | None = None,
        idempotency_key: object,
        destination_connections: Any = None,
    ) -> tuple[dict[str, Any], int]:
        context = _context(principal)
        normalized = _name(name)
        if type(expected_fingerprint) is not str or _HEX.fullmatch(expected_fingerprint) is None:
            raise ValueError("A 64-character lowercase project fingerprint is required.")
        choices = normalize_destination_connections(destination_connections)
        key = self.idempotency.validate_key(idempotency_key)
        body = {
            "source_project_id": source_project_id,
            "destination_workspace_id": destination_workspace_id,
            "expected_fingerprint": expected_fingerprint,
            "name": normalized,
        }
        if choices:
            body["destination_connections"] = choices
        reservation_id = None
        try:
            with self.database.immediate_session() as session:
                _admission(session, context, source_project_id, destination_workspace_id, run=True)
                reservation = self.idempotency.begin(
                    session,
                    principal=principal,
                    operation_id="copyCurrentProject",
                    idempotency_key=key,
                    payload=body,
                )
                if reservation.replayed:
                    job_id = reservation.record.resource_id
                    if not job_id or reservation.record.resource_kind != "job":
                        raise project_copy_error("copy_input_invalid")
                    status, headers = self._get(session, context, job_id)
                    if (
                        headers.source_project_id != source_project_id
                        or headers.destination_workspace_id != destination_workspace_id
                        or headers.source_fingerprint != expected_fingerprint
                    ):
                        raise project_copy_error("copy_input_invalid")
                    return status, 200 if status["state"] in _TERMINAL else 202
                reservation_id = reservation.record.id
            with self.database.snapshot_session() as session:
                captured_root = _admission(
                    session, context, source_project_id, destination_workspace_id, run=True
                )
                captured_source_session_id = captured_root.source_session_id
                snapshot = capture_current_project(
                    session,
                    context,
                    source_project_id,
                    destination_workspace_id,
                    name=normalized,
                    settings_service=self.settings_service,
                    destination_connections=choices,
                )
                if not snapshot.ready:
                    raise project_copy_error("copy_not_ready")
                if not hmac.compare_digest(snapshot.fingerprint, expected_fingerprint):
                    raise project_copy_error("copy_preview_stale")
            digest, encoded = _encode_snapshot(snapshot)
            with self.database.immediate_session() as session:
                root = _admission(
                    session, context, source_project_id, destination_workspace_id, run=True
                )
                if root.source_session_id != captured_source_session_id:
                    raise project_copy_error("copy_preview_stale")
                validate_current_connections(
                    session,
                    destination_workspace_id,
                    json.loads(snapshot.state_json).get("destination_connections", []),
                )
                _file_headers(session, context.actor, snapshot, interactive=True)
                reservation = self.idempotency.load_in_progress(
                    session, reservation_id, principal=principal
                )
                payload = {
                    "copy_version": 1,
                    "copy_id": "",
                    "source_project_id": source_project_id,
                    "source_session_id": root.source_session_id,
                    "destination_workspace_id": destination_workspace_id,
                    "source_fingerprint": snapshot.fingerprint,
                    "snapshot_sha256": digest,
                    "snapshot_zlib_b64": encoded,
                }
                with execution_actor_scope(context.actor):
                    job = self.jobs.enqueue_in_session(
                        session,
                        PROJECT_COPY_JOB_KIND,
                        payload,
                        workspace_id=root.workspace_id,
                        project_id=source_project_id,
                        session_id=None,
                        workflow_run_id=None,
                        max_attempts=2,
                        resource_keys=["project-copy:disk"],
                    )
                job.payload_json = {**payload, "copy_id": job.id}
                status = {
                    "id": job.id,
                    "kind": PROJECT_COPY_JOB_KIND,
                    "state": job.status,
                    "progress": 0.0,
                    "detail": None,
                    "error": None,
                    "result": None,
                }
                self.idempotency.complete(
                    session,
                    reservation,
                    response=status,
                    status_code=202,
                    resource_kind="job",
                    resource_id=job.id,
                )
            return status, 202
        except Exception as error:
            self._abandon(reservation_id, principal)
            _raise_safe(error)


class ProjectCopyWorker:
    def __init__(self, database: Database, paths: DataPaths, jobs: JobQueue, audit: AuditService):
        self.database, self.paths, self.jobs, self.audit = database, paths, jobs, audit

    @staticmethod
    def _cancel(event: threading.Event) -> None:
        if event.is_set():
            raise project_copy_error("copy_canceled")

    def _fence(
        self, session: Session, payload: dict[str, Any], cancel_event: threading.Event
    ) -> UserActor:
        self._cancel(cancel_event)
        actor, lease = current_execution_actor.get(), current_job_lease.get()
        if type(actor) is not UserActor or lease is None:
            raise AccessDenied()
        incoming = _header_values(payload)
        if (
            lease.get("job_id") != incoming["copy_id"]
            or payload.get("_job_id", lease["job_id"]) != lease["job_id"]
            or payload.get("_lease_generation", lease["lease_generation"])
            != lease["lease_generation"]
        ):
            raise AccessDenied()
        _mode(session)
        try:
            require_execution_actor_in_session(
                session, actor, required_scopes=("app.read", "app.write", "app.run")
            )
        except ActorAuthorizationDenied:
            self._cancel(cancel_event)
            raise
        headers = _headers(session, lease["job_id"])
        if (
            headers is None
            or headers.kind != PROJECT_COPY_JOB_KIND
            or headers.session_id is not None
            or headers.workflow_run_id is not None
            or headers.project_id != headers.source_project_id
            or headers.copy_id != headers.id
        ):
            raise project_copy_error("copy_input_invalid")
        if any(incoming[key] != getattr(headers, key) for key in _HEADERS):
            raise project_copy_error("copy_input_invalid")
        source = session.execute(
            select(m.Project.workspace_id, m.Project.source_session_id).where(
                m.Project.id == headers.source_project_id,
                m.Project.id.in_(project_ids_for_user(actor.user_id, action=ProjectAction.EDIT)),
                credential_project_clause(actor),
            )
        ).one_or_none()
        destination = session.scalar(
            select(m.Workspace.id).where(
                m.Workspace.id == headers.destination_workspace_id,
                m.Workspace.id.in_(creatable_workspace_ids_for_user(actor.user_id)),
                credential_workspace_clause(actor),
            )
        )
        if source is None or destination is None:
            raise AccessDenied()
        if source.workspace_id != headers.workspace_id:
            raise project_copy_error("copy_input_invalid")
        _source(session, headers.source_project_id, headers.source_session_id)
        return actor

    def _possibly_committed(
        self, staged: StagedProjectFiles, copy_id: str, source_project_id: str
    ) -> bool:
        try:
            with self.database.snapshot_session() as session:
                _mode(session)
                root_id = copy_identity(copy_id, "projects", source_project_id)
                if session.scalar(select(m.Project.id).where(m.Project.id == root_id)) is not None:
                    return True
                paths = [file.relative_path for file in staged.files]
                for start in range(0, len(paths), 500):
                    if (
                        session.scalar(
                            select(m.Artifact.id)
                            .where(m.Artifact.relative_path.in_(paths[start : start + 500]))
                            .limit(1)
                        )
                        is not None
                    ):
                        return True
                receipt = session.execute(
                    select(m.Job.status, m.Job.result_json["copy_id"].as_string()).where(
                        m.Job.id == copy_id
                    )
                ).one_or_none()
                return bool(receipt and receipt[0] == "succeeded" and receipt[1] == copy_id)
        except Exception:
            return True

    def run(
        self,
        payload: dict[str, Any],
        progress: Callable[[float, str | None], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        staged: StagedProjectFiles | None = None
        body_error: Exception | None = None
        try:
            if type(payload) is not dict:
                raise project_copy_error("copy_input_invalid")
            actor, lease = current_execution_actor.get(), current_job_lease.get()
            if type(actor) is not UserActor or lease is None:
                raise AccessDenied()
            progress(0.0, "Preparing project copy")
            with self.database.snapshot_session() as session:
                actor = self._fence(session, payload, cancel_event)
                encoded = session.scalar(
                    select(m.Job.payload_json["snapshot_zlib_b64"].as_string()).where(
                        m.Job.id == lease["job_id"]
                    )
                )
                if encoded != payload["snapshot_zlib_b64"]:
                    raise project_copy_error("copy_input_invalid")
                snapshot = _decode_snapshot(encoded, payload["snapshot_sha256"])
                if (
                    snapshot.source_project_id != payload["source_project_id"]
                    or snapshot.destination_workspace_id != payload["destination_workspace_id"]
                    or snapshot.fingerprint != payload["source_fingerprint"]
                ):
                    raise project_copy_error("copy_input_invalid")
                validate_project_copy_snapshot(snapshot)
                validate_current_connections(
                    session,
                    snapshot.destination_workspace_id,
                    json.loads(snapshot.state_json).get("destination_connections", []),
                )
                _file_headers(session, actor, snapshot, interactive=False)
            checked_at = 0.0

            def check() -> None:
                nonlocal checked_at
                self._cancel(cancel_event)
                now = time.monotonic()
                if checked_at == 0.0 or now - checked_at >= 1.0:
                    with self.database.snapshot_session() as session:
                        self._fence(session, payload, cancel_event)
                    checked_at = time.monotonic()

            def copied(done: int, total: int) -> None:
                progress(0.1 + 0.75 * (done / total if total else 1.0), "Copying project files")

            staged = stage_project_files(
                self.paths,
                snapshot,
                copy_id=payload["copy_id"],
                artifact_ids={
                    file.artifact_id: copy_identity(
                        payload["copy_id"], "artifacts", file.artifact_id
                    )
                    for file in snapshot.files
                },
                check=check,
                progress=copied,
            )
            progress(0.95, "Publishing project copy")
            with self.database.immediate_session() as session:
                try:
                    actor = self._fence(session, payload, cancel_event)
                    _file_headers(session, actor, snapshot, interactive=False)
                    result = publish_project_copy_rows(
                        session,
                        snapshot,
                        staged,
                        copy_id=payload["copy_id"],
                        created_by_user_id=actor.user_id,
                    )
                    self.audit.record_command_in_session(
                        session,
                        AccessContext(actor),
                        action="project.copy",
                        resource_kind="project",
                        workspace_id=result.project_id,
                        metadata={
                            "source_project_id": snapshot.source_project_id,
                            "destination_workspace_id": snapshot.destination_workspace_id,
                            "fingerprint": snapshot.fingerprint,
                            "job_id": payload["copy_id"],
                        },
                    )
                    private = {**result.manifest(), "copy_id": payload["copy_id"]}
                    self.jobs.complete_in_session(
                        session,
                        payload["copy_id"],
                        lease["owner_id"],
                        private,
                        lease_generation=lease["lease_generation"],
                    )
                    job = session.get(m.Job, payload["copy_id"])
                    assert job is not None and isinstance(job.result_json, dict)
                    sealed = dict(job.result_json)
                except Exception as error:
                    body_error = error
                    raise
            return sealed
        except Exception as error:
            if staged is not None and (
                error is body_error
                or not self._possibly_committed(
                    staged, payload["copy_id"], payload["source_project_id"]
                )
            ):
                try:
                    cleanup_staged_project_files(self.paths, staged)
                except Exception:
                    # Preserve an uncertain/replaced attempt for the caller's
                    # recovery qualification; never expose an OS path in logs.
                    pass
            _raise_safe(error)
