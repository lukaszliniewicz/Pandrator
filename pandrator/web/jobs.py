"""SQLite-backed durable jobs and worker execution."""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, TypeGuard

from sqlalchemy import and_, delete, false, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from pandrator.logic.tts_service_identity import normalize_service_id

from .accounts.administration import PRIVILEGED_JOB_SCOPES, AdministratorRequired
from .accounts.execution_actor import (
    ExecutionActor,
    actor_from_storage,
    actor_to_storage,
    current_execution_actor,
    execution_actor_scope,
)
from .accounts.job_authorization import require_execution_actor_in_session
from .accounts.model_acquisition import model_acquisition_for_actor
from .credentials import SecretRedactor
from .database import Database, is_sqlite_busy
from .job_execution import current_job_lease
from .job_identity import job_creation_ownership
from .model_base import as_utc
from .model_maintenance import ensure_app_job_allowed
from .models import (
    AgentRun,
    GenerationRun,
    Job,
    JobActorRecord,
    JobEvent,
    QuickTranscription,
    ResourceClaim,
    SessionPurge,
    SessionRecord,
    utcnow,
)
from .output_storage import storage_effect_scope, storage_wait_detail
from .projects.copy_contracts import PROJECT_COPY_JOB_KIND, public_copy_job_result
from .resource_keys import canonical_resource_key
from .storage_admission import StoragePressure
from .training_state import reconcile_training_runs_in_session
from .update_maintenance import update_maintenance_active

_ATOMIC_COMPLETION_JOB_KINDS = frozenset({PROJECT_COPY_JOB_KIND, "subtitle.evidence"})

JobHandler = Callable[
    [dict[str, Any], Callable[[float, str | None], None], threading.Event],
    dict[str, Any] | None,
]
WORKER_LEASE_SECONDS = 30
WORKER_LEASE_SAFETY_SECONDS = 2
HEARTBEAT_WRITE_TIMEOUT_MS = 250


def _canonical_tts_resource_key(key: str) -> str:
    candidate = key.strip()
    prefix = "service:tts:"
    if candidate.startswith(prefix):
        return prefix + normalize_service_id(candidate.removeprefix(prefix))
    return key


