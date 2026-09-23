"""HTTP retry responses and reservation probes shared across route domains."""

from __future__ import annotations

from typing import Any

from flask import jsonify, request

from .auth import Principal
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext


class MutationIdempotency:
    def __init__(self, context: RouteContext):
        self.context = context

    def principal(self) -> Principal:
        principal = self.context.guards.principal()
        if principal is None:
            raise RuntimeError("An authenticated principal is required.")
        return principal

    def require_key(self):
        """Require retry identity for MCP principals, preserve browser UX."""

        key = str(request.headers.get("Idempotency-Key") or "").strip()
        principal = self.context.guards.principal()
        if key:
            return key, None
        if principal is not None and principal.kind in {
            "automation_client",
            "manager_bootstrap",
        }:
            return None, self.context.guards.error_response(
                "idempotency_key_required",
                "This automation write requires Idempotency-Key.",
                400,
            )
        return None, None

    def failure(self, error):
        if isinstance(
            error,
            (IdempotencyConflict, IdempotencyInProgress),
        ):
            return self.context.guards.error_response(
                error.code,
                str(error),
                409,
                {"retryable": error.retryable},
            )
        return self.context.guards.error_response(
            "validation_error",
            str(error),
            422,
        )

    @staticmethod
    def abandon(db_session, reservation) -> None:
        """Do not retain a reservation when no domain mutation occurred."""

        if not reservation.replayed:
            db_session.delete(reservation.record)
            db_session.flush()

    @staticmethod
    def replay(reservation, *, etag_key: str | None = None):
        """Return a stored mutation response, preserving its original status."""

        replay = reservation.response
        if replay is None:
            return None
        payload, status_code = replay
        response = jsonify(payload)
        response.status_code = status_code
        response.headers["Idempotency-Replayed"] = "true"
        if etag_key and payload.get(etag_key) is not None:
            response.headers["ETag"] = f'"{payload[etag_key]}"'
        return response

    def inspect(
        self,
        operation_id: str,
        idempotency_key: str | None,
        payload: dict[str, Any],
        *,
        etag_key: str | None = None,
    ):
        """Replay a completed mutation before inspecting mutable state.

        A fresh reservation is removed immediately.  The real mutation path
        reserves again in the same transaction as its write or queued job;
        this probe exists only so a completed request can replay after the
        referenced resource has changed or disappeared.
        """

        if idempotency_key is None:
            return None
        try:
            with self.context.services.database.immediate_session() as db_session:
                try:
                    reservation = self.context.services.idempotency.begin(
                        db_session,
                        principal=self.principal(),
                        operation_id=operation_id,
                        idempotency_key=idempotency_key,
                        payload=payload,
                    )
                except (
                    IdempotencyConflict,
                    IdempotencyInProgress,
                    ValueError,
                ) as error:
                    return self.failure(error)
                replay = self.replay(reservation, etag_key=etag_key)
                if replay is not None:
                    return replay
                self.abandon(db_session, reservation)
        except (
            IdempotencyConflict,
            IdempotencyInProgress,
            ValueError,
        ) as error:
            return self.failure(error)
        return None
