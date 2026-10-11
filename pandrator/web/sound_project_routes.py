"""Versioned project sound editing, independent of speech mutations."""

from __future__ import annotations

from flask import jsonify, request

from .domain_blueprints import DomainBlueprints
from .route_context import RouteContext
from .sound_http_mutations import SoundHttpMutations
from .sound_project_schemas import SoundCueCreate, SoundCueUpdate, SoundPlanRestore


def register_project_sound_routes(app: DomainBlueprints, context: RouteContext) -> None:
    cues, guards = context.services.project_sounds, context.guards
    mutations = SoundHttpMutations(context)

    @app.get("/api/v1/sessions/<session_id>/sound-plan")
    @guards.require_auth
    def get_project_sound_plan(session_id: str):
        try:
            segment_ids = (
                request.args.getlist("segment_id") if "segment_id" in request.args else None
            )
            return jsonify(
                cues.get_plan(
                    session_id,
                    cursor=request.args.get("cursor"),
                    limit=int(request.args.get("limit", "40")),
                    segment_ids=segment_ids,
                )
            )
        except (ValueError, KeyError) as error:
            return mutations.failure(error)

    @app.post("/api/v1/sessions/<session_id>/sound-plan/cues")
    @guards.require_auth
    def add_project_sound(session_id: str):
        try:
            body = SoundCueCreate.model_validate(request.get_json(silent=True) or {}).model_dump(
                mode="json", exclude_unset=True
            )
            return mutations.mutate(
                "addProjectSound",
                {"session_id": session_id, **body},
                lambda session: cues.add_in_session(session, session_id, body),
                201,
            )
        except ValueError as error:
            return mutations.failure(error)

    @app.patch("/api/v1/sessions/<session_id>/sound-plan/cues/<cue_id>")
    @guards.require_auth
    def update_project_sound(session_id: str, cue_id: str):
        try:
            body = SoundCueUpdate.model_validate(request.get_json(silent=True) or {}).model_dump(
                mode="json", exclude_unset=True
            )
            return mutations.mutate(
                "updateProjectSound",
                {"session_id": session_id, "cue_id": cue_id, **body},
                lambda session: cues.update_in_session(session, session_id, cue_id, body),
            )
        except ValueError as error:
            return mutations.failure(error)

    @app.delete("/api/v1/sessions/<session_id>/sound-plan/cues/<cue_id>")
    @guards.require_auth
    def remove_project_sound(session_id: str, cue_id: str):
        raw = request.headers.get("If-Match", "").strip('"')
        if not raw or not raw.isdecimal():
            return guards.error_response(
                "precondition_required",
                "If-Match must contain the current sound-plan revision.",
                428,
            )
        revision = int(raw)
        return mutations.mutate(
            "removeProjectSound",
            {"session_id": session_id, "cue_id": cue_id, "expected_revision": revision},
            lambda session: cues.remove_in_session(session, session_id, cue_id, revision),
        )

    @app.post("/api/v1/sessions/<session_id>/sound-plan/restore")
    @guards.require_auth
    def restore_project_sound_plan(session_id: str):
        try:
            body = SoundPlanRestore.model_validate(request.get_json(silent=True) or {}).model_dump(
                mode="json"
            )
            return mutations.mutate(
                "restoreProjectSounds",
                {"session_id": session_id, **body},
                lambda session: cues.restore_in_session(session, session_id, body),
            )
        except ValueError as error:
            return mutations.failure(error)
