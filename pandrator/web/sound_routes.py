"""Thin HTTP bindings for the independent reusable-sound domain."""

from __future__ import annotations

from flask import jsonify, request
from pydantic import ValidationError

from .domain_blueprints import DomainBlueprints
from .route_context import RouteContext
from .sound_http_mutations import SoundHttpMutations
from .sound_schemas import SoundGenerate, SoundSave, SoundUpdate


def register_sound_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    sound = services.sounds
    guards = context.guards
    mutations = SoundHttpMutations(context)
    retry, failure, mutation = mutations.retry, mutations.failure, mutations.mutate

    @app.get("/api/v1/sounds/catalogue")
    @guards.require_auth
    def get_sound_catalogue():
        return jsonify(sound.catalogue())

    @app.get("/api/v1/sounds")
    @guards.require_auth
    def list_sounds():
        try:
            archived = request.args.get("archived", "false")
            if archived not in {"true", "false"}:
                raise ValueError("archived must be true or false.")
            return jsonify(
                sound.list_sounds(
                    query=request.args.get("query", ""),
                    tag=request.args.get("tag"),
                    cursor=request.args.get("cursor"),
                    limit=int(request.args.get("limit", "40")),
                    archived=archived == "true",
                )
            )
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.get("/api/v1/sounds/candidates")
    @guards.require_auth
    def list_sound_candidates():
        try:
            return jsonify(
                sound.list_candidates(
                    cursor=request.args.get("cursor"), limit=int(request.args.get("limit", "20"))
                )
            )
        except ValueError as error:
            return failure(error)

    @app.get("/api/v1/sounds/candidates/<candidate_id>")
    @guards.require_auth
    def get_sound_candidate(candidate_id: str):
        try:
            return jsonify(sound.get_candidate(candidate_id))
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.get("/api/v1/sounds/<item_id>")
    @guards.require_auth
    def get_sound(item_id: str):
        try:
            return jsonify(
                sound.get_sound(
                    item_id,
                    cursor=request.args.get("cursor"),
                    limit=int(request.args.get("limit", "20")),
                )
            )
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.post("/api/v1/sounds/generate")
    @guards.require_auth
    def generate_sound():
        try:
            payload = SoundGenerate.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", exclude_unset=True)
            key = request.headers.get("Idempotency-Key", "").strip()
            if not key:
                return guards.error_response(
                    "idempotency_key_required", "Idempotency-Key is required.", 400
                )
            replay = retry.inspect("generateSound", key, body)
            if replay is not None:
                return replay
            prepared = sound.prepare_generation(body)
            return mutation(
                "generateSound",
                body,
                lambda session: sound.generate_in_session(session, body, prepared=prepared),
                202,
            )
        except (ValidationError, ValueError, RuntimeError) as error:
            return failure(error)

    @app.post("/api/v1/sounds/save")
    @guards.require_auth
    def save_sound():
        try:
            payload = SoundSave.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", exclude_unset=True)
            return mutation("saveSound", body, lambda session: sound.save_in_session(session, body))
        except (ValidationError, ValueError) as error:
            return failure(error)

    @app.patch("/api/v1/sounds/<item_id>")
    @guards.require_auth
    def update_sound(item_id: str):
        try:
            payload = SoundUpdate.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", exclude_unset=True)
            return mutation(
                "updateSound",
                {"item_id": item_id, **body},
                lambda session: sound.update_in_session(session, item_id, body),
            )
        except (ValidationError, ValueError) as error:
            return failure(error)

    @app.delete("/api/v1/sounds/candidates/<candidate_id>")
    @guards.require_auth
    def discard_sound(candidate_id: str):
        return mutation(
            "discardSoundCandidate",
            {"candidate_id": candidate_id},
            lambda session: sound.discard_in_session(session, candidate_id),
        )
