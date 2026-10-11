"""Bounded, content-free audit projection for authenticated API activity."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

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
        safe_metadata: dict[str, Any] = {}
        for key in AUDIT_METADATA_KEYS:
            if key not in supplied_metadata:
                continue
            value = supplied_metadata[key]
            if value is None or isinstance(value, (bool, int, float)):
                safe_metadata[key] = value
            else:
                safe_metadata[key] = self.redactor.redact(value)[:160]
        event = AuditEvent(
            request_id=str(request_id)[:80],
            traceparent=(str(traceparent)[:80] if traceparent else None),
            principal_subject=principal.subject[:200],
            principal_kind=principal.kind,
            scopes_json=sorted(principal.scopes),
            action=str(action or "unknown")[:160],
            method=str(method)[:12],
            path=self.redactor.redact(path)[:500],
            idempotency_key=(
                self.redactor.redact(idempotency_key)[:200] if idempotency_key else None
            ),
            plan_id=str(plan_id)[:120] if plan_id else None,
            plan_digest=(str(plan_digest)[:128] if plan_digest else None),
            resource_kind=(str(resource_kind)[:80] if resource_kind else None),
            resource_id=(str(resource_id)[:160] if resource_id else None),
            outcome=outcome,
            status_code=max(100, min(int(status_code), 599)),
            duration_ms=max(0, min(int(duration_ms), 86_400_000)),
            metadata_json=safe_metadata,
            created_at=utcnow(),
        )
        try:
            with self.database.session(busy_timeout_ms=AUDIT_WRITE_TIMEOUT_MS) as session:
                session.add(event)
                session.flush()
                if event.id % 100 == 0:
                    self._prune_in_session(session)
        except Exception as error:
            if not is_sqlite_busy(error):
                raise
            self._spool(event)
            _LOGGER.warning("Audit writer busy; event saved to pending audit spool")
        else:
            self._drain_pending(
                limit=AUDIT_RECORD_REPLAY_LIMIT,
                budget_ms=AUDIT_RECORD_REPLAY_BUDGET_MS,
            )

    @property
    def _pending_dir(self) -> Path:
        return self.database.path.parent / "audit-pending"

    def _spool(self, event: AuditEvent) -> None:
        pending = self._pending_dir
        pending.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending.chmod(0o700)
        spool_id = str(uuid.uuid4())
        payload = {field: getattr(event, field) for field in _EVENT_FIELDS}
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
            directory_descriptor = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _drain_pending(
        self, *, limit: int = AUDIT_REPLAY_LIMIT, budget_ms: int | None = None
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
                remaining_ms = (
                    max(0, int((deadline - time.monotonic()) * 1000))
                    if deadline is not None
                    else AUDIT_WRITE_TIMEOUT_MS
                )
                if remaining_ms == 0:
                    break
                with self.database.immediate_session(
                    busy_timeout_ms=min(AUDIT_WRITE_TIMEOUT_MS, remaining_ms)
                ) as session:
                    existing = session.scalar(
                        select(AuditEvent.id)
                        .where(AuditEvent.metadata_json["spool_id"].as_string() == spool_id)
                        .limit(1)
                    )
                    if existing is None:
                        session.add(AuditEvent(**event_data))
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
            return [
                {
                    "id": event.id,
                    "request_id": event.request_id,
                    "traceparent": event.traceparent,
                    "principal_subject": event.principal_subject,
                    "principal_kind": event.principal_kind,
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
