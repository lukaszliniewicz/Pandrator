"""Authenticated HTTP transport for audiobook setup and speech previews."""

from __future__ import annotations

from flask import jsonify, request

from .audiobook_setup import configure_audiobook_setup, get_audiobook_setup
from .audiobook_schemas import (
    AudiobookSetupConfigureRequest,
    SpeechPlanPreviewRequest,
)
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .speech_plan_preview import preview_speech_segment
from .workspace import RevisionConflict


def register_audiobook_routes(app, context) -> None:
    """Register revisioned audiobook setup and read-only preview endpoints."""

    services, guards = context.services, context.guards
    setup_path = "/api/v1/sessions/<session_id>/audiobook-setup"
    preview_path = "/api/v1/sessions/<session_id>/speech-plan/preview"

    def failure(error):
        if isinstance(error, KeyError):
            return guards.error_response("not_found", "Audiobook session not found.", 404)
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(
                error.code,
                str(error),
                409,
                {"retryable": error.retryable},
            )
        return guards.error_response("validation_error", str(error), 422)

    @app.get(setup_path, endpoint="get_audiobook_setup")
    @guards.require_scope("app.read")
    def get_setup(session_id):
        try:
            with services.database.session() as session:
                return jsonify(get_audiobook_setup(services, session, session_id))
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.patch(setup_path, endpoint="configure_audiobook_setup")
    @guards.require_scope("app.write")
    def configure_setup(session_id):
        try:
            payload = AudiobookSetupConfigureRequest.model_validate(
                request.get_json(silent=True) or {}
            ).model_dump(mode="json")
            key = request.headers.get("Idempotency-Key", "")
            try:
                services.idempotency.validate_key(key)
            except ValueError as error:
                return guards.error_response(
                    "idempotency_key_required", str(error), 400
                )

            with services.database.immediate_session() as session:
                # Confirm the session and current audiobook snapshot before
                # reserving a retry identity. This avoids durable reservations
                # for invalid or non-audiobook sessions.
                get_audiobook_setup(services, session, session_id)
                reservation = services.idempotency.begin(
                    session,
                    principal=guards.principal(),
                    operation_id="configureAudiobook",
                    idempotency_key=key,
                    payload={"session_id": session_id, **payload},
                )
                if reservation.response is not None:
                    response_payload, status = reservation.response
                    response = jsonify(response_payload)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = configure_audiobook_setup(
                    services,
                    session,
                    session_id,
                    **payload,
                )
                services.idempotency.complete(
                    session,
                    reservation,
                    response=result,
                    status_code=200,
                    resource_kind="audiobook_setup",
                    resource_id=session_id,
                )
            return jsonify(result)
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)

    @app.post(preview_path, endpoint="preview_speech_segment")
    @guards.require_scope("app.read")
    def preview_segment(session_id):
        try:
            payload = SpeechPlanPreviewRequest.model_validate(
                request.get_json(silent=True) or {}
            ).model_dump(mode="json")
            return jsonify(
                preview_speech_segment(
                    services,
                    session_id,
                    **payload,
                )
            )
        except (KeyError, ValueError, RevisionConflict) as error:
            return failure(error)


__all__ = ["register_audiobook_routes"]