class JobQueue:
    TERMINAL_EVENT_TYPES: ClassVar[set[str]] = {
        "job.succeeded",
        "job.failed",
        "job.canceled",
    }
    EVENT_SCOPE_IDS = (
        "generation_run_id",
        "output_assembly_id",
        "source_id",
        "source_asset_id",
        "source_artifact_id",
        "artifact_id",
        "agent_run_id",
        "training_id",
        "training_run_id",
        "voice_id",
        "sample_id",
        "upload_id",
        "document_id",
        "model_id",
    )
    GENERATION_ACTIVE_STATUSES = frozenset(
        {
            "queued",
            "running",
            "pausing",
            "pause_requested",
            "cancel_requested",
        }
    )
    GENERATION_JOB_TERMINAL_STATUS: ClassVar[dict[str, str]] = {
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
        "interrupted": "canceled",
    }
    AGENT_RUN_ACTIVE_STATUSES = frozenset({"queued", "running", "retrying"})
    AGENT_JOB_TERMINAL_STATUS: ClassVar[dict[str, str]] = {
        "succeeded": "failed",
        "failed": "failed",
        "canceled": "interrupted",
        "interrupted": "interrupted",
    }

    def __init__(
        self,
        database: Database,
        secret_redactor: SecretRedactor | None = None,
    ):
        self.database = database
        self.secret_redactor = secret_redactor or SecretRedactor(database)

    def redact_diagnostic(self, value: Any) -> Any:
        """Remove credential-shaped fields and known secret values from diagnostics."""

        return self.secret_redactor.redact_value(value)

    @staticmethod
    def _changed_entities(job: Job, event_type: str) -> list[str]:
        entities = {"jobs"}
        kind = str(job.kind or "")
        if event_type == "job.progress":
            # Audio takes are committed incrementally while generation jobs are
            # still running.  Advertise that resource change so an open review
            # drawer can replace placeholders with playable takes as soon as
            # each unit completes.  Other broad workflow resources remain
            # terminal-only to avoid progress-event fan-out.
            if (
                kind.startswith("generation.")
                or kind
                in {
                    "audiobook.generate_audio",
                    "dubbing.generate_audio",
                    "workflow.continue",
                }
                or (kind == "rvc.convert" and bool(job.session_id))
            ):
                entities.add("generation")
            return sorted(entities)

        if job.session_id:
            entities.add("workflow")
        if event_type in JobQueue.TERMINAL_EVENT_TYPES and (
            job.session_id or kind.startswith("session.")
        ):
            entities.add("sessions")
        if event_type == "job.succeeded" and kind == PROJECT_COPY_JOB_KIND:
            entities.add("sessions")
        if kind.startswith(("source.", "pdf.")):
            entities.add("sources")
        if (
            kind.startswith("generation.")
            or kind
            in {
                "text.prepare",
                "text.optimize_tts",
                "speech.prepare",
                "speech.performance",
                "audiobook.generate_audio",
                "dubbing.generate_audio",
                "workflow.continue",
            }
            or (kind == "rvc.convert" and bool(job.session_id))
        ):
            entities.add("generation")
        if kind.startswith("generation.assemble") or kind.startswith("export."):
            entities.add("output")
        if kind.startswith(("voice.", "tts.preview")):
            entities.add("voices")
        if kind.startswith("training."):
            entities.add("training")
        return sorted(entities)

    def _event(
        self, session, job_id: str, event_type: str, payload: dict | None = None
    ) -> JobEvent:
        if type(job_id) is not str or not job_id.strip():
            raise ValueError("A job event requires a job ID.")
        event_payload = (
            self.redact_diagnostic(payload) if isinstance(payload, dict) else {}
        )
        job = session.get(Job, job_id) if job_id else None
        if job is not None:
            job_payload = job.payload_json if isinstance(job.payload_json, dict) else {}
            for key in self.EVENT_SCOPE_IDS:
                value = job_payload.get(key)
                if isinstance(value, (str, int)) and value != "":
                    event_payload.setdefault(key, value)
            event_payload.update(
                {
                    "job_kind": job.kind,
                    "session_id": job.session_id,
                    "workflow_run_id": job.workflow_run_id,
                    "status": job.status,
                    "progress": float(job.progress or 0.0),
                    "changed_entities": self._changed_entities(job, event_type),
                }
            )
            if job.progress_detail and "detail" not in event_payload:
                event_payload["detail"] = job.progress_detail
        event_payload = self.redact_diagnostic(event_payload)
        event = JobEvent(
            job_id=job_id, event_type=event_type, payload_json=event_payload
        )
        session.add(event)
        return event

    def log(
        self,
        job_id: str,
        level: str,
        message: str,
        *,
        logger: str = "",
        trace: str = "",
        worker_id: str | None = None,
        lease_generation: int | None = None,
    ) -> None:
        """Persist a worker log record beside the durable job timeline."""
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            if worker_id is not None and (
                lease_generation is None
                or job.lease_owner != worker_id
                or job.lease_generation != lease_generation
            ):
                return
            self._event(
                session,
                job_id,
                "job.log",
                {
                    "level": str(level or "INFO").upper(),
                    "message": self.secret_redactor.redact(message),
                    "logger": str(logger or ""),
                    **({"trace": self.secret_redactor.redact(trace)} if trace else {}),
                },
            )

    @staticmethod
    def _stale_filter(now):
        return and_(
            Job.status.in_(("running", "cancel_requested")),
            or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
        )

    @staticmethod
    def _available_filter(now):
        return and_(
            Job.status == "queued",
            or_(Job.available_at.is_(None), Job.available_at <= now),
        )

    @staticmethod
    def _owns_lease(
        job: Job | None,
        worker_id: str,
        lease_generation: int,
        *,
        require_running: bool = True,
    ) -> TypeGuard[Job]:
        return bool(
            job
            and job.lease_owner == worker_id
            and job.lease_generation == lease_generation
            and (not require_running or job.status == "running")
            and job.lease_expires_at is not None
            and as_utc(job.lease_expires_at) > utcnow()
        )

    def _reconcile_stale_locked(self, session) -> None:
        """Close jobs whose worker lease vanished instead of leaving them running forever."""
        now = utcnow()
        records = list(
            session.scalars(select(Job).where(self._stale_filter(now))).all()
        )
        terminal_job_ids: list[str] = []
        for job in records:
            if job.status == "cancel_requested":
                job.status = "canceled"
                job.finished_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                self._event(
                    session, job.id, "job.canceled", {"reason": "worker_lease_expired"}
                )
                terminal_job_ids.append(job.id)
            elif job.attempts >= job.max_attempts or job.lease_expires_at is None:
                job.status = "failed"
                job.error_code = job.error_code or "worker_lease_expired"
                job.error_message = (
                    job.error_message or "The worker stopped before this job completed."
                )
                job.finished_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                self._event(
                    session,
                    job.id,
                    "job.failed",
                    {"code": job.error_code, "message": job.error_message},
                )
                terminal_job_ids.append(job.id)
        if terminal_job_ids:
            session.execute(
                delete(ResourceClaim).where(ResourceClaim.job_id.in_(terminal_job_ids))
            )
            self._reconcile_generation_runs_for_jobs_locked(
                session,
                terminal_job_ids,
            )
            self._reconcile_agent_runs_for_jobs_locked(session, terminal_job_ids)
            reconcile_training_runs_in_session(session, terminal_job_ids)

    @staticmethod
    def _is_generation_owner(run: GenerationRun, job: Job) -> bool:
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        root_id = str(payload.get("generation_run_id") or "")
        if job.kind == "generation.run":
            return bool(job.session_id == run.session_id and root_id == run.id)
        if job.kind == "generation.repair-existing":
            snapshot = (
                run.settings_snapshot_json
                if isinstance(run.settings_snapshot_json, dict)
                else {}
            )
            return bool(
                job.session_id == run.session_id
                and run.job_id == job.id
                and root_id
                and run.id != root_id
                and run.source_generation_run_id is not None
                and snapshot.get("early_repair_parent_run_id") == root_id
            )
        return False

    def _apply_generation_job_status(
        self,
        run: GenerationRun,
        job: Job,
    ) -> bool:
        """Project one safely linked terminal job onto its generation run."""

        status = self.GENERATION_JOB_TERMINAL_STATUS.get(str(job.status or ""))
        if status is None or not self._is_generation_owner(run, job):
            return False
        if job.kind == "generation.repair-existing" and status == "completed":
            # A successful repair worker finalizes each staged child itself.
            # A terminal queue success cannot fabricate missing audio for a
            # child that is still queued or running; fail that orphan instead.
            status = "failed"
        run.status = status
        run.cancel_requested = False
        run.pause_requested = False
        run.updated_at = utcnow()
        return True

    def _reconcile_generation_run_for_job_locked(
        self,
        session: Session,
        job: Job,
    ) -> None:
        """Reconcile only the domain record owned by a normal job transition."""

        if job.kind == "generation.repair-existing":
            # A post-run repair job owns the children it staged, not the
            # completed root named in its payload. Close every unfinished
            # child so a failure or cancellation cannot leave one queued.
            children = session.scalars(
                select(GenerationRun).where(
                    GenerationRun.job_id == job.id,
                    GenerationRun.status.in_(self.GENERATION_ACTIVE_STATUSES),
                )
            ).all()
            for child in children:
                self._apply_generation_job_status(child, job)
            return
        if job.kind != "generation.run":
            return
        run_id = str(
            (job.payload_json or {}).get("generation_run_id")
            if isinstance(job.payload_json, dict)
            else ""
        )
        if not run_id:
            return
        run = session.scalar(
            select(GenerationRun).where(
                GenerationRun.id == run_id,
                GenerationRun.job_id == job.id,
                GenerationRun.status.in_(self.GENERATION_ACTIVE_STATUSES),
            )
        )
        if run is not None:
            transitioned = self._apply_generation_job_status(run, job)
            if transitioned and run.resume_source_on_completion:
                from .generation_scheduling import release_interrupted_run

                release_interrupted_run(session, self, run)

    def _reconcile_generation_runs_for_jobs_locked(
        self,
        session: Session,
        job_ids: list[str],
    ) -> None:
        """Bulk-project a small set of terminal job transitions."""

        if not job_ids:
            return
        pairs = session.execute(
            select(GenerationRun, Job)
            .join(Job, GenerationRun.job_id == Job.id)
            .where(
                GenerationRun.job_id.in_(job_ids),
                GenerationRun.status.in_(self.GENERATION_ACTIVE_STATUSES),
            )
        ).all()
        for run, job in pairs:
            self._apply_generation_job_status(run, job)

    def _repair_generation_runs_locked(self, session: Session) -> None:
        """One startup repair for terminal jobs and every broken job link.

        Normal reads remain lock-free.  The outer join intentionally detects
        both NULL job IDs and dangling non-NULL IDs left by old databases or a
        partially restored backup.
        """

        pairs = session.execute(
            select(GenerationRun, Job)
            .outerjoin(Job, GenerationRun.job_id == Job.id)
            .where(GenerationRun.status.in_(self.GENERATION_ACTIVE_STATUSES))
        ).all()
        repaired_at = utcnow()
        for run, job in pairs:
            if job is None:
                run.status = (
                    "canceled" if run.status == "cancel_requested" else "failed"
                )
                run.cancel_requested = False
                run.pause_requested = False
                run.updated_at = repaired_at
                continue
            if self._apply_generation_job_status(run, job):
                continue
            if not self._is_generation_owner(run, job):
                # A non-generation job (or a mismatched session/payload) can
                # never drive this run. Keeping it active would strand the UI.
                run.status = "failed"
                run.cancel_requested = False
                run.pause_requested = False
                run.updated_at = repaired_at

    @staticmethod
    def _is_agent_run_owner(run: AgentRun, job: Job) -> bool:
        return bool(
            run.job_id == job.id
            and (job.session_id is None or job.session_id == run.session_id)
        )

    def _apply_agent_job_status(self, run: AgentRun, job: Job) -> bool:
        """Project a terminal job onto an unfinished durable agent run."""

        status = self.AGENT_JOB_TERMINAL_STATUS.get(str(job.status or ""))
        if status is None or not self._is_agent_run_owner(run, job):
            return False
        run.status = status
        if status == "failed" and not run.error_message:
            run.error_message = (
                job.error_message
                or "The job ended before the agentic operation was finalized."
            )
        run.updated_at = utcnow()
        return True

    def _reconcile_agent_runs_for_jobs_locked(
        self,
        session: Session,
        job_ids: list[str],
    ) -> None:
        if not job_ids:
            return
        pairs = session.execute(
            select(AgentRun, Job)
            .join(Job, AgentRun.job_id == Job.id)
            .where(
                AgentRun.job_id.in_(job_ids),
                AgentRun.status.in_(self.AGENT_RUN_ACTIVE_STATUSES),
            )
        ).all()
        for run, job in pairs:
            self._apply_agent_job_status(run, job)

    def _repair_agent_runs_locked(self, session: Session) -> None:
        """Make interrupted checkpoints resumable after a process crash."""

        pairs = session.execute(
            select(AgentRun, Job)
            .outerjoin(Job, AgentRun.job_id == Job.id)
            .where(AgentRun.status.in_(self.AGENT_RUN_ACTIVE_STATUSES))
        ).all()
        repaired_at = utcnow()
        for run, job in pairs:
            if job is None:
                run.status = "interrupted"
                run.error_message = (
                    run.error_message
                    or "The owning job disappeared before this operation completed."
                )
                run.updated_at = repaired_at
                continue
            if self._apply_agent_job_status(run, job):
                continue
            if not self._is_agent_run_owner(run, job):
                run.status = "failed"
                run.error_message = (
                    run.error_message
                    or "The operation was linked to a job from another session."
                )
                run.updated_at = repaired_at

    def reconcile(self) -> None:
        """Run the serialized startup repair once before serving requests."""

        with self.database.immediate_session() as session:
            self._reconcile_stale_locked(session)
            self._repair_generation_runs_locked(session)
            self._repair_agent_runs_locked(session)
            reconcile_training_runs_in_session(session)

    def _reconcile_stale(self) -> None:
        """Serialize the uncommon stale-job transition without locking normal reads."""
        now = utcnow()
        with self.database.session() as session:
            stale_id = session.scalar(
                select(Job.id).where(self._stale_filter(now)).limit(1)
            )
        if stale_id is None:
            return
        with self.database.immediate_session() as session:
            self._reconcile_stale_locked(session)

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        workflow_run_id: str | None = None,
        max_attempts: int = 1,
        resource_keys: list[str] | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> Job:
        with self.database.session() as session:
            job = self.enqueue_in_session(
                session,
                kind,
                payload,
                session_id=session_id,
                workflow_run_id=workflow_run_id,
                max_attempts=max_attempts,
                resource_keys=resource_keys,
                workspace_id=workspace_id,
                project_id=project_id,
            )
            session.flush()
            session.expunge(job)
            return job

    def enqueue_in_session(
        self,
        session: Session,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        workflow_run_id: str | None = None,
        max_attempts: int = 1,
        resource_keys: list[str] | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> Job:
        """Enqueue within a caller-owned transaction without committing it."""

        try:
            # Flush caller-owned roots, then acquire the writer even when that
            # flush had nothing to write. Resolve current authority only after
            # this fence, without inserting a temporarily unscoped job.
            session.flush()
            session.execute(
                update(Job).where(false()).values(id=Job.id),
                execution_options={"synchronize_session": False},
            )
            actor = current_execution_actor.get()
            actor_record = None
            if actor is not None:
                actor_record = actor_to_storage(actor)
                if actor_record is None:
                    raise ValueError("The current execution actor is invalid.")
            privileged_scopes = PRIVILEGED_JOB_SCOPES.get(kind)
            try:
                if privileged_scopes is not None:
                    require_execution_actor_in_session(
                        session,
                        actor,
                        required_scopes=privileged_scopes,
                        require_admin=True,
                        allow_host=True,
                    )
                elif actor is not None:
                    require_execution_actor_in_session(session, actor, allow_host=True)
            except PermissionError:
                if privileged_scopes is not None:
                    raise AdministratorRequired() from None
                raise
            ownership = job_creation_ownership(
                session,
                session_id=session_id,
                workflow_run_id=workflow_run_id,
                project_id=project_id,
                workspace_id=workspace_id,
            )
            job = Job(
                kind=kind,
                payload_json=payload or {},
                session_id=session_id,
                workflow_run_id=workflow_run_id,
                workspace_id=ownership.workspace_id,
                project_id=ownership.project_id,
                max_attempts=max(1, int(max_attempts)),
                resource_keys_json=sorted(
                    {
                        _canonical_tts_resource_key(str(key))
                        for key in (resource_keys or [])
                        if str(key).strip()
                    }
                ),
            )
            session.add(job)
            session.flush()
            if actor_record is not None:
                session.add(JobActorRecord(job_id=job.id, **actor_record))
            # Check after acquiring the SQLite writer lock, so an older read
            # cannot submit work after a purge has claimed the session.
            if session_id:
                owner = session.get(SessionRecord, session_id, populate_existing=True)
                if owner is None or owner.trashed_at is not None or owner.status == "purging":
                    raise ValueError(
                        "Restore the session before starting work; permanent deletion cannot be resumed as a session."
                    )
            # The flush has acquired the app SQLite writer transaction.  The
            # manager guard must run before the queue event and return so a busy
            # check raises into the caller's transaction and removes this job.
            ensure_app_job_allowed(self.database.path)
            self._event(
                session,
                job.id,
                "job.queued",
                {"kind": kind, "session_id": session_id},
            )
            session.flush()
        except Exception:
            # Admission shares the caller's transaction.  A rejected enqueue
            # must also undo the domain work that led to it, so a caller cannot
            # catch the error and commit a partial, unscoped job or its inputs.
            session.rollback()
            raise
        return job

    def acquire_resources(
        self,
        job_id: str,
        worker_id: str,
        keys: list[str],
        *,
        lease_generation: int,
        lease_seconds: int = 30,
    ) -> bool:
        normalized_keys = sorted(
            {_canonical_tts_resource_key(str(key).strip()) for key in keys if str(key).strip()}
        )
        with self.database.immediate_session() as session:
            now = utcnow()
            expires = now + timedelta(seconds=max(5, lease_seconds))
            job = session.scalar(
                select(Job).where(Job.id == job_id, Job.lease_expires_at > now)
            )
            if not self._owns_lease(job, worker_id, lease_generation):
                return False
            if not normalized_keys:
                return True
            conflict = session.scalar(
                select(ResourceClaim.resource_key)
                .where(
                    ResourceClaim.resource_key.in_(normalized_keys),
                    ResourceClaim.expires_at > now,
                    ResourceClaim.job_id != job_id,
                )
                .limit(1)
            )
            if conflict is not None:
                return False
            alias_filters = []
            if any(key.startswith("service:tts:") for key in normalized_keys):
                alias_filters.append(ResourceClaim.resource_key.like("service:tts:%"))
            if any(key.startswith("gpu:") for key in normalized_keys):
                alias_filters.append(ResourceClaim.resource_key.like("gpu:%"))
            if alias_filters:
                # Match legacy spellings without rewriting an active lease.
                identities = {canonical_resource_key(key) for key in normalized_keys}
                legacy_keys = session.scalars(
                    select(ResourceClaim.resource_key).where(
                        or_(*alias_filters),
                        ResourceClaim.expires_at > now,
                        ResourceClaim.job_id != job_id,
                    )
                )
                if any(canonical_resource_key(key) in identities for key in legacy_keys):
                    return False
            claims = {
                claim.resource_key: claim
                for claim in session.scalars(
                    select(ResourceClaim).where(ResourceClaim.resource_key.in_(normalized_keys))
                ).all()
            }
            for key in normalized_keys:
                claim = claims.get(key)
                if claim is None:
                    session.add(
                        ResourceClaim(
                            resource_key=key,
                            job_id=job_id,
                            lease_owner=worker_id,
                            lease_generation=lease_generation,
                            expires_at=expires,
                        )
                    )
                else:
                    claim.job_id = job_id
                    claim.lease_owner = worker_id
                    claim.lease_generation = lease_generation
                    claim.expires_at = expires
            return True

    def heartbeat_resources(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
        lease_seconds: int = 30,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        with self.database.immediate_session(busy_timeout_ms=HEARTBEAT_WRITE_TIMEOUT_MS) as session:
            now = utcnow()
            renewed_keys = list(session.scalars(
                update(ResourceClaim)
                .where(
                    ResourceClaim.job_id == job_id,
                    ResourceClaim.lease_owner == worker_id,
                    ResourceClaim.lease_generation == lease_generation,
                    ResourceClaim.expires_at > now,
                    select(Job.id)
                    .where(
                        Job.id == job_id,
                        Job.status == "running",
                        Job.lease_owner == worker_id,
                        Job.lease_generation == lease_generation,
                        Job.lease_expires_at > now,
                    )
                    .exists(),
                )
                .values(expires_at=now + timedelta(seconds=max(5, lease_seconds)))
                .returning(ResourceClaim.resource_key)
            ))
            if expected_keys is None:
                return bool(renewed_keys)
            required = {canonical_resource_key(str(key)) for key in expected_keys if str(key).strip()}
            renewed = {canonical_resource_key(str(key)) for key in renewed_keys}
            return required.issubset(renewed)

    def release_resources(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
    ) -> None:
        with self.database.session() as session:
            session.execute(
                delete(ResourceClaim).where(
                    ResourceClaim.job_id == job_id,
                    ResourceClaim.lease_owner == worker_id,
                    ResourceClaim.lease_generation == lease_generation,
                )
            )

    def defer_for_resources(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
        retry_delay_seconds: float | None = None,
    ) -> bool:
        with self.database.immediate_session() as session:
            job = session.get(Job, job_id)
            if not self._owns_lease(job, worker_id, lease_generation):
                return False
            delay = (
                random.uniform(0.05, 0.5)
                if retry_delay_seconds is None
                else max(0.01, min(float(retry_delay_seconds), 2.0))
            )
            now = utcnow()
            job.status = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
            job.attempts = max(0, job.attempts - 1)
            job.available_at = now + timedelta(seconds=delay)
            job.updated_at = now
            self._event(
                session,
                job.id,
                "job.waiting_for_resource",
                {
                    "resources": job.resource_keys_json,
                    "retry_after_ms": round(delay * 1000),
                },
            )
            return True

    def defer_for_storage(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
        detail: str,
        retry_delay_seconds: float = 60.0,
    ) -> bool:
        delay = float(retry_delay_seconds)
        if not math.isfinite(delay):
            raise ValueError("Storage retry delay must be finite.")
        delay = max(1.0, min(delay, 300.0))
        safe_detail = self.secret_redactor.redact(detail)

        def defer(session: Session) -> bool:
            # run_immediate has already acquired BEGIN IMMEDIATE, so this time
            # and the lease check fence the exact claim that is being released.
            now = utcnow()
            job = session.scalar(
                select(Job).where(
                    Job.id == job_id,
                    Job.status == "running",
                    Job.lease_owner == worker_id,
                    Job.lease_generation == lease_generation,
                    Job.lease_expires_at > now,
                )
            )
            if job is None:
                return False

            job.status = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
            job.attempts = max(0, job.attempts - 1)
            job.available_at = now + timedelta(seconds=delay)
            job.error_code = "storage_pressure"
            job.error_message = safe_detail
            job.progress_detail = safe_detail
            job.updated_at = now
            self._event(
                session,
                job.id,
                "job.waiting_for_storage",
                {"detail": safe_detail, "retry_after_ms": round(delay * 1000)},
            )
            return True

        return self.database.run_immediate(defer)

    def list(
        self, limit: int = 100, *, session_id: str | None = None, kind: str | None = None
    ) -> list[Job]:
        self._reconcile_stale()
        statement = select(Job)
        if session_id is not None:
            statement = statement.where(Job.session_id == session_id)
        if kind is not None:
            statement = statement.where(Job.kind == kind)
        with self.database.session() as session:
            jobs = list(
                session.scalars(
                    statement
                    .order_by(Job.created_at.desc())
                    .limit(max(1, min(limit, 500)))
                ).all()
            )
            for job in jobs:
                session.expunge(job)
            return jobs

    def get(self, job_id: str) -> Job:
        self._reconcile_stale()
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            session.expunge(job)
            return job

    def waiting_for_jobs(self, records: list[Job]) -> dict[str, dict[str, Any]]:
        """Return blockers for trusted internal callers."""
        with self.database.session() as session:
            return self.waiting_for_jobs_in_session(session, records)

    def waiting_for_jobs_in_session(
        self, session: Session, records: list[Job], *,
        visibility: ColumnElement[bool] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Select blockers in the caller's unit, optionally intersecting its policy."""
        waiters_by_resource: dict[str, list[str]] = {}
        for record in records:
            if record.status != "queued":
                continue
            raw_keys = record.resource_keys_json
            if not isinstance(raw_keys, list):
                continue
            keys = {
                canonical_resource_key(str(key))
                for key in raw_keys
                if isinstance(key, str) and key.strip()
            }
            if not keys:
                continue
            for key in keys:
                waiters_by_resource.setdefault(key, []).append(record.id)

        if not waiters_by_resource:
            return {}

        requested_keys = set(waiters_by_resource)
        resource_filter = ResourceClaim.resource_key.in_(requested_keys)
        if any(key.startswith("service:tts:") for key in requested_keys):
            # Older workers can still hold an alias spelling of a TTS service.
            resource_filter = or_(
                resource_filter,
                ResourceClaim.resource_key.like("service:tts:%"),
            )
        if any(key.startswith("gpu:") for key in requested_keys):
            resource_filter = or_(resource_filter, ResourceClaim.resource_key.like("gpu:%"))

        now = utcnow()
        waiting_for: dict[str, dict[str, Any]] = {}
        statement = (
            select(ResourceClaim, Job)
            .join(Job, Job.id == ResourceClaim.job_id)
            .where(
                resource_filter,
                ResourceClaim.expires_at > now,
                Job.lease_expires_at > now,
                Job.status.in_(("running", "cancel_requested")),
                ResourceClaim.lease_owner == Job.lease_owner,
                ResourceClaim.lease_generation == Job.lease_generation,
                Job.kind != "transcription.quick",
                ~select(QuickTranscription.id)
                .where(QuickTranscription.job_id == Job.id)
                .exists(),
            )
            .order_by(Job.created_at.asc(), Job.id.asc(), ResourceClaim.resource_key.asc())
        )
        if visibility is not None:
            statement = statement.where(visibility)
        with session.no_autoflush:
            claims = session.execute(statement).all()
        for claim, blocker in claims:
            resource_key = canonical_resource_key(claim.resource_key)
            for waiter_id in waiters_by_resource.get(resource_key, ()):
                if waiter_id == blocker.id or waiter_id in waiting_for:
                    continue
                waiting_for[waiter_id] = {
                    "id": blocker.id,
                    "kind": blocker.kind,
                    "progress_detail": blocker.progress_detail,
                }
        return waiting_for

    def _next_claimable_job_id(self, session: Session, now) -> str | None:
        """Skip occupied resources without spending attempts or rewriting waiters."""
        running_session_ids = select(Job.session_id).where(
            Job.status == "running",
            Job.session_id.is_not(None),
            Job.lease_expires_at > now,
        )
        statement = (
            select(Job.id, Job.resource_keys_json)
            .where(
                or_(
                    self._available_filter(now),
                    and_(Job.status == "running", Job.lease_expires_at <= now),
                ),
                or_(Job.session_id.is_(None), Job.session_id.not_in(running_session_ids)),
                Job.attempts < Job.max_attempts,
            )
            .order_by(Job.created_at.asc(), Job.id.asc())
        )
        occupied: dict[str, set[str]] = {}
        for key, owner_job_id in session.execute(
            select(ResourceClaim.resource_key, ResourceClaim.job_id).where(
                ResourceClaim.expires_at > now
            )
        ):
            occupied.setdefault(canonical_resource_key(key), set()).add(owner_job_id)
        if not occupied:
            return session.scalar(statement.limit(1))
        candidates = session.execute(statement.execution_options(yield_per=128))
        try:
            for job_id, keys in candidates:
                if all(
                    occupied.get(canonical_resource_key(str(key)), set())
                    .issubset({job_id})
                    for key in (keys or [])
                ):
                    return job_id
        finally:
            candidates.close()
        return None

    def claim(self, worker_id: str, lease_seconds: int = 30) -> Job | None:
        if update_maintenance_active(self.database.path.parent):
            return None
        now = utcnow()
        with self.database.session() as session:
            if session.scalar(
                select(Job.id)
                .where(or_(self._available_filter(now), self._stale_filter(now)))
                .limit(1)
            ) is None:
                return None
            potentially_available = self._next_claimable_job_id(session, now)
            if potentially_available is None:
                # Expired owners still need reconciliation even when their
                # resource claims temporarily hide all queued candidates.
                potentially_available = session.scalar(
                    select(Job.id).where(self._stale_filter(now)).limit(1)
                )
        if potentially_available is None:
            return None
        with self.database.immediate_session() as session:
            if update_maintenance_active(self.database.path.parent):
                return None
            now = utcnow()
            self._reconcile_stale_locked(session)
            session.flush()
            candidate_id = self._next_claimable_job_id(session, now)
            job = session.get(Job, candidate_id) if candidate_id is not None else None
            if job is None:
                return None
            reclaimed = job.status == "running"
            if job.error_code == "storage_pressure":
                job.error_code = None
                job.error_message = None
                job.progress_detail = None
            job.status = "running"
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
            job.lease_generation = int(job.lease_generation or 0) + 1
            job.available_at = None
            job.attempts += 1
            job.started_at = job.started_at or now
            job.updated_at = now
            self._event(
                session,
                job.id,
                "job.reclaimed" if reclaimed else "job.started",
                {
                    "worker_id": worker_id,
                    "lease_generation": job.lease_generation,
                },
            )
            session.flush()
            session.expunge(job)
            return job

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
        progress: float | None = None,
        detail: str | None = None,
        lease_seconds: int = 30,
    ) -> bool:
        with self.database.immediate_session(
            busy_timeout_ms=HEARTBEAT_WRITE_TIMEOUT_MS
        ) as session:
            now = utcnow()
            job = session.scalar(select(Job).where(
                Job.id == job_id, Job.lease_expires_at > now,
            ))
            if not self._owns_lease(job, worker_id, lease_generation):
                return False
            job.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
            job.updated_at = now
            payload: dict[str, Any] = {}
            progress_was_current = True
            if progress is not None:
                requested_progress = max(0.0, min(1.0, float(progress)))
                # Concurrent child workers may finish close together. A late
                # callback from an earlier unit must never move the bar back.
                progress_was_current = requested_progress >= float(job.progress or 0.0)
                job.progress = max(float(job.progress or 0.0), requested_progress)
                payload["progress"] = job.progress
            if detail is not None and progress_was_current:
                job.progress_detail = self.secret_redactor.redact(detail)
                payload["detail"] = job.progress_detail
            if payload:
                self._event(session, job.id, "job.progress", payload)
            return True

    def owns_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
    ) -> bool:
        with self.database.session() as session:
            return self._owns_lease(
                session.get(Job, job_id),
                worker_id,
                lease_generation,
            )

    def is_current_generation(
        self,
        job_id: str,
        *,
        lease_generation: int,
    ) -> bool:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            return bool(job and job.lease_generation == lease_generation)

    def should_cancel(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
    ) -> bool:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            return bool(
                job
                and job.lease_generation == lease_generation
                and job.status in {"cancel_requested", "canceled"}
                and (job.lease_owner in {None, worker_id})
            )

    def request_cancel_in_session(self, session: Session, job_id: str) -> Job:
        """Cancel a job and any safely linked generation child in one transaction."""

        job = session.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        if job.kind == "maintenance.session_purge":
            target = (job.payload_json or {}).get("target_session_id")
            journal = session.get(SessionPurge, target) if isinstance(target, str) else None
            if journal is not None and journal.state in {"purging", "failed", "complete"}:
                raise ValueError("Session purge has claimed irreversible deletion and cannot be cancelled.")
        if job.status == "queued":
            job.status = "canceled"
            job.finished_at = utcnow()
            job.error_code = None
            job.error_message = None
            job.available_at = None
            job.progress_detail = None
            event_type = "job.canceled"
        elif job.status in {"running", "cancel_requested"}:
            # The lease owner must acknowledge cancellation.  Clearing its
            # lease here used to make that acknowledgement impossible and left
            # linked generation runs in cancel_requested forever.
            job.status = "cancel_requested"
            event_type = "job.cancel_requested"
        else:
            event_type = "job.cancel_ignored"
        job.updated_at = utcnow()
        self._event(session, job.id, event_type)
        self._reconcile_generation_run_for_job_locked(session, job)
        self._reconcile_agent_runs_for_jobs_locked(session, [job.id])
        reconcile_training_runs_in_session(session, [job_id])
        if job.kind == "generation.admit" and isinstance(job.result_json, dict):
            marker = job.result_json
            run_id = str(marker.get("generation_run_id") or "")
            child_id = str(marker.get("job_id") or "")
            if run_id and child_id and job.session_id:
                run = session.get(GenerationRun, run_id)
                child = session.get(Job, child_id)
                if (
                    run is not None
                    and run.job_id == child_id
                    and run.session_id == job.session_id
                    and child is not None
                    and child.kind == "generation.run"
                    and child.session_id == job.session_id
                    and child.status in {"queued", "running", "cancel_requested"}
                    and isinstance(child.payload_json, dict)
                    and child.payload_json.get("generation_run_id") == run_id
                ):
                    self.request_cancel_in_session(session, child_id)
        session.flush()
        return job

    def request_cancel(self, job_id: str) -> Job:
        with self.database.immediate_session() as session:
            job = self.request_cancel_in_session(session, job_id)
            session.expunge(job)
            return job

    def complete(
        self,
        job_id: str,
        worker_id: str,
        result: dict[str, Any] | None = None,
        *,
        lease_generation: int,
    ) -> None:
        with self.database.immediate_session() as session:
            self.complete_in_session(
                session, job_id, worker_id, result, lease_generation=lease_generation
            )

    def complete_in_session(
        self,
        session: Session,
        job_id: str,
        worker_id: str,
        result: dict[str, Any] | None = None,
        *,
        lease_generation: int,
    ) -> None:
        """Seal domain publication and terminal success in one caller writer."""
        job = session.get(Job, job_id, populate_existing=True)
        if job is None:
            raise RuntimeError("Job lease is no longer owned by this worker.")
        # A handler may have sealed its output atomically before returning to
        # Worker. This duplicate acknowledgement has no effects or new event.
        if (
            job.kind in _ATOMIC_COMPLETION_JOB_KINDS
            and job.status == "succeeded"
            and job.lease_generation == lease_generation
        ):
            if job.result_json == self.redact_diagnostic(result or {}):
                return
            raise RuntimeError("Job lease is no longer owned by this worker.")
        if not self._owns_lease(job, worker_id, lease_generation):
            raise RuntimeError("Job lease is no longer owned by this worker.")
        safe_result = self.redact_diagnostic(result or {})
        job.status = "succeeded"
        job.progress = 1.0
        job.result_json = safe_result
        job.error_code = None
        job.error_message = None
        retry_detail = f"Retry scheduled after attempt {job.attempts - 1} of {job.max_attempts}"
        if job.progress_detail == retry_detail:
            job.progress_detail = None
        job.lease_owner = None
        job.lease_expires_at = None
        finished_at = utcnow()
        job.finished_at = finished_at
        job.updated_at = finished_at
        self._event(
            session, job.id, "job.succeeded",
            public_copy_job_result(job.id) if job.kind == PROJECT_COPY_JOB_KIND else job.result_json,
        )
        self._reconcile_generation_run_for_job_locked(session, job)
        self._reconcile_agent_runs_for_jobs_locked(session, [job.id])
        reconcile_training_runs_in_session(session, [job_id])

    def fail(
        self,
        job_id: str,
        worker_id: str,
        code: str,
        message: str,
        *,
        lease_generation: int,
        trace: str | None = None,
    ) -> bool:
        with self.database.immediate_session() as session:
            job = session.get(Job, job_id)
            if not self._owns_lease(job, worker_id, lease_generation):
                return False
            retry = job.attempts < job.max_attempts
            job.status = "queued" if retry else "failed"
            if retry:
                job.progress = 0.0
                job.progress_detail = f"Retry scheduled after attempt {job.attempts} of {job.max_attempts}"
            safe_code = self.secret_redactor.redact(code)
            safe_message = self.secret_redactor.redact(message)
            safe_trace = self.secret_redactor.redact(trace) if trace else ""
            job.error_code = safe_code
            job.error_message = safe_message
            job.lease_owner = None
            job.lease_expires_at = None
            job.finished_at = None if retry else utcnow()
            job.updated_at = utcnow()
            self._event(
                session,
                job.id,
                "job.retry_scheduled" if retry else "job.failed",
                {"code": safe_code, "message": safe_message, "trace": safe_trace},
            )
            if not retry:
                self._reconcile_generation_run_for_job_locked(session, job)
                self._reconcile_agent_runs_for_jobs_locked(session, [job.id])
                reconcile_training_runs_in_session(session, [job_id])
            else:
                # Handlers persist their own failure before the queue schedules
                # another attempt. Keep the run non-resumable while that retry
                # is already pending, then the next handler invocation will
                # restore its accepted checkpoints.
                session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.job_id == job.id,
                        AgentRun.status.in_(
                            (*self.AGENT_RUN_ACTIVE_STATUSES, "failed", "interrupted")
                        ),
                    )
                    .values(status="retrying", updated_at=utcnow())
                )
            return True

    def cancel_owned(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_generation: int,
    ) -> bool:
        with self.database.immediate_session() as session:
            job = session.get(Job, job_id)
            if not self._owns_lease(
                job,
                worker_id,
                lease_generation,
                require_running=False,
            ) or job.status not in {"running", "cancel_requested"}:
                return False
            job.status = "canceled"
            job.lease_owner = None
            job.lease_expires_at = None
            finished_at = utcnow()
            job.finished_at = finished_at
            job.updated_at = finished_at
            self._event(session, job.id, "job.canceled")
            self._reconcile_generation_run_for_job_locked(session, job)
            self._reconcile_agent_runs_for_jobs_locked(session, [job.id])
            reconcile_training_runs_in_session(session, [job_id])
            return True

    def events_after(self, event_id: int = 0, limit: int = 250) -> list[JobEvent]:
        with self.database.session() as session:
            events = list(
                session.scalars(
                    select(JobEvent)
                    .where(JobEvent.id > max(0, event_id))
                    .order_by(JobEvent.id.asc())
                    .limit(limit)
                ).all()
            )
            for event in events:
                session.expunge(event)
            return events

    def event_bounds(self) -> dict[str, int]:
        """Return the retained event window without loading event payloads."""
        with self.database.session() as session:
            oldest, latest = session.execute(
                select(func.min(JobEvent.id), func.max(JobEvent.id))
            ).one()
        return {
            "oldest": int(oldest or 0),
            "latest": int(latest or 0),
        }

    def events_for(self, job_id: str, limit: int = 1000) -> list[JobEvent]:
        with self.database.session() as session:
            if session.get(Job, job_id) is None:
                raise KeyError(job_id)
            events = list(
                session.scalars(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id)
                    .order_by(JobEvent.id.desc())
                    .limit(max(1, min(int(limit), 5000)))
                ).all()
            )
            events.reverse()
            for event in events:
                session.expunge(event)
            return events


