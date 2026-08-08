"""SQLite-backed durable jobs and worker execution."""

from __future__ import annotations

import logging
import random
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, ClassVar

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from .credentials import SecretRedactor
from .database import Database
from .models import AgentRun, GenerationRun, Job, JobEvent, ResourceClaim, utcnow

JobHandler = Callable[
    [dict[str, Any], Callable[[float, str | None], None], threading.Event],
    dict[str, Any] | None,
]


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
        if kind.startswith(("source.", "pdf.")):
            entities.add("sources")
        if (
            kind.startswith("generation.")
            or kind
            in {
                "text.prepare",
                "text.optimize_tts",
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
        self, session, job_id: str | None, event_type: str, payload: dict | None = None
    ) -> JobEvent:
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
    ) -> bool:
        return bool(
            job
            and job.lease_owner == worker_id
            and job.lease_generation == lease_generation
            and (not require_running or job.status == "running")
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

    @staticmethod
    def _is_generation_owner(run: GenerationRun, job: Job) -> bool:
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        return bool(
            job.kind == "generation.run"
            and job.session_id == run.session_id
            and str(payload.get("generation_run_id") or "") == run.id
        )

    def _apply_generation_job_status(
        self,
        run: GenerationRun,
        job: Job,
    ) -> bool:
        """Project one safely linked terminal job onto its generation run."""

        status = self.GENERATION_JOB_TERMINAL_STATUS.get(str(job.status or ""))
        if status is None or not self._is_generation_owner(run, job):
            return False
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
            self._apply_generation_job_status(run, job)

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
    ) -> Job:
        """Enqueue within a caller-owned transaction without committing it."""

        job = Job(
            kind=kind,
            payload_json=payload or {},
            session_id=session_id,
            workflow_run_id=workflow_run_id,
            max_attempts=max(1, int(max_attempts)),
            resource_keys_json=sorted(
                {str(key) for key in (resource_keys or []) if str(key).strip()}
            ),
        )
        session.add(job)
        session.flush()
        self._event(
            session,
            job.id,
            "job.queued",
            {"kind": kind, "session_id": session_id},
        )
        session.flush()
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
        normalized_keys = sorted({str(key).strip() for key in keys if str(key).strip()})
        now = utcnow()
        expires = now + timedelta(seconds=max(5, lease_seconds))
        with self.database.immediate_session() as session:
            job = session.get(Job, job_id)
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
            claims = {
                claim.resource_key: claim
                for claim in session.scalars(
                    select(ResourceClaim).where(
                        ResourceClaim.resource_key.in_(normalized_keys)
                    )
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
    ) -> bool:
        with self.database.session() as session:
            result = session.execute(
                update(ResourceClaim)
                .where(
                    ResourceClaim.job_id == job_id,
                    ResourceClaim.lease_owner == worker_id,
                    ResourceClaim.lease_generation == lease_generation,
                    select(Job.id)
                    .where(
                        Job.id == job_id,
                        Job.status == "running",
                        Job.lease_owner == worker_id,
                        Job.lease_generation == lease_generation,
                    )
                    .exists(),
                )
                .values(expires_at=utcnow() + timedelta(seconds=max(5, lease_seconds)))
            )
            return bool(result.rowcount)

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

    def list(self, limit: int = 100) -> list[Job]:
        self._reconcile_stale()
        with self.database.session() as session:
            jobs = list(
                session.scalars(
                    select(Job)
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

    def claim(self, worker_id: str, lease_seconds: int = 30) -> Job | None:
        now = utcnow()
        with self.database.session() as session:
            potentially_available = session.scalar(
                select(Job.id)
                .where(
                    or_(
                        self._available_filter(now),
                        self._stale_filter(now),
                    )
                )
                .limit(1)
            )
        if potentially_available is None:
            return None
        with self.database.immediate_session() as session:
            now = utcnow()
            self._reconcile_stale_locked(session)
            session.flush()
            running_session_ids = select(Job.session_id).where(
                Job.status == "running",
                Job.session_id.is_not(None),
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at > now,
            )
            statement = (
                select(Job)
                .where(
                    or_(
                        self._available_filter(now),
                        and_(Job.status == "running", Job.lease_expires_at <= now),
                    ),
                    or_(
                        Job.session_id.is_(None),
                        Job.session_id.not_in(running_session_ids),
                    ),
                    Job.attempts < Job.max_attempts,
                )
                .order_by(Job.created_at.asc())
                .limit(1)
            )
            job = session.scalar(statement)
            if job is None:
                return None
            reclaimed = job.status == "running"
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
        now = utcnow()
        with self.database.immediate_session() as session:
            job = session.get(Job, job_id)
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
        """Cancel one job inside the caller's existing write transaction."""

        job = session.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status == "queued":
            job.status = "canceled"
            job.finished_at = utcnow()
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
            job = session.get(Job, job_id)
            if not self._owns_lease(job, worker_id, lease_generation):
                raise RuntimeError("Job lease is no longer owned by this worker.")
            job.status = "succeeded"
            job.progress = 1.0
            job.result_json = self.redact_diagnostic(result or {})
            job.lease_owner = None
            job.lease_expires_at = None
            job.finished_at = utcnow()
            job.updated_at = job.finished_at
            self._event(session, job.id, "job.succeeded", job.result_json)
            self._reconcile_generation_run_for_job_locked(session, job)
            self._reconcile_agent_runs_for_jobs_locked(session, [job.id])

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
            job.finished_at = utcnow()
            job.updated_at = job.finished_at
            self._event(session, job.id, "job.canceled")
            self._reconcile_generation_run_for_job_locked(session, job)
            self._reconcile_agent_runs_for_jobs_locked(session, [job.id])
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


class _JobLogHandler(logging.Handler):
    """Route Python logs emitted while a handler runs into its durable timeline."""

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

    def emit(self, record: logging.LogRecord) -> None:
        try:
            trace = (
                logging.Formatter().formatException(record.exc_info)
                if record.exc_info
                else ""
            )
            self.queue.log(
                self.job_id,
                record.levelname,
                record.getMessage(),
                logger=record.name,
                trace=trace,
                worker_id=self.worker_id,
                lease_generation=self.lease_generation,
            )
        except Exception:
            # Logging must never be allowed to fail the job it is observing.
            self.handleError(record)


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
        """Update a linked agent run only while this claim is still current."""
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        agent_run_id = str(payload.get("agent_run_id") or "")
        from .models import AgentRun

        with self.queue.database.immediate_session() as session:
            current_job = session.get(Job, job.id)
            if (
                current_job is None
                or current_job.lease_generation != lease_generation
                or current_job.lease_owner != self.worker_id
            ):
                return
            active_statuses = {"queued", "running", "retrying"}
            if agent_run_id:
                candidate = session.get(AgentRun, agent_run_id)
                agent_runs = (
                    [candidate]
                    if candidate is not None and candidate.status in active_statuses
                    else []
                )
            else:
                # Transformation handlers create their AgentRun after the job
                # begins, so its ID cannot be present in the immutable payload.
                # Recover every active run owned by this exact job without
                # touching completed stages from an earlier workflow step.
                agent_runs = list(
                    session.scalars(
                        select(AgentRun).where(
                            AgentRun.job_id == job.id,
                            AgentRun.status.in_(active_statuses),
                        )
                    ).all()
                )
            for agent_run in agent_runs:
                agent_run.status = status
                agent_run.updated_at = utcnow()

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
        root_logger = logging.getLogger()
        previous_log_level = root_logger.level
        logging.basicConfig(level=logging.INFO)
        if previous_log_level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        root_logger.addHandler(log_handler)

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
                        if not self.queue.heartbeat(
                            job.id,
                            self.worker_id,
                            lease_generation=lease_generation,
                        ):
                            lease_lost_event.set()
                            cancel_event.set()
                            return
                        self.queue.heartbeat_resources(
                            job.id,
                            self.worker_id,
                            lease_generation=lease_generation,
                        )
                        heartbeat_at = now + 5.0
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
            if not self.queue.heartbeat(
                job.id,
                self.worker_id,
                lease_generation=lease_generation,
                progress=value,
                detail=detail,
            ):
                lease_lost_event.set()
                cancel_event.set()
                return
            self.queue.heartbeat_resources(
                job.id,
                self.worker_id,
                lease_generation=lease_generation,
            )

        try:
            monitor_thread.start()
            # Handlers occasionally create child domain records (for example a
            # GenerationRun) while still executing inside this durable job.
            # Pass the owning job ID as internal context so those records can
            # expose accurate status, cancellation, and error information.
            if not isinstance(job.payload_json, dict):
                raise TypeError("Job payload must be a JSON object.")
            handler_payload = dict(job.payload_json)
            handler_payload["_job_id"] = job.id
            handler_payload["_lease_generation"] = lease_generation
            result = handler(handler_payload, progress, cancel_event)
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
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(previous_log_level)

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

    def run_forever(self, poll_interval: float = 0.5) -> None:
        while not self.stop_event.is_set():
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
