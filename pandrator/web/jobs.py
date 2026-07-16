"""SQLite-backed durable jobs and worker execution."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select

from .database import Database
from .models import Job, JobEvent, ResourceClaim, utcnow


JobHandler = Callable[[dict[str, Any], Callable[[float, str | None], None], threading.Event], dict[str, Any] | None]


class JobQueue:
    def __init__(self, database: Database):
        self.database = database

    def _event(self, session, job_id: str | None, event_type: str, payload: dict | None = None) -> JobEvent:
        event = JobEvent(job_id=job_id, event_type=event_type, payload_json=payload or {})
        session.add(event)
        return event

    def log(self, job_id: str, level: str, message: str, *, logger: str = "", trace: str = "") -> None:
        """Persist a worker log record beside the durable job timeline."""
        with self.database.session() as session:
            if session.get(Job, job_id) is None:
                return
            self._event(
                session,
                job_id,
                "job.log",
                {
                    "level": str(level or "INFO").upper(),
                    "message": str(message or ""),
                    "logger": str(logger or ""),
                    **({"trace": str(trace)} if trace else {}),
                },
            )

    def _reconcile_stale_locked(self, session) -> None:
        """Close jobs whose worker lease vanished instead of leaving them running forever."""
        now = utcnow()
        records = list(
            session.scalars(
                select(Job).where(
                    Job.status.in_(("running", "cancel_requested")),
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                )
            ).all()
        )
        for job in records:
            if job.status == "cancel_requested":
                job.status = "canceled"
                job.finished_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                self._event(session, job.id, "job.canceled", {"reason": "worker_lease_expired"})
            elif job.attempts >= job.max_attempts or job.lease_expires_at is None:
                job.status = "failed"
                job.error_code = job.error_code or "worker_lease_expired"
                job.error_message = job.error_message or "The worker stopped before this job completed."
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
        job = Job(
            kind=kind,
            payload_json=payload or {},
            session_id=session_id,
            workflow_run_id=workflow_run_id,
            max_attempts=max(1, int(max_attempts)),
            resource_keys_json=sorted({str(key) for key in (resource_keys or []) if str(key).strip()}),
        )
        with self.database.session() as session:
            session.add(job)
            session.flush()
            self._event(session, job.id, "job.queued", {"kind": kind, "session_id": session_id})
            session.flush()
            session.expunge(job)
            return job

    def acquire_resources(self, job_id: str, worker_id: str, keys: list[str], lease_seconds: int = 30) -> bool:
        if not keys:
            return True
        now = utcnow()
        expires = now + timedelta(seconds=max(5, lease_seconds))
        with self.database.session() as session:
            conflicts = list(
                session.scalars(
                    select(ResourceClaim).where(
                        ResourceClaim.resource_key.in_(keys),
                        ResourceClaim.expires_at > now,
                        ResourceClaim.job_id != job_id,
                    )
                ).all()
            )
            if conflicts:
                return False
            for key in keys:
                claim = session.get(ResourceClaim, key)
                if claim is None:
                    session.add(ResourceClaim(resource_key=key, job_id=job_id, lease_owner=worker_id, expires_at=expires))
                else:
                    claim.job_id = job_id
                    claim.lease_owner = worker_id
                    claim.expires_at = expires
            return True

    def heartbeat_resources(self, job_id: str, worker_id: str, lease_seconds: int = 30) -> None:
        with self.database.session() as session:
            for claim in session.scalars(select(ResourceClaim).where(ResourceClaim.job_id == job_id, ResourceClaim.lease_owner == worker_id)).all():
                claim.expires_at = utcnow() + timedelta(seconds=max(5, lease_seconds))

    def release_resources(self, job_id: str, worker_id: str) -> None:
        with self.database.session() as session:
            for claim in session.scalars(select(ResourceClaim).where(ResourceClaim.job_id == job_id, ResourceClaim.lease_owner == worker_id)).all():
                session.delete(claim)

    def defer_for_resources(self, job_id: str, worker_id: str) -> None:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.lease_owner != worker_id:
                return
            job.status = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
            job.attempts = max(0, job.attempts - 1)
            job.updated_at = utcnow()
            self._event(session, job.id, "job.waiting_for_resource", {"resources": job.resource_keys_json})

    def list(self, limit: int = 100) -> list[Job]:
        with self.database.session() as session:
            self._reconcile_stale_locked(session)
            session.flush()
            jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500)))).all())
            for job in jobs:
                session.expunge(job)
            return jobs

    def get(self, job_id: str) -> Job:
        with self.database.session() as session:
            self._reconcile_stale_locked(session)
            session.flush()
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            session.expunge(job)
            return job

    def claim(self, worker_id: str, lease_seconds: int = 30) -> Job | None:
        now = utcnow()
        with self.database.session() as session:
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
                        Job.status == "queued",
                        and_(Job.status == "running", Job.lease_expires_at <= now),
                    ),
                    or_(Job.session_id.is_(None), Job.session_id.not_in(running_session_ids)),
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
            job.attempts += 1
            job.started_at = job.started_at or now
            job.updated_at = now
            self._event(session, job.id, "job.reclaimed" if reclaimed else "job.started", {"worker_id": worker_id})
            session.flush()
            session.expunge(job)
            return job

    def heartbeat(self, job_id: str, worker_id: str, *, progress: float | None = None, detail: str | None = None, lease_seconds: int = 30) -> bool:
        now = utcnow()
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "running" or job.lease_owner != worker_id:
                return False
            job.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
            job.updated_at = now
            payload: dict[str, Any] = {}
            if progress is not None:
                job.progress = max(0.0, min(1.0, float(progress)))
                payload["progress"] = job.progress
            if detail:
                payload["detail"] = detail
            if payload:
                self._event(session, job.id, "job.progress", payload)
            return True

    def should_cancel(self, job_id: str, worker_id: str) -> bool:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            return bool(
                job
                and job.status in {"cancel_requested", "canceled"}
                and (job.lease_owner in {None, worker_id})
            )

    def request_cancel(self, job_id: str) -> Job:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status == "queued":
                job.status = "canceled"
                job.finished_at = utcnow()
                event_type = "job.canceled"
            elif job.status in {"running", "cancel_requested"}:
                # Cancellation is a terminal state immediately. The worker's
                # monitor notices it independently of progress callbacks and
                # prevents any later result from replacing this state.
                self._event(session, job.id, "job.cancel_requested")
                job.status = "canceled"
                job.finished_at = utcnow()
                job.lease_owner = None
                job.lease_expires_at = None
                event_type = "job.canceled"
            else:
                event_type = "job.cancel_ignored"
            job.updated_at = utcnow()
            self._event(session, job.id, event_type)
            session.flush()
            session.expunge(job)
            return job

    def complete(self, job_id: str, worker_id: str, result: dict[str, Any] | None = None) -> None:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.lease_owner != worker_id or job.status != "running":
                raise RuntimeError("Job lease is no longer owned by this worker.")
            job.status = "succeeded"
            job.progress = 1.0
            job.result_json = result or {}
            job.lease_owner = None
            job.lease_expires_at = None
            job.finished_at = utcnow()
            job.updated_at = job.finished_at
            self._event(session, job.id, "job.succeeded", job.result_json)

    def fail(self, job_id: str, worker_id: str, code: str, message: str, *, trace: str | None = None) -> None:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.lease_owner != worker_id:
                return
            retry = job.attempts < job.max_attempts
            job.status = "queued" if retry else "failed"
            job.error_code = code
            job.error_message = message
            job.lease_owner = None
            job.lease_expires_at = None
            job.finished_at = None if retry else utcnow()
            job.updated_at = utcnow()
            self._event(
                session,
                job.id,
                "job.retry_scheduled" if retry else "job.failed",
                {"code": code, "message": message, "trace": trace or ""},
            )

    def cancel_owned(self, job_id: str, worker_id: str) -> None:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.lease_owner != worker_id:
                return
            job.status = "canceled"
            job.lease_owner = None
            job.lease_expires_at = None
            job.finished_at = utcnow()
            job.updated_at = job.finished_at
            self._event(session, job.id, "job.canceled")

    def events_after(self, event_id: int = 0, limit: int = 250) -> list[JobEvent]:
        with self.database.session() as session:
            events = list(
                session.scalars(
                    select(JobEvent).where(JobEvent.id > max(0, event_id)).order_by(JobEvent.id.asc()).limit(limit)
                ).all()
            )
            for event in events:
                session.expunge(event)
            return events

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

    def __init__(self, queue: JobQueue, job_id: str):
        super().__init__(level=logging.INFO)
        self.queue = queue
        self.job_id = job_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            trace = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
            self.queue.log(
                self.job_id,
                record.levelname,
                record.getMessage(),
                logger=record.name,
                trace=trace,
            )
        except Exception:
            # Logging must never be allowed to fail the job it is observing.
            self.handleError(record)


class Worker:
    def __init__(self, queue: JobQueue, worker_id: str, handlers: dict[str, JobHandler] | None = None):
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = handlers or {}
        self.stop_event = threading.Event()

    def register(self, kind: str, handler: JobHandler) -> None:
        self.handlers[kind] = handler

    def stop(self) -> None:
        self.stop_event.set()

    def run_once(self) -> bool:
        job = self.queue.claim(self.worker_id)
        if job is None:
            return False
        if not self.queue.acquire_resources(job.id, self.worker_id, list(job.resource_keys_json or [])):
            self.queue.defer_for_resources(job.id, self.worker_id)
            return False
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.queue.fail(job.id, self.worker_id, "unknown_job_kind", f"No handler is registered for '{job.kind}'.")
            return True

        cancel_event = threading.Event()
        monitor_stop = threading.Event()
        log_handler = _JobLogHandler(self.queue, job.id)
        root_logger = logging.getLogger()
        previous_log_level = root_logger.level
        logging.basicConfig(level=logging.INFO)
        if previous_log_level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        root_logger.addHandler(log_handler)

        def monitor() -> None:
            """Keep leases alive and observe cancellation even during quiet handlers."""
            heartbeat_at = 0.0
            while not monitor_stop.wait(0.2):
                if self.queue.should_cancel(job.id, self.worker_id):
                    cancel_event.set()
                    return
                now = time.monotonic()
                if now >= heartbeat_at:
                    if not self.queue.heartbeat(job.id, self.worker_id):
                        cancel_event.set()
                        return
                    self.queue.heartbeat_resources(job.id, self.worker_id)
                    heartbeat_at = now + 5.0

        monitor_thread = threading.Thread(
            target=monitor,
            name=f"job-monitor-{job.id[:8]}",
            daemon=True,
        )
        monitor_thread.start()

        def progress(value: float, detail: str | None = None) -> None:
            if self.queue.should_cancel(job.id, self.worker_id):
                cancel_event.set()
            self.queue.heartbeat(job.id, self.worker_id, progress=value, detail=detail)
            self.queue.heartbeat_resources(job.id, self.worker_id)

        try:
            # Handlers occasionally create child domain records (for example a
            # GenerationRun) while still executing inside this durable job.
            # Pass the owning job ID as internal context so those records can
            # expose accurate status, cancellation, and error information.
            handler_payload = dict(job.payload_json or {})
            handler_payload["_job_id"] = job.id
            result = handler(handler_payload, progress, cancel_event)
            if cancel_event.is_set() or self.queue.should_cancel(job.id, self.worker_id):
                agent_run_id = str(job.payload_json.get("agent_run_id") or "")
                if agent_run_id:
                    from .models import AgentRun

                    with self.queue.database.session() as session:
                        agent_run = session.get(AgentRun, agent_run_id)
                        if agent_run is not None:
                            agent_run.status = "canceled"
                            agent_run.updated_at = utcnow()
                self.queue.cancel_owned(job.id, self.worker_id)
            else:
                self.queue.complete(job.id, self.worker_id, result)
        except Exception as error:
            canceled = cancel_event.is_set() or self.queue.should_cancel(job.id, self.worker_id)
            if canceled:
                logging.warning("Worker job %s stopped after cancellation: %s", job.id, error)
            else:
                logging.exception("Worker job %s failed", job.id)
            agent_run_id = str(job.payload_json.get("agent_run_id") or "")
            if agent_run_id:
                from .models import AgentRun

                with self.queue.database.session() as session:
                    agent_run = session.get(AgentRun, agent_run_id)
                    if agent_run is not None:
                        agent_run.status = "canceled" if canceled else "failed"
                        agent_run.updated_at = utcnow()
            if canceled:
                self.queue.cancel_owned(job.id, self.worker_id)
            else:
                self.queue.fail(
                    job.id,
                    self.worker_id,
                    type(error).__name__,
                    str(error),
                    trace=traceback.format_exc(),
                )
        finally:
            monitor_stop.set()
            monitor_thread.join(timeout=1.0)
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(previous_log_level)
            self.queue.release_resources(job.id, self.worker_id)
        return True

    def run_forever(self, poll_interval: float = 0.5) -> None:
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(max(0.05, poll_interval))


def noop_handler(payload: dict[str, Any], progress, cancel_event: threading.Event) -> dict[str, Any]:
    duration = max(0.0, min(float(payload.get("duration", 0.0) or 0.0), 30.0))
    if duration:
        steps = max(1, int(duration * 4))
        for index in range(steps):
            if cancel_event.is_set():
                break
            time.sleep(duration / steps)
            progress((index + 1) / steps, "Checking the worker pipeline")
    return {"echo": payload.get("echo"), "worker": "ready"}