_JOB_LOG_CAPTURE_LOCK = threading.Lock()
_job_log_capture_count = 0
_job_log_previous_level = logging.NOTSET
_job_log_managed_level = False


@contextmanager
def _capture_job_logs(handler: logging.Handler) -> Iterator[None]:
    """Share root INFO admission and restore it only after the last capture."""
    global _job_log_capture_count, _job_log_previous_level, _job_log_managed_level
    root = logging.getLogger()
    with _JOB_LOG_CAPTURE_LOCK:
        if _job_log_capture_count == 0:
            _job_log_previous_level = root.level
            logging.basicConfig(level=min(root.level, logging.INFO))
            _job_log_managed_level = _job_log_previous_level > logging.INFO
            if _job_log_managed_level:
                root.setLevel(logging.INFO)
        _job_log_capture_count += 1
        root.addHandler(handler)
    try:
        yield
    finally:
        with _JOB_LOG_CAPTURE_LOCK:
            root.removeHandler(handler)
            _job_log_capture_count -= 1
            if _job_log_capture_count == 0:
                # Respect an external level change made during capture.
                if _job_log_managed_level and root.level == logging.INFO:
                    root.setLevel(_job_log_previous_level)
                _job_log_managed_level = False


@dataclass(frozen=True, slots=True)
class _PendingJobLog:
    level: str
    message: str
    logger: str
    trace: str
    emitted_at: str


