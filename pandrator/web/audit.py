"""Bounded, content-free audit projection for authenticated API activity."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select

from .auth import Principal
from .credentials import SecretRedactor
from .database import Database
from .models import AuditEvent, utcnow

AUDIT_METADATA_KEYS = frozenset(
    {
        "expected_revision",
        "network_zone",
        "replayed",
        "retryable",
    }
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
        supplied_metadata = (
            metadata if isinstance(metadata, dict) else {}
        )
        safe_metadata: dict[str, Any] = {}
        for key in AUDIT_METADATA_KEYS:
            if key not in supplied_metadata:
                continue
            value = supplied_metadata[key]
            if value is None or isinstance(value, (bool, int, float)):
                safe_metadata[key] = value
            else:
                safe_metadata[key] = self.redactor.redact(value)[:160]
        with self.database.session() as session:
            event = AuditEvent(
                request_id=str(request_id)[:80],
                traceparent=(
                    str(traceparent)[:80] if traceparent else None
                ),
                principal_subject=principal.subject[:200],
                principal_kind=principal.kind,
                scopes_json=sorted(principal.scopes),
                action=str(action or "unknown")[:160],
                method=str(method)[:12],
                path=self.redactor.redact(path)[:500],
                idempotency_key=(
                    self.redactor.redact(idempotency_key)[:200]
                    if idempotency_key
                    else None
                ),
                plan_id=str(plan_id)[:120] if plan_id else None,
                plan_digest=(
                    str(plan_digest)[:128] if plan_digest else None
                ),
                resource_kind=(
                    str(resource_kind)[:80] if resource_kind else None
                ),
                resource_id=(
                    str(resource_id)[:160] if resource_id else None
                ),
                outcome=outcome,
                status_code=max(100, min(int(status_code), 599)),
                duration_ms=max(0, min(int(duration_ms), 86_400_000)),
                metadata_json=safe_metadata,
            )
            session.add(event)
            session.flush()
            if event.id % 100 == 0:
                self._prune_in_session(session)

    def _prune_in_session(self, session) -> int:
        cutoff = utcnow() - timedelta(days=self.retention_days)
        removed = int(
            session.execute(
                delete(AuditEvent).where(AuditEvent.created_at < cutoff)
            ).rowcount
            or 0
        )
        count = int(
            session.scalar(select(func.count(AuditEvent.id))) or 0
        )
        overflow = max(0, count - self.maximum_events)
        if overflow:
            ids = list(
                session.scalars(
                    select(AuditEvent.id)
                    .order_by(AuditEvent.id)
                    .limit(overflow)
                ).all()
            )
            if ids:
                removed += int(
                    session.execute(
                        delete(AuditEvent).where(AuditEvent.id.in_(ids))
                    ).rowcount
                    or 0
                )
        return removed

    def cleanup(self) -> int:
        with self.database.session() as session:
            return self._prune_in_session(session)

    def list(
        self,
        *,
        principal_subject: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self.database.session() as session:
            statement = select(AuditEvent).order_by(
                AuditEvent.id.desc()
            )
            if principal_subject:
                statement = statement.where(
                    AuditEvent.principal_subject == principal_subject
                )
            events = list(
                session.scalars(statement.limit(bounded_limit)).all()
            )
            return [
                {
                    "id": event.id,
                    "request_id": event.request_id,
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
                    "metadata": event.metadata_json or {},
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]
