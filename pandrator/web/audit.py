"""Bounded, content-free audit projection for authenticated API activity."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .access.contracts import AccessContext
from .accounts.actor_models import AuditActorRecord
from .accounts.execution_actor import (
    ExecutionActor,
    UserActor,
    actor_from_principal,
    actor_from_storage,
    actor_to_storage,
)
from .auth import Principal
from .credentials import SecretRedactor
from .database import Database, is_sqlite_busy
from .models import AuditEvent, utcnow

AUDIT_METADATA_KEYS = frozenset(
    {
        "expected_revision",
        "network_zone",
        "replayed",
        "retryable",
    }
)
AUDIT_WRITE_TIMEOUT_MS = 250
AUDIT_BATCH_LIMIT = 200
AUDIT_REPLAY_LIMIT = 25
AUDIT_RECORD_REPLAY_LIMIT = 5
AUDIT_RECORD_REPLAY_BUDGET_MS = 250
_LOGGER = logging.getLogger(__name__)
_EVENT_FIELDS = (
    "request_id",
    "traceparent",
    "principal_subject",
    "principal_kind",
    "scopes_json",
    "action",
    "method",
    "path",
    "idempotency_key",
    "plan_id",
    "plan_digest",
    "resource_kind",
    "resource_id",
    "outcome",
    "status_code",
    "duration_ms",
    "metadata_json",
)


@dataclass(eq=False, slots=True)
class _PendingAudit:
    """One request's audit event waiting for, or finished by, a group commit."""

    fields: dict[str, Any]
    actor: ExecutionActor | None
    done: bool = False
    committed: bool = False
    # The writer was busy: the request spools its own event once released, so a
    # held writer never makes one thread write every spool file of a batch.
    spool: bool = False
    error: BaseException | None = None

    def event(self) -> AuditEvent:
        # A rolled-back flush leaves the old instance with a stale id.
        return AuditEvent(**self.fields)


