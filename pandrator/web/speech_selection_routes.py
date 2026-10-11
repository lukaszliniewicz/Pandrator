"""Authenticated routes for direct speech-selection editing."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import select

from . import models as m
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .speech_selection_batch import (
    apply_speech_selection_batch,
    preview_speech_selection_batch,
)
from .speech_selection_batch_schemas import (
    SpeechSelectionBatchApplyRequest,
    SpeechSelectionBatchRequest,
)
from .speech_selection_schemas import (
    SpeechSelectionApplyRequest,
    SpeechSelectionContextRequest,
    SpeechSelectionRequest,
)
from .speech_selection_sounds import (
    apply_speech_selection,
    get_speech_selection_context,
    preview_speech_selection,
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

    @app.post(base + "/selection-context", endpoint="get_speech_selection_context")
    @guards.require_scope("app.read")
    def selection_context(session_id):
        try:
            payload = SpeechSelectionContextRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            with services.database.session() as session:
                result = get_speech_selection_context(
                    services, session, session_id, payload
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
                    resource_kind=(
                        "performance_plan" if result.get("performance_plan") else None
                    ),
                    resource_id=(result.get("performance_plan") or {}).get("id"),
                )
            return jsonify(result)
        except (ValueError, KeyError, IdempotencyConflict, IdempotencyInProgress) as error:
            return failure(error)

    @app.post(base + "/selection-batch-preview", endpoint="preview_speech_selection_batch")
    @guards.require_scope("app.read")
    def batch_preview(session_id):
        try:
            payload = SpeechSelectionBatchRequest.model_validate(request.get_json(silent=True) or {})
            with services.database.session() as session:
                result = preview_speech_selection_batch(services, session, session_id, payload)
            return jsonify(result)
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.post(base + "/selection-batch", endpoint="apply_speech_selection_batch")
    @guards.require_scope("app.write")
    def batch_apply(session_id):
        try:
            payload = SpeechSelectionBatchApplyRequest.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
            key = services.idempotency.validate_key(request.headers.get("Idempotency-Key", ""))
            principal = guards.principal()
            operation = "applySpeechSelectionBatch"
            request_payload = {"session_id": session_id, **body}
            # A completed receipt takes precedence over current plan revisions.
            with services.database.session() as session:
                existing = session.scalar(select(m.ApiIdempotency).where(
                    m.ApiIdempotency.principal_subject == principal.subject,
                    m.ApiIdempotency.operation_id == operation,
                    m.ApiIdempotency.idempotency_key == key,
                ))
                if existing is not None:
                    from .idempotency import _aware

                    if _aware(existing.expires_at) > m.utcnow():
                        digest = services.idempotency.request_digest(operation, request_payload)
                        if existing.request_digest != digest:
                            raise IdempotencyConflict(
                                "This idempotency key was already used with different arguments."
                            )
                        if existing.state in {"completed", "failed"}:
                            response = jsonify(dict(existing.response_json or {}))
                            response.status_code = int(existing.status_code or 200)
                            response.headers["Idempotency-Replayed"] = "true"
                            return response
            # Compile against a read session before opening the writer transaction.
            with services.database.session() as session:
                preview_speech_selection_batch(services, session, session_id, payload)
            with services.database.immediate_session() as session:
                if session.get(m.SessionRecord, session_id) is None:
                    raise KeyError(session_id)
                reservation = services.idempotency.begin(
                    session, principal=principal, operation_id=operation,
                    idempotency_key=key, payload=request_payload,
                )
                if reservation.response is not None:
                    response_body, status = reservation.response
                    response = jsonify(response_body)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = apply_speech_selection_batch(services, session, session_id, body)
                services.idempotency.complete(
                    session, reservation, response=result, status_code=200,
                    resource_kind="performance_plan",
                    resource_id=result["performance_plan"]["id"],
                )
            return jsonify(result)
        except (ValueError, KeyError, IdempotencyConflict, IdempotencyInProgress) as error:
            return failure(error)


__all__ = ["register_speech_selection_routes"]
