"""Revisioned character dictionary and cast shared by the UI and passive MCP."""

from flask import jsonify, request
from sqlalchemy import select

from .access.contracts import ProjectAction
from .access.generation_resource_policy import require_generation_resource_access
from .generation_control_schemas import CastSettings, GenerationControlsUpdateRequest
from .generation_controls import get_generation_controls, iter_cast_voice_bindings
from .http_session_access import require_session_action
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Artifact, Project, ProjectSpeechReference, SessionRecord
from .project_cast_settings import write_project_cast_settings
from .project_speech_references import speech_reference_selector_hash
from .settings_policy import RevisionConflict


def _retained_cast_projection(session, session_id, controls):
    """Optional UI hints from owned headers; never resolve a library or open a file."""
    bindings = dict(iter_cast_voice_bindings(CastSettings.model_validate(controls["cast"])))
    if not bindings:
        return {}
    reference = ProjectSpeechReference
    statement = (
        select(reference.slot_key, reference.selector_hash, reference.binding_json["model"].as_string())
        .join(Artifact, Artifact.id == reference.input_artifact_id)
        .join(SessionRecord, SessionRecord.id == reference.session_id)
        .join(Project, Project.id == SessionRecord.project_id)
        .where(
            reference.session_id == session_id,
            reference.slot_key.like("cast:%"),
            reference.binding_json["adapter"].as_string() == "audio_cpp",
            Artifact.session_id == session_id,
            Artifact.project_id == Project.id,
            Artifact.workspace_id == Project.workspace_id,
            Artifact.state == "current",
            Artifact.kind == "speech_voice_snapshot",
            Artifact.role == "speech_input",
            Artifact.content_hash == reference.input_content_hash,
        )
        .limit(2506)
    )
    rows = list(session.execute(statement))
    if len(rows) > 2505:
        return {}
    return {
        slot: {"model": model}
        for slot, selector_hash, model in rows
        if slot in bindings
        and selector_hash == speech_reference_selector_hash(bindings[slot].model_dump(mode="json"))
        and type(model) is str and 0 < len(model) <= 255
    }


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
    @require_session_action(context, ProjectAction.READ)
    def get_controls(session_id):
        try:
            include_retained = request.args.get("include_retained", "false")
            if include_retained not in {"true", "false"}:
                raise ValueError("include_retained must be true or false.")
            with services.database.session() as session:
                require_generation_resource_access(session, "session", session_id, ProjectAction.READ)
                value = get_generation_controls(session, session_id)
                if include_retained == "true":
                    value["retained_cast"] = _retained_cast_projection(session, session_id, value)
                return jsonify(value)
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.put(path, endpoint="update_generation_controls")
    @guards.require_scope("app.write")
    @require_session_action(context, ProjectAction.EDIT)
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
            result = write_project_cast_settings(
                services.database, services.paths, services.workspace_settings,
                session_id=session_id, **body, idempotency=services.idempotency,
                principal=guards.principal(), idempotency_key=key,
            )
            response = jsonify(result.value)
            response.status_code = result.status_code
            if result.replayed:
                response.headers["Idempotency-Replayed"] = "true"
            return response
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)
