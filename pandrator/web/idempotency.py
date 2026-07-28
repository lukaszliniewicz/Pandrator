"""Transactional idempotency reservations for exact API mutations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .auth import Principal
from .credentials import SecretRedactor
from .database import Database
from .models import ApiIdempotency, utcnow

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


class IdempotencyError(RuntimeError):
    code: str
    retryable: bool = False

    def __init__(self, message: str):
        super().__init__(message)


class IdempotencyConflict(IdempotencyError):
    code = "idempotency_conflict"


class IdempotencyInProgress(IdempotencyError):
    code = "idempotency_in_progress"
    retryable = True


@dataclass(slots=True)
class IdempotencyReservation:
    record: ApiIdempotency
    replayed: bool

    @property
    def response(self) -> tuple[dict[str, Any], int] | None:
        if not self.replayed:
            return None
        return (
            dict(self.record.response_json or {}),
            int(self.record.status_code or 200),
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class IdempotencyService:
    def __init__(
        self,
        database: Database,
        redactor: SecretRedactor,
        *,
        retention_hours: int = 24,
        in_progress_minutes: int = 10,
    ) -> None:
        self.database = database
        self.redactor = redactor
        self.retention_hours = max(1, min(int(retention_hours), 168))
        self.in_progress_minutes = max(
            1,
            min(int(in_progress_minutes), 60),
        )

    @staticmethod
    def request_digest(operation_id: str, payload: Any) -> str:
        canonical = json.dumps(
            {
                "operation_id": operation_id,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_key(value: object) -> str:
        key = str(value or "").strip()
        if not _KEY.fullmatch(key):
            raise ValueError(
                "Idempotency-Key must contain 8-200 safe ASCII characters."
            )
        return key

    def begin(
        self,
        session: Session,
        *,
        principal: Principal,
        operation_id: str,
        idempotency_key: object,
        payload: Any,
    ) -> IdempotencyReservation:
        key = self.validate_key(idempotency_key)
        digest = self.request_digest(operation_id, payload)
        existing = session.scalar(
            select(ApiIdempotency).where(
                ApiIdempotency.principal_subject == principal.subject,
                ApiIdempotency.operation_id == operation_id,
                ApiIdempotency.idempotency_key == key,
            )
        )
        now = utcnow()
        if existing is not None and _aware(existing.expires_at) <= now:
            session.delete(existing)
            session.flush()
            existing = None
        if existing is not None:
            if existing.request_digest != digest:
                raise IdempotencyConflict(
                    "This idempotency key was already used with different arguments."
                )
            if existing.state in {"completed", "failed"}:
                return IdempotencyReservation(existing, replayed=True)
            stale_after = _aware(existing.created_at) + timedelta(
                minutes=self.in_progress_minutes
            )
            if stale_after > now:
                raise IdempotencyInProgress(
                    "The original request is still in progress."
                )
            if existing.resource_id:
                raise IdempotencyInProgress(
                    "The original request is recovering from its recorded resource."
                )
            session.delete(existing)
            session.flush()

        record = ApiIdempotency(
            principal_subject=principal.subject,
            operation_id=operation_id,
            idempotency_key=key,
            request_digest=digest,
            state="in_progress",
            expires_at=now + timedelta(hours=self.retention_hours),
        )
        session.add(record)
        session.flush()
        return IdempotencyReservation(record, replayed=False)

    def complete(
        self,
        session: Session,
        reservation: IdempotencyReservation,
        *,
        response: dict[str, Any],
        status_code: int,
        resource_kind: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        safe_response = self.redactor.redact_value(response)
        if not isinstance(safe_response, dict):
            safe_response = {}
        reservation.record.state = (
            "completed" if status_code < 400 else "failed"
        )
        reservation.record.status_code = int(status_code)
        reservation.record.response_json = safe_response
        reservation.record.resource_kind = resource_kind
        reservation.record.resource_id = resource_id
        session.flush()

    def cleanup(self) -> int:
        with self.database.session() as session:
            return int(
                session.execute(
                    delete(ApiIdempotency).where(
                        ApiIdempotency.expires_at < utcnow()
                    )
                ).rowcount
                or 0
            )
