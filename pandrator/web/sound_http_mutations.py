"""Small HTTP transaction adapter shared by the two sound domains."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import jsonify, request
from sqlalchemy.orm import Session

from .http_idempotency import MutationIdempotency
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .settings_policy import RevisionConflict


class SoundHttpMutations:
    def __init__(self, context: RouteContext):
        self.context = context
        self.retry = MutationIdempotency(context)

    def failure(self, error: Exception):
        guards = self.context.guards
        if isinstance(error, KeyError):
            return guards.error_response(
                "not_found", "The requested sound or project was not found.", 404
            )
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return self.retry.failure(error)
        return guards.error_response("validation_error", str(error), 422)

    def mutate(
        self,
        operation: str,
        body: dict[str, Any],
        action: Callable[[Session], dict[str, Any]],
        status: int = 200,
    ):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return self.context.guards.error_response(
                "idempotency_key_required", "Idempotency-Key is required.", 400
            )
        services = self.context.services
        try:
            with services.database.immediate_session() as session:
                reservation = services.idempotency.begin(
                    session,
                    principal=self.retry.principal(),
                    operation_id=operation,
                    idempotency_key=key,
                    payload=body,
                )
                if reservation.replayed:
                    return self.retry.replay(reservation)
                result = action(session)
                services.idempotency.complete(
                    session, reservation, response=result, status_code=status
                )
            return jsonify(result), status
        except (
            ValueError,
            KeyError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return self.failure(error)
