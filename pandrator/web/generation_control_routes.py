"""Revisioned character dictionary and cast shared by the UI and passive MCP."""

from flask import jsonify, request

from .generation_control_schemas import GenerationControlsUpdateRequest
from .generation_controls import get_generation_controls, save_generation_controls
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .settings_policy import RevisionConflict


def register_generation_control_routes(app, context):
    services, guards = context.services, context.guards
    path = "/api/v1/sessions/<session_id>/generation-controls"

    def failure(error):
        if isinstance(error, KeyError):
            return guards.error_response(
                "not_found", "Session or voice not found.", 404
            )
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(error.code, str(error), 409)
        return guards.error_response("validation_error", str(error), 422)

    @app.get(path, endpoint="get_generation_controls")
    @guards.require_scope("app.read")
    def get_controls(session_id):
        try:
            with services.database.session() as session:
                return jsonify(get_generation_controls(session, session_id))
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.put(path, endpoint="update_generation_controls")
    @guards.require_scope("app.write")
    def update_controls(session_id):
        try:
            body = GenerationControlsUpdateRequest.model_validate(
                request.get_json(silent=True) or {}
            ).model_dump(mode="json")
            # Omitted top-level collections preserve their current value. A
            # nested null narrator is an explicit request to clear its binding.
            body = {name: value for name, value in body.items() if value is not None}
            key = request.headers.get("Idempotency-Key", "")
            try:
                services.idempotency.validate_key(key)
            except ValueError as error:
                return guards.error_response(
                    "idempotency_key_required", str(error), 400
                )
            with services.database.immediate_session() as session:
                # Verify session scope before reserving a write identity.
                get_generation_controls(session, session_id)
                reservation = services.idempotency.begin(
                    session,
                    principal=guards.principal(),
                    operation_id="updateGenerationControls",
                    idempotency_key=key,
                    payload={"session_id": session_id, **body},
                )
                if reservation.response is not None:
                    payload, status = reservation.response
                    response = jsonify(payload)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = save_generation_controls(session, session_id, **body)
                services.idempotency.complete(
                    session,
                    reservation,
                    response=result,
                    status_code=200,
                    resource_kind="generation_controls",
                    resource_id=result.get("id"),
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