class AuditService:
    def __init__(
        self,
        database: Database,
        redactor: SecretRedactor,
        *,
        maximum_events: int = 10_000,
        retention_days: int = 90,
    ) -> None:
        self.database = database
        self.redactor = redactor
        self.maximum_events = max(1_000, int(maximum_events))
        self.retention_days = max(1, int(retention_days))
        self._commit_condition = threading.Condition()
        self._commit_queue: deque[_PendingAudit] = deque()
        self._commit_leader = False

    def record_command_in_session(
        self,
        session: Session,
        context: AccessContext,
        *,
        action: str,
        workspace_id: str,
        metadata: dict[str, str | int | bool | None],
        resource_kind: str = "workspace",
    ) -> None:
        """Commit governance evidence atomically with its authorized mutation."""
        if type(context) is not AccessContext:
            raise TypeError("An access context is required.")
        allowed = {
            "user_id", "invitation_id", "role", "state", "revision",
            "expected_revision", "invited_by_user_id", "mode",
            "source_project_id", "destination_workspace_id", "fingerprint", "job_id",
        }
        if set(metadata) - allowed or any(
            value is not None and type(value) not in (str, int, bool)
            for value in metadata.values()
        ):
            raise ValueError("Command audit metadata must be content-free.")
        actor = context.actor
        event = AuditEvent(
            request_id=f"command:{uuid.uuid4()}",
            principal_subject=actor.subject,
            principal_kind=actor.principal_kind,
            scopes_json=sorted(actor.scopes),
            action=action[:160], method="COMMAND", path="",
            resource_kind=resource_kind, resource_id=workspace_id,
            outcome="succeeded", status_code=200, duration_ms=0,
            metadata_json=dict(metadata), created_at=utcnow(),
        )
        session.add(event)
        session.flush([event])
        self._add_actor(session, event.id, actor)
        session.flush()

    def record(
        self,
        *,
        principal: Principal,
        request_id: str,
        traceparent: str | None,
        action: str,
        method: str,
        path: str,
        status_code: int,
        duration_ms: int,
        idempotency_key: str | None = None,
        plan_id: str | None = None,
        plan_digest: str | None = None,
        resource_kind: str | None = None,
        resource_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        outcome = (
            "succeeded"
            if status_code < 400
            else "denied"
            if status_code in {401, 403}
            else "failed"
        )
        supplied_metadata = metadata if isinstance(metadata, dict) else {}
        actor = actor_from_principal(principal)
        safe_metadata: dict[str, Any] = {}
        for key in AUDIT_METADATA_KEYS:
            if key not in supplied_metadata:
                continue
            value = supplied_metadata[key]
            if value is None or isinstance(value, (bool, int, float)):
                safe_metadata[key] = value
            else:
                safe_metadata[key] = self.redactor.redact(value)[:160]
        fields: dict[str, Any] = {
            "request_id": str(request_id)[:80],
            "traceparent": (str(traceparent)[:80] if traceparent else None),
            "principal_subject": principal.subject[:200],
            "principal_kind": principal.kind,
            "scopes_json": sorted(principal.scopes),
            "action": str(action or "unknown")[:160],
            "method": str(method)[:12],
            "path": self.redactor.redact(path)[:500],
            "idempotency_key": (
                self.redactor.redact(idempotency_key)[:200] if idempotency_key else None
            ),
            "plan_id": str(plan_id)[:120] if plan_id else None,
            "plan_digest": (str(plan_digest)[:128] if plan_digest else None),
            "resource_kind": (str(resource_kind)[:80] if resource_kind else None),
            "resource_id": (str(resource_id)[:160] if resource_id else None),
            "outcome": outcome,
            "status_code": max(100, min(int(status_code), 599)),
            "duration_ms": max(0, min(int(duration_ms), 86_400_000)),
            "metadata_json": safe_metadata,
            "created_at": utcnow(),
        }
        entry = _PendingAudit(fields, actor)
        led = self._commit(entry)
        if entry.spool:
            self._spool_entry(entry)
        if entry.error is not None:
            raise entry.error
        if led and entry.committed:
            self._drain_pending(
                limit=AUDIT_RECORD_REPLAY_LIMIT,
                budget_ms=AUDIT_RECORD_REPLAY_BUDGET_MS,
                # Replay is opportunistic after the current event has committed.
                busy_timeout_ms=0,
            )

    def _commit(self, entry: _PendingAudit) -> bool:
        """Return once the entry is committed or marked to spool; report whether this thread led.

        Concurrent requests share one write transaction per batch. The thread that
        finds no leader writes batches from the queue front until its own entry is
        done, then hands leadership to a waiting request. A leader whose entry sits
        behind a backlog therefore writes the batches ahead of it first. When the
        writer is busy, the whole batch is released at once with ``spool`` set, and
        each request writes its own spool file in its own thread before returning.
        """
        condition = self._commit_condition
        with condition:
            self._commit_queue.append(entry)
            while not entry.done and self._commit_leader:
                condition.wait()
            lead = not entry.done
            if lead:
                self._commit_leader = True
        if lead:
            try:
                while True:
                    with condition:
                        if entry.done:
                            break
                        batch = [
                            self._commit_queue.popleft()
                            for _ in range(min(AUDIT_BATCH_LIMIT, len(self._commit_queue)))
                        ]
                    self._write_batch(batch)
            finally:
                with condition:
                    self._commit_leader = False
                    condition.notify_all()
        return lead

    def _write_batch(self, batch: list[_PendingAudit]) -> None:
        try:
            self._write_entries(batch)
        except BaseException as error:  # noqa: BLE001 - every entry must be resolved
            for entry in batch:
                if (
                    not entry.done
                    and not entry.committed
                    and not entry.spool
                    and entry.error is None
                ):
                    entry.error = error
            if not isinstance(error, Exception):
                raise
        finally:
            with self._commit_condition:
                for entry in batch:
                    entry.done = True
                self._commit_condition.notify_all()

    def _write_entries(self, batch: list[_PendingAudit]) -> None:
        try:
            self._insert(batch)
        except Exception as error:
            if is_sqlite_busy(error):
                for entry in batch:
                    entry.spool = True
            elif len(batch) > 1:
                for entry in batch:
                    self._write_entries([entry])
            else:
                batch[0].error = error
        else:
            for entry in batch:
                entry.committed = True

    def _insert(self, batch: list[_PendingAudit]) -> None:
        with self.database.immediate_session(busy_timeout_ms=AUDIT_WRITE_TIMEOUT_MS) as session:
            events = [entry.event() for entry in batch]
            session.add_all(events)
            session.flush()
            for event, entry in zip(events, batch, strict=True):
                self._add_actor(session, event.id, entry.actor)
            if any(event.id % 100 == 0 for event in events):
                self._prune_in_session(session)

    def _spool_entry(self, entry: _PendingAudit) -> None:
        try:
            self._spool(entry.event(), entry.actor)
        except Exception as error:
            entry.error = error
            return
        _LOGGER.warning("Audit writer busy; event saved to pending audit spool")

    @property
    def _pending_dir(self) -> Path:
        return self.database.path.parent / "audit-pending"

    @staticmethod
    def _add_actor(
        session: Session, event_id: int, actor: ExecutionActor | None
    ) -> None:
        stored = actor_to_storage(actor)
        if stored is not None:
            session.add(AuditActorRecord(audit_event_id=event_id, **stored))

    @staticmethod
    def _read_actor(record: AuditActorRecord) -> ExecutionActor:
        actor = actor_from_storage(record.user_id, record.auth_revision, record.context_json)
        if actor is None:
            raise ValueError("The saved audit actor is invalid.")
        return actor

    def _spool(self, event: AuditEvent, actor: ExecutionActor | None = None) -> None:
        pending = self._pending_dir
        pending.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending.chmod(0o700)
        spool_id = str(uuid.uuid4())
        payload = {field: getattr(event, field) for field in _EVENT_FIELDS}
        payload["actor"] = actor_to_storage(actor)
        payload["metadata_json"] = {**payload["metadata_json"], "spool_id": spool_id}
        payload["created_at"] = event.created_at.isoformat()
        destination = pending / f"{spool_id}.json"
        temporary = pending / f".{spool_id}.tmp"
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            directory_flag = getattr(os, "O_DIRECTORY", None)
            if directory_flag is not None:
                directory_descriptor = os.open(pending, os.O_RDONLY | directory_flag)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _drain_pending(
        self,
        *,
        limit: int = AUDIT_REPLAY_LIMIT,
        budget_ms: int | None = None,
        busy_timeout_ms: int = AUDIT_WRITE_TIMEOUT_MS,
    ) -> None:
        deadline = time.monotonic() + budget_ms / 1000 if budget_ms is not None else None
        pending = self._pending_dir
        if not pending.is_dir():
            return
        for path in islice(pending.glob("*.json"), limit):
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                spool_id = path.stem
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("metadata_json"), dict
                ):
                    raise ValueError("Invalid pending audit event")
                if payload["metadata_json"].get("spool_id") != spool_id:
                    raise ValueError("Pending audit spool ID mismatch")
                event_data = {field: payload[field] for field in _EVENT_FIELDS}
                event_data["created_at"] = datetime.fromisoformat(payload["created_at"])
                stored_actor = payload.get("actor")
                actor = None
                if stored_actor is not None:
                    if not isinstance(stored_actor, dict) or set(stored_actor) != {
                        "user_id", "auth_revision", "context_json"
                    }:
                        raise ValueError("Invalid pending audit actor")
                    actor = actor_from_storage(**stored_actor)
                    if actor is None:
                        raise ValueError("Invalid pending audit actor")
                remaining_ms = (
                    max(0, int((deadline - time.monotonic()) * 1000))
                    if deadline is not None
                    else AUDIT_WRITE_TIMEOUT_MS
                )
                if remaining_ms == 0:
                    break
                with self.database.immediate_session(
                    busy_timeout_ms=min(busy_timeout_ms, remaining_ms)
                ) as session:
                    existing = session.scalar(
                        select(AuditEvent.id)
                        .where(AuditEvent.metadata_json["spool_id"].as_string() == spool_id)
                        .limit(1)
                    )
                    if existing is None:
                        event = AuditEvent(**event_data)
                        session.add(event)
                        session.flush()
                        self._add_actor(session, event.id, actor)
                    else:
                        stored = session.get(AuditActorRecord, existing)
                        existing_actor = (
                            self._read_actor(stored)
                            if stored is not None
                            else None
                        )
                        if actor_to_storage(existing_actor) != actor_to_storage(actor):
                            raise ValueError("Pending audit actor does not match the saved event")
                if deadline is not None and time.monotonic() >= deadline:
                    break
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                # Another request may have replayed and removed this event.
                continue
            except Exception as error:
                _LOGGER.warning(
                    "Pending audit event retained after replay failure: %s",
                    type(error).__name__,
                )
                if is_sqlite_busy(error):
                    break

    def _prune_in_session(self, session) -> int:
        cutoff = utcnow() - timedelta(days=self.retention_days)
        removed = int(
            session.execute(delete(AuditEvent).where(AuditEvent.created_at < cutoff)).rowcount or 0
        )
        count = int(session.scalar(select(func.count(AuditEvent.id))) or 0)
        overflow = max(0, count - self.maximum_events)
        if overflow:
            ids = list(
                session.scalars(select(AuditEvent.id).order_by(AuditEvent.id).limit(overflow)).all()
            )
            if ids:
                removed += int(
                    session.execute(delete(AuditEvent).where(AuditEvent.id.in_(ids))).rowcount or 0
                )
        return removed

    def cleanup(self) -> int:
        self._drain_pending()
        with self.database.immediate_session(busy_timeout_ms=AUDIT_WRITE_TIMEOUT_MS) as session:
            return self._prune_in_session(session)

    def list(
        self,
        *,
        principal_subject: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self.database.session() as session:
            statement = select(AuditEvent).order_by(AuditEvent.id.desc())
            if principal_subject:
                statement = statement.where(AuditEvent.principal_subject == principal_subject)
            events = list(session.scalars(statement.limit(bounded_limit)).all())
            actors: dict[int, ExecutionActor] = {}
            if events:
                actors = {
                    record.audit_event_id: self._read_actor(record)
                    for record in session.scalars(
                        select(AuditActorRecord).where(
                            AuditActorRecord.audit_event_id.in_([event.id for event in events])
                        )
                    ).all()
                }
            return [
                {
                    "id": event.id,
                    "request_id": event.request_id,
                    "traceparent": event.traceparent,
                    "principal_subject": event.principal_subject,
                    "principal_kind": event.principal_kind,
                    **self._actor_fields(actors.get(event.id)),
                    "scopes": list(event.scopes_json or []),
                    "action": event.action,
                    "method": event.method,
                    "path": event.path,
                    "idempotency_key": event.idempotency_key,
                    "plan_id": event.plan_id,
                    "plan_digest": event.plan_digest,
                    "resource_kind": event.resource_kind,
                    "resource_id": event.resource_id,
                    "outcome": event.outcome,
                    "status_code": event.status_code,
                    "duration_ms": event.duration_ms,
                    "metadata": {
                        key: value
                        for key, value in (event.metadata_json or {}).items()
                        if key != "spool_id"
                    },
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]

    @staticmethod
    def _actor_fields(actor: ExecutionActor | None) -> dict[str, Any]:
        user = actor if isinstance(actor, UserActor) else None
        return {
            "user_id": user.user_id if user else None,
            "auth_revision": user.auth_revision if user else None,
            "token_id": user.token_id if user else None,
            "browser_session_id": user.browser_session_id if user else None,
            "client_id": user.client_id if user else None,
        }
