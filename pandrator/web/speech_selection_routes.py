"""Authenticated routes for direct speech-selection editing."""

from __future__ import annotations

from flask import jsonify, request

from . import models as m
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .speech_selection import apply_speech_selection, preview_speech_selection
from .speech_selection_schemas import (
    SpeechSelectionApplyRequest,
    SpeechSelectionRequest,
)


def register_speech_selection_routes(app, context) -> None:
    services, guards = context.services, context.guards
    base = "/api/v1/sessions/<session_id>/speech-plan"

    def failure(error):
        if isinstance(error, KeyError):
            return guards.error_response(
                "not_found", "Session, speech plan or segment not found.", 404
            )
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(
                error.code, str(error), 409, {"retryable": error.retryable}
            )
        from .settings_policy import RevisionConflict

        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        return guards.error_response("validation_error", str(error), 422)

    @app.post(base + "/selection-preview", endpoint="preview_speech_selection")
    @guards.require_scope("app.read")
    def preview(session_id):
        try:
            payload = SpeechSelectionRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            with services.database.session() as session:
                result = preview_speech_selection(
                    services,
                    session,
                    session_id,
                    payload.model_dump(mode="json", by_alias=True, exclude_unset=True),
                )
            return jsonify(result)
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.post(base + "/selection", endpoint="apply_speech_selection")
    @guards.require_scope("app.write")
    def apply(session_id):
        try:
            payload = SpeechSelectionApplyRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            key = request.headers.get("Idempotency-Key", "")
            services.idempotency.validate_key(key)
            body = payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
            with services.database.immediate_session() as session:
                if session.get(m.SessionRecord, session_id) is None:
                    raise KeyError(session_id)
                reservation = services.idempotency.begin(
                    session,
                    principal=guards.principal(),
                    operation_id="applySpeechSelection",
                    idempotency_key=key,
                    payload={"session_id": session_id, **body},
                )
                if reservation.response is not None:
                    response_body, status = reservation.response
                    response = jsonify(response_body)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = apply_speech_selection(services, session, session_id, body)
                services.idempotency.complete(
                    session,
                    reservation,
                    response=result,
                    status_code=200,
                    resource_kind="performance_plan",
                    resource_id=result["performance_plan"]["id"],
                )
            return jsonify(result)
        except (ValueError, KeyError, IdempotencyConflict, IdempotencyInProgress) as error:
            return failure(error)


__all__ = ["register_speech_selection_routes"]
