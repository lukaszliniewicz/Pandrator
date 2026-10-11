"""Read-only mix status and replay-safe admission outside the writer lock."""

from __future__ import annotations

from flask import jsonify, request

from .domain_blueprints import DomainBlueprints
from .route_context import RouteContext
from .settings_policy import RevisionConflict
from .sound_http_mutations import SoundHttpMutations
from .sound_job_schemas import SoundMixRequest, SoundResolveRequest


def register_sound_job_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards
    mutations = SoundHttpMutations(context)
    base = "/api/v1/sessions/<session_id>/sound-plan"

    @app.get(base + "/mix")
    @guards.require_scope("app.read")
    def get_project_sound_mix(session_id: str):
        try:
            return jsonify(services.sound_mixes.get_status(
                session_id, request.args.get("base_artifact_id")
            ))
        except (ValueError, KeyError) as error:
            return mutations.failure(error)

    def admit(session_id: str, action: str):
        operation = "renderProjectSounds" if action == "render" else "resolveProjectSounds"
        try:
            schema = SoundMixRequest if action == "render" else SoundResolveRequest
            body = schema.model_validate(request.get_json(silent=True) or {}).model_dump(mode="json")
            accepted = {"session_id": session_id, **body}
            key = request.headers.get("Idempotency-Key", "").strip()
            if not key:
                return guards.error_response("idempotency_key_required", "Idempotency-Key is required.", 400)
            replay = mutations.retry.inspect(operation, key, accepted)
            if replay is not None:
                return replay
            if action == "render":
                prepared = services.sound_mixes.prepare(
                    session_id, body["base_artifact_id"],
                    timeline_artifact_id=body.get("timeline_artifact_id"),
                    expected_revision=body["expected_revision"],
                )
                return mutations.mutate(operation, accepted, lambda session: services.sound_mixes.admit_in_session(session, prepared), 202)
            resolution = services.sound_resolution.prepare(
                session_id, expected_revision=body["expected_revision"], cue_ids=body.get("cue_ids"),
            )
            return mutations.mutate(operation, accepted, lambda session: services.sound_resolution.admit_in_session(session, resolution), 202)
        except (ValueError, KeyError, RevisionConflict) as error:
            return mutations.failure(error)

    @app.post(base + "/render")
    @guards.require_scope("app.run")
    def render_project_sounds(session_id: str):
        return admit(session_id, "render")

    @app.post(base + "/resolve")
    @guards.require_scope("app.run")
    def resolve_project_sounds(session_id: str):
        return admit(session_id, "resolve")
