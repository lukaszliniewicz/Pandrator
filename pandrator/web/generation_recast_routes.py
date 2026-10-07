"""Shared casting actions for browser and MCP clients."""

from flask import jsonify, request

from .generation_audio_versions import preview_audio_version_undo
from .generation_recast_schemas import (
    AudioVersionUndoRequest,
    GenerationRecastPreviewRequest,
    GenerationRecastStartRequest,
)
from .generation_recast_workspace import start_recast_in_session, undo_recast_in_session
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import GenerationRun
from .settings_policy import RevisionConflict


def register_generation_recast_routes(app, context):
    services, guards = context.services, context.guards

    def failure(error):
        if isinstance(error, KeyError):
            return guards.error_response("not_found", "Project, plan or audio version not found.", 404)
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(error.code, str(error), 409)
        return guards.error_response("validation_error", str(error), 422)

    def prepare(session_id, body):
        from .generation_recast import prepare_recast

        return prepare_recast(services.database, services.generation.settings, session_id, **body)

    @app.post("/api/v1/sessions/<session_id>/generation-recast/preview")
    @guards.require_scope("app.read")
    def generation_recast_preview(session_id):
        try:
            body = GenerationRecastPreviewRequest.model_validate(
                request.get_json(silent=True) or {}
            ).model_dump()
            return jsonify(prepare(session_id, body)["preview"])
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.post("/api/v1/sessions/<session_id>/generation-recast")
    @guards.require_scope("app.write")
    def generation_recast_start(session_id):
        reservation_id = None
        principal = guards.principal()

        def abandon():
            if reservation_id is None:
                return
            try:
                with services.database.immediate_session() as session:
                    pending = services.idempotency.load_in_progress(
                        session, reservation_id, principal=principal
                    )
                    services.idempotency.abandon_in_progress(session, pending)
            except (IdempotencyInProgress, KeyError):
                pass

        try:
            body = GenerationRecastStartRequest.model_validate(
                request.get_json(silent=True) or {}
            ).model_dump()
            key = request.headers.get("Idempotency-Key", "")
            try:
                services.idempotency.validate_key(key)
            except ValueError as error:
                return guards.error_response("idempotency_key_required", str(error), 400)
            expected_hash = body.pop("expected_selection_hash")
            with services.database.immediate_session() as session:
                reservation = services.idempotency.begin(
                    session, principal=principal, operation_id="startGenerationRecast",
                    idempotency_key=key,
                    payload={"session_id": session_id, **body, "expected_selection_hash": expected_hash},
                )
                if reservation.response is not None:
                    result, status = reservation.response
                    response = jsonify(result)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                reservation_id = reservation.record.id
            # Compilation is outside the writer lock and accepted retries replay
            # before inspecting mutable casting or active audio.
            prepared = prepare(session_id, body)
            with services.database.immediate_session() as session:
                reservation = services.idempotency.load_in_progress(
                    session, reservation_id, principal=principal
                )
                result = start_recast_in_session(
                    services.generation, session, session_id, prepared, expected_hash
                )
                services.idempotency.complete(
                    session, reservation, response=result, status_code=201,
                    resource_kind="generation_run", resource_id=result["id"],
                )
            return jsonify(result), 201
        except (KeyError, ValueError, RevisionConflict, IdempotencyConflict, IdempotencyInProgress) as error:
            abandon()
            return failure(error)
        except Exception:
            abandon()
            raise

    @app.post("/api/v1/generation-runs/<run_id>/audio-version/undo-preview")
    @guards.require_scope("app.read")
    def generation_audio_version_undo_preview(run_id):
        try:
            with services.database.session() as session:
                return jsonify(preview_audio_version_undo(session, run_id))
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.post("/api/v1/generation-runs/<run_id>/audio-version/undo")
    @guards.require_scope("app.write")
    def generation_audio_version_undo(run_id):
        try:
            body = AudioVersionUndoRequest.model_validate(request.get_json(silent=True) or {})
            key = request.headers.get("Idempotency-Key", "")
            try:
                services.idempotency.validate_key(key)
            except ValueError as error:
                return guards.error_response("idempotency_key_required", str(error), 400)
            with services.database.immediate_session() as session:
                if session.get(GenerationRun, run_id) is None:
                    raise KeyError(run_id)
                reservation = services.idempotency.begin(
                    session, principal=guards.principal(), operation_id="undoGenerationRecast",
                    idempotency_key=key, payload={"run_id": run_id, **body.model_dump()},
                )
                if reservation.response is not None:
                    result, status = reservation.response
                    response = jsonify(result)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = undo_recast_in_session(
                    services.generation, session, run_id, body.expected_selection_hash
                )
                services.idempotency.complete(
                    session, reservation, response=result, status_code=200,
                    resource_kind="generation_run", resource_id=run_id,
                )
            return jsonify(result)
        except (KeyError, ValueError, RevisionConflict, IdempotencyConflict, IdempotencyInProgress) as error:
            return failure(error)
