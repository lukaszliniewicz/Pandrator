"""Thin HTTP bindings for the independent reusable-sound domain."""

from __future__ import annotations

from dataclasses import replace

from flask import jsonify, request
from pydantic import ValidationError

from .access.contracts import ProjectAction, WorkspaceNotFound
from .access.sound_library_policy import (
    require_sound_library_headers,
    require_sound_library_workspace,
)
from .credential_identity import credential_namespace
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

    def library_scope():
        if "session_id" in request.args:
            raise ValueError("Session scope is not available for the sound library.")
        workspace_id = request.args.get("workspace_id")
        if workspace_id is not None and (
            not workspace_id or workspace_id != workspace_id.strip() or len(workspace_id) > 80
        ):
            raise ValueError("Choose a valid sound workspace.")
        return workspace_id

    def library_write_scope(*, item_id=None, candidate_id=None):
        requested = library_scope()
        with services.database.snapshot_session() as session:
            workspace = require_sound_library_workspace(
                session, ProjectAction.EDIT, workspace_id=requested
            )
            workspace_id = workspace.id
            require_sound_library_headers(
                session, workspace_id, item_id=item_id, candidate_id=candidate_id
            )
        return requested, workspace_id

    def library_mutation(
        operation, body, action, *, requested, workspace_id, item_id=None, candidate_id=None
    ):
        def authorize(session):
            proved = require_sound_library_workspace(
                session, ProjectAction.EDIT, workspace_id=workspace_id
            )
            if proved.id != workspace_id:
                raise WorkspaceNotFound()
            require_sound_library_headers(
                session, proved.id, item_id=item_id, candidate_id=candidate_id
            )

        receipt_body = {**body, "workspace_id": workspace_id} if requested is not None else body
        return mutation(operation, receipt_body, action, authorize=authorize)

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
                    workspace_id=library_scope(),
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
                    cursor=request.args.get("cursor"), limit=int(request.args.get("limit", "20")),
                    workspace_id=library_scope(),
                )
            )
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.get("/api/v1/sounds/candidates/<candidate_id>")
    @guards.require_auth
    def get_sound_candidate(candidate_id: str):
        try:
            return jsonify(sound.get_candidate(candidate_id, workspace_id=library_scope()))
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
                    workspace_id=library_scope(),
                )
            )
        except (ValueError, KeyError) as error:
            return failure(error)

    @app.post("/api/v1/sounds/generate")
    @guards.require_auth
    def generate_sound():
        try:
            requested = library_scope()
            with services.database.snapshot_session() as session:
                workspace_id = require_sound_library_workspace(
                    session, ProjectAction.RUN, workspace_id=requested
                ).id
                principal = retry.principal()
                if not credential_namespace(session, workspace_id).legacy_host_access:
                    principal = replace(
                        principal,
                        subject=f"workspace:{workspace_id}:user:{guards.access_context().actor.user_id}",
                    )

            def authorize(session):
                require_sound_library_workspace(
                    session, ProjectAction.RUN, workspace_id=workspace_id
                )

            payload = SoundGenerate.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", exclude_unset=True)
            key = request.headers.get("Idempotency-Key", "").strip()
            if not key:
                return guards.error_response(
                    "idempotency_key_required", "Idempotency-Key is required.", 400
                )
            replay = retry.inspect(
                "generateSound", key, body, authorize=authorize, principal=principal
            )
            if replay is not None:
                return replay
            prepared = sound.prepare_generation(body, workspace_id=workspace_id)
            return mutation(
                "generateSound",
                body,
                lambda session: sound.generate_in_session(
                    session, body, prepared=prepared, workspace_id=workspace_id
                ),
                202,
                authorize=authorize,
                principal=principal,
            )
        except (ValidationError, ValueError, RuntimeError) as error:
            return failure(error)

    @app.post("/api/v1/sounds/save")
    @guards.require_auth
    def save_sound():
        try:
            requested, workspace_id = library_write_scope()
            payload = SoundSave.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", exclude_unset=True)
            return library_mutation(
                "saveSound", body,
                lambda session: sound.save_in_session(session, body, workspace_id=workspace_id),
                requested=requested, workspace_id=workspace_id,
                item_id=payload.item_id, candidate_id=payload.candidate_id,
            )
        except (ValidationError, ValueError, KeyError) as error:
            return failure(error)

    @app.patch("/api/v1/sounds/<item_id>")
    @guards.require_auth
    def update_sound(item_id: str):
        try:
            requested, workspace_id = library_write_scope(item_id=item_id)
            payload = SoundUpdate.model_validate(request.get_json(silent=True) or {})
            body = payload.model_dump(mode="json", exclude_unset=True)
            return library_mutation(
                "updateSound",
                {"item_id": item_id, **body},
                lambda session: sound.update_in_session(
                    session, item_id, body, workspace_id=workspace_id
                ),
                requested=requested, workspace_id=workspace_id,
                item_id=item_id, candidate_id=payload.preferred_variant_id,
            )
        except (ValidationError, ValueError, KeyError) as error:
            return failure(error)

    @app.delete("/api/v1/sounds/candidates/<candidate_id>")
    @guards.require_auth
    def discard_sound(candidate_id: str):
        try:
            requested, workspace_id = library_write_scope(candidate_id=candidate_id)
            return library_mutation(
                "discardSoundCandidate",
                {"candidate_id": candidate_id},
                lambda session: sound.discard_in_session(
                    session, candidate_id, workspace_id=workspace_id
                ),
                requested=requested, workspace_id=workspace_id, candidate_id=candidate_id,
            )
        except (ValueError, KeyError) as error:
            return failure(error)