class _JobLogHandler(logging.Handler):
    """Buffer only this handler thread's logs; emitting never writes to SQLite.

    Child-thread diagnostics require explicit attribution in their own logging
    port. Thread attribution is deliberately not inherited from a parent worker.
    """

    MAX_RECORDS = 256
    MAX_TEXT_BYTES = 8192

    def __init__(
        self,
        queue: JobQueue,
        job_id: str,
        worker_id: str,
        lease_generation: int,
    ):
        super().__init__(level=logging.INFO)
        self.queue = queue
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_generation = lease_generation
        self.owner_thread_id = threading.get_ident()
        self._pending: deque[_PendingJobLog] = deque()
        self._pending_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._dropped = 0
        self._dropped_at = ""
        self._persistence_errors = 0
        self._last_error_type = ""

    def _bounded_text(self, value: object) -> str:
        safe = self.queue.secret_redactor.redact(value)
        return safe.encode("utf-8")[: self.MAX_TEXT_BYTES].decode("utf-8", errors="ignore")

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.owner_thread_id:
            return
        try:
            trace = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
            pending = _PendingJobLog(
                self._bounded_text(record.levelname),
                self._bounded_text(record.getMessage()),
                self._bounded_text(record.name),
                self._bounded_text(trace),
                datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            )
            with self._pending_lock:
                if len(self._pending) < self.MAX_RECORDS:
                    self._pending.append(pending)
                else:
                    self._dropped += 1
                    self._dropped_at = self._dropped_at or pending.emitted_at
        except Exception as error:
            # Never retain unsanitized text or invoke logging/handleError here.
            # The loss remains observable through the next durable summary.
            with self._pending_lock:
                self._dropped += 1
                self._dropped_at = self._dropped_at or utcnow().isoformat()
                self._persistence_errors += 1
                self._last_error_type = type(error).__name__[:100]

    def flush_pending(self, max_records: int = 20, *, deadline: float | None = None) -> int:
        """Persist one fenced batch with zero writer wait; retain it on failure.

        Terminal draining may wait for an already active flush, within its
        existing deadline. Monitor flushing never waits on another flusher.
        """
        if max_records <= 0:
            return 0
        wait = max(0.0, deadline - time.monotonic()) if deadline is not None else 0.0
        if not self._flush_lock.acquire(timeout=wait):
            return 0
        try:
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            limit = min(int(max_records), 20)
            with self._pending_lock:
                dropped, errors = self._dropped, self._persistence_errors
                batch: list[_PendingJobLog] = []
                if dropped:
                    batch.append(
                        _PendingJobLog(
                            "WARNING",
                            f"Job log capture dropped {dropped} record(s).",
                            __name__,
                            "",
                            self._dropped_at,
                        )
                    )
                if errors and len(batch) < limit:
                    batch.append(
                        _PendingJobLog(
                            "WARNING",
                            f"Job log capture recovered after {errors} error(s) ({self._last_error_type}).",
                            __name__,
                            "",
                            utcnow().isoformat(),
                        )
                    )
                else:
                    errors = 0
                records = list(self._pending)[: max(0, limit - len(batch))]
                batch.extend(records)
            if not batch:
                return 0
            try:
                with self.queue.database.immediate_session(busy_timeout_ms=0) as session:
                    job = session.get(Job, self.job_id)
                    if (
                        job is None
                        or job.lease_owner != self.worker_id
                        or job.lease_generation != self.lease_generation
                    ):
                        return 0
                    common: dict[str, Any] | None = None
                    log_fields = {"level", "message", "logger", "trace", "emitted_at"}
                    for pending in batch:
                        payload = {
                            "level": pending.level,
                            "message": pending.message,
                            "logger": pending.logger,
                            "emitted_at": pending.emitted_at,
                            **({"trace": pending.trace} if pending.trace else {}),
                        }
                        if common is None:
                            event = self.queue._event(session, self.job_id, "job.log", payload)
                            # Job context is invariant within this fenced writer.
                            # Redact it once per batch; keep each record's fields
                            # independently redacted against current credentials.
                            common = {
                                key: value
                                for key, value in event.payload_json.items()
                                if key not in log_fields
                            }
                        else:
                            session.add(
                                JobEvent(
                                    job_id=self.job_id,
                                    event_type="job.log",
                                    payload_json={
                                        **common,
                                        **self.queue.redact_diagnostic(payload),
                                    },
                                )
                            )
            except Exception as error:
                if not is_sqlite_busy(error):
                    # Bound the diagnostic and report recovery durably; never
                    # recursively log a storage failure or fail the job for it.
                    with self._pending_lock:
                        self._persistence_errors += 1
                        self._last_error_type = type(error).__name__[:100]
                return 0
            with self._pending_lock:
                for _ in records:
                    self._pending.popleft()
                self._dropped -= dropped
                self._persistence_errors -= errors
                if not self._dropped:
                    self._dropped_at = ""
            return len(batch)
        finally:
            self._flush_lock.release()


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        worker_id: str,
        handlers: Mapping[str, JobHandler] | None = None,
    ):
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = dict(handlers or {})
        self.stop_event = threading.Event()

    def register(self, kind: str, handler: JobHandler) -> None:
        self.handlers[kind] = handler

    def stop(self) -> None:
        self.stop_event.set()

    def _report_exception(self, context: str, error: BaseException) -> tuple[str, str]:
        """Log the active exception without exposing configured credentials."""

        safe_context = self.queue.secret_redactor.redact(context)
        safe_message = self.queue.secret_redactor.redact(error)
        safe_trace = self.queue.secret_redactor.redact(traceback.format_exc())
        logging.error("%s: %s\n%s", safe_context, safe_message, safe_trace)
        return safe_message, safe_trace

    def _update_agent_run_status(
        self,
        job: Job,
        status: str,
        lease_generation: int,
    ) -> None:
        """Record terminal state for this claim's active runs without reading bodies."""
        with self.queue.database.immediate_session() as session:
            now = utcnow()
            current_job = session.execute(
                select(Job.lease_generation, Job.lease_owner, Job.session_id).where(
                    Job.id == job.id,
                    Job.lease_expires_at > now,
                    Job.status.in_({"running", "cancel_requested"}),
                )
            ).one_or_none()
            if (
                current_job is None
                or current_job.lease_generation != lease_generation
                or current_job.lease_owner != self.worker_id
                or current_job.session_id != job.session_id
            ):
                return
            # The stored job link, not a payload-selected ID, owns bookkeeping.
            # A job may create several phase runs after its immutable enqueue.
            session.execute(
                update(AgentRun)
                .where(
                    AgentRun.job_id == job.id,
                    AgentRun.session_id == current_job.session_id,
                    AgentRun.status.in_({"queued", "running", "retrying"}),
                )
                .values(status=status, updated_at=now)
                .execution_options(synchronize_session=False)
            )

    def _execution_actor_for_job(self, job_id: str) -> ExecutionActor | None:
        """Read and strictly decode only this job's private attribution row."""

        with self.queue.database.snapshot_session() as session:
            record = session.get(JobActorRecord, job_id)
            if record is None:
                return None
            actor = actor_from_storage(
                record.user_id,
                record.auth_revision,
                record.context_json,
            )
            if actor is None:
                raise ValueError("The job actor record is invalid.")
            return actor

    def _run_claimed(
        self,
        job: Job,
        handler: JobHandler,
        lease_generation: int,
    ) -> None:
        cancel_event = threading.Event()
        lease_lost_event = threading.Event()
        monitor_stop = threading.Event()
        log_handler = _JobLogHandler(
            self.queue,
            job.id,
            self.worker_id,
            lease_generation,
        )
        if job.kind == "transcription.quick":
            # ASR diagnostics can contain recognized speech. Temporary work
            # must not copy those diagnostics into durable job logs.
            log_handler.addFilter(lambda _record: False)
        log_capture = ExitStack()
        log_capture.enter_context(_capture_job_logs(log_handler))

        def flush_terminal_logs() -> None:
            # A bounded drain outside the handler's writer, before terminal
            # fencing clears ownership. Busy/error stops immediately. During
            # DB failure persistence (including loss summaries) is best effort.
            deadline = time.monotonic() + 0.5
            for _ in range((log_handler.MAX_RECORDS + 2 + 19) // 20):
                if time.monotonic() >= deadline or not log_handler.flush_pending(deadline=deadline):
                    break

        last_successful_heartbeat = time.monotonic()

        def renew_lease(
            *, progress_value: float | None = None, progress_detail: str | None = None
        ) -> bool | None:
            nonlocal last_successful_heartbeat
            try:
                renewed = self.queue.heartbeat(
                    job.id,
                    self.worker_id,
                    lease_generation=lease_generation,
                    progress=progress_value,
                    detail=progress_detail,
                    lease_seconds=WORKER_LEASE_SECONDS,
                )
                if not renewed:
                    lease_lost_event.set()
                    cancel_event.set()
                    return False
                expected_resources = {
                    _canonical_tts_resource_key(str(key).strip())
                    for key in (job.resource_keys_json or []) if str(key).strip()
                }
                resources_renewed = self.queue.heartbeat_resources(
                    job.id,
                    self.worker_id,
                    lease_generation=lease_generation,
                    lease_seconds=WORKER_LEASE_SECONDS,
                    expected_keys=sorted(expected_resources),
                )
                if expected_resources and not resources_renewed:
                    lease_lost_event.set()
                    cancel_event.set()
                    return False
                # Keep the grace clock aligned with the full job and resource lease.
                last_successful_heartbeat = time.monotonic()
                return True
            except Exception as error:
                if is_sqlite_busy(error):
                    if time.monotonic() - last_successful_heartbeat < (
                        WORKER_LEASE_SECONDS - WORKER_LEASE_SAFETY_SECONDS
                    ):
                        return None
                    lease_lost_event.set()
                    cancel_event.set()
                    return False
                raise

        def monitor() -> None:
            """Keep leases alive and observe cancellation even during quiet handlers."""
            heartbeat_at = 0.0
            try:
                while not monitor_stop.wait(0.2):
                    if self.queue.should_cancel(
                        job.id,
                        self.worker_id,
                        lease_generation=lease_generation,
                    ):
                        cancel_event.set()
                        return
                    now = time.monotonic()
                    if now >= heartbeat_at:
                        renewed = renew_lease()
                        if renewed is False:
                            return
                        if renewed is True:
                            log_handler.flush_pending()
                        heartbeat_at = now + (0.2 if renewed is None else 5.0)
            except Exception as error:
                self._report_exception(f"Worker monitor failed for job {job.id}", error)
                lease_lost_event.set()
                cancel_event.set()

        monitor_thread = threading.Thread(
            target=monitor,
            name=f"job-monitor-{job.id[:8]}",
            daemon=True,
        )

        def progress(value: float, detail: str | None = None) -> None:
            if self.queue.should_cancel(
                job.id,
                self.worker_id,
                lease_generation=lease_generation,
            ):
                cancel_event.set()
            renew_lease(progress_value=value, progress_detail=detail)

        try:
            execution_actor = self._execution_actor_for_job(job.id)
            lease_token = current_job_lease.set(
                {
                    "job_id": job.id,
                    "owner_id": self.worker_id,
                    "lease_generation": lease_generation,
                }
            )
            try:
                with (
                    execution_actor_scope(execution_actor),
                    model_acquisition_for_actor(self.queue.database),
                ):
                    privileged_scopes = PRIVILEGED_JOB_SCOPES.get(job.kind)
                    if privileged_scopes is not None:
                        with self.queue.database.immediate_session() as session:
                            try:
                                require_execution_actor_in_session(
                                    session,
                                    execution_actor,
                                    required_scopes=privileged_scopes,
                                    require_admin=True,
                                    allow_host=True,
                                )
                            except PermissionError:
                                raise AdministratorRequired() from None
                    monitor_thread.start()
                    # Handlers occasionally create child domain records (for
                    # example a GenerationRun) while executing inside a durable
                    # job. Pass the owning job ID as internal context so those
                    # records can expose accurate status and cancellation.
                    if not isinstance(job.payload_json, dict):
                        raise TypeError("Job payload must be a JSON object.")
                    handler_payload = dict(job.payload_json)
                    handler_payload["_job_id"] = job.id
                    handler_payload["_lease_generation"] = lease_generation
                    with storage_effect_scope(self.queue.database):
                        result = handler(handler_payload, progress, cancel_event)
            finally:
                current_job_lease.reset(lease_token)
            flush_terminal_logs()
            canceled = self.queue.should_cancel(
                job.id,
                self.worker_id,
                lease_generation=lease_generation,
            )
            if canceled:
                self._update_agent_run_status(job, "canceled", lease_generation)
                self.queue.cancel_owned(
                    job.id,
                    self.worker_id,
                    lease_generation=lease_generation,
                )
            elif lease_lost_event.is_set():
                self._update_agent_run_status(job, "failed", lease_generation)
                self.queue.fail(
                    job.id,
                    self.worker_id,
                    "worker_lease_lost",
                    "The worker could not renew its job lease.",
                    lease_generation=lease_generation,
                )
            else:
                self.queue.complete(
                    job.id,
                    self.worker_id,
                    result,
                    lease_generation=lease_generation,
                )
        except StoragePressure as error:
            canceled = self.queue.should_cancel(
                job.id,
                self.worker_id,
                lease_generation=lease_generation,
            )
            if canceled:
                safe_message = self.queue.secret_redactor.redact(error)
                logging.warning(
                    "Worker job %s stopped after cancellation: %s",
                    job.id,
                    safe_message,
                )
                flush_terminal_logs()
                self._update_agent_run_status(job, "canceled", lease_generation)
                self.queue.cancel_owned(
                    job.id,
                    self.worker_id,
                    lease_generation=lease_generation,
                )
            elif not lease_lost_event.is_set():
                flush_terminal_logs()
                try:
                    self.queue.defer_for_storage(
                        job.id,
                        self.worker_id,
                        lease_generation=lease_generation,
                        detail=storage_wait_detail(error),
                    )
                except Exception as recovery_error:
                    self._report_exception(
                        f"Worker could not defer job {job.id} after storage pressure",
                        recovery_error,
                    )
        except Exception as error:
            safe_message = self.queue.secret_redactor.redact(error)
            safe_trace = self.queue.secret_redactor.redact(traceback.format_exc())
            canceled = self.queue.should_cancel(
                job.id,
                self.worker_id,
                lease_generation=lease_generation,
            )
            if canceled:
                logging.warning(
                    "Worker job %s stopped after cancellation: %s",
                    job.id,
                    safe_message,
                )
            else:
                logging.error(
                    "Worker job %s failed: %s\n%s", job.id, safe_message, safe_trace
                )
            flush_terminal_logs()
            self._update_agent_run_status(
                job,
                "canceled" if canceled else "failed",
                lease_generation,
            )
            if canceled:
                self.queue.cancel_owned(
                    job.id,
                    self.worker_id,
                    lease_generation=lease_generation,
                )
            else:
                self.queue.fail(
                    job.id,
                    self.worker_id,
                    type(error).__name__,
                    safe_message,
                    lease_generation=lease_generation,
                    trace=safe_trace,
                )
        finally:
            monitor_stop.set()
            if monitor_thread.ident is not None:
                monitor_thread.join(timeout=1.0)
            log_handler.flush_pending()
            log_capture.close()

    def run_once(self) -> bool:
        """Claim and run at most one job without letting infrastructure errors escape."""
        job: Job | None = None
        lease_generation: int | None = None
        resources_acquired = False
        try:
            job = self.queue.claim(self.worker_id)
            if job is None:
                return False
            lease_generation = int(job.lease_generation)
            if not self.queue.acquire_resources(
                job.id,
                self.worker_id,
                list(job.resource_keys_json or []),
                lease_generation=lease_generation,
            ):
                self.queue.defer_for_resources(
                    job.id,
                    self.worker_id,
                    lease_generation=lease_generation,
                )
                return False
            resources_acquired = True

            handler = self.handlers.get(job.kind)
            if handler is None:
                self.queue.fail(
                    job.id,
                    self.worker_id,
                    "unknown_job_kind",
                    f"No handler is registered for '{job.kind}'.",
                    lease_generation=lease_generation,
                )
                return True

            self._run_claimed(job, handler, lease_generation)
            return True
        except Exception as error:
            safe_message, safe_trace = self._report_exception(
                "Worker infrastructure failure",
                error,
            )
            if job is not None and lease_generation is not None:
                try:
                    if resources_acquired:
                        self.queue.fail(
                            job.id,
                            self.worker_id,
                            "worker_infrastructure_error",
                            safe_message,
                            lease_generation=lease_generation,
                            trace=safe_trace,
                        )
                    else:
                        self.queue.defer_for_resources(
                            job.id,
                            self.worker_id,
                            lease_generation=lease_generation,
                        )
                except Exception as recovery_error:
                    self._report_exception(
                        f"Worker could not recover job {job.id} after an infrastructure failure",
                        recovery_error,
                    )
            return job is not None
        finally:
            if resources_acquired and job is not None and lease_generation is not None:
                try:
                    self.queue.release_resources(
                        job.id,
                        self.worker_id,
                        lease_generation=lease_generation,
                    )
                except Exception as error:
                    self._report_exception(
                        f"Worker could not release resources for job {job.id}",
                        error,
                    )

    def run_forever(
        self,
        poll_interval: float = 0.5,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        while not self.stop_event.is_set():
            if stop_requested is not None and stop_requested():
                break
            try:
                processed = self.run_once()
            except Exception as error:
                # Defense in depth: a future regression in run_once must not
                # silently terminate the long-lived worker.
                self._report_exception("Unexpected worker loop failure", error)
                processed = False
            if not processed:
                self.stop_event.wait(max(0.05, poll_interval))


def noop_handler(
    payload: dict[str, Any], progress, cancel_event: threading.Event
) -> dict[str, Any]:
    duration = max(0.0, min(float(payload.get("duration", 0.0) or 0.0), 30.0))
    if duration:
        steps = max(1, int(duration * 4))
        for index in range(steps):
            if cancel_event.is_set():
                break
            time.sleep(duration / steps)
            progress((index + 1) / steps, "Checking the worker pipeline")
    return {"echo": payload.get("echo"), "worker": "ready"}
