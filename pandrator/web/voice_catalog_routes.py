"""Catalog search, persistent collections, and provider voice annotations."""

from __future__ import annotations

from dataclasses import replace
from functools import wraps

from flask import jsonify, request
from sqlalchemy import select

from pandrator.logic.tts_service_identity import normalize_service_id

from .access.contracts import ProjectNotFound, WorkspaceNotFound
from .access.session_policy import SessionNotFound
from .access.workspace_policy import WorkspaceAction
from .accounts.installation_mode import installation_mode_in_session
from .credential_identity import credential_namespace
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import VoiceCollection
from .settings_policy import RevisionConflict
from .speech_catalogue_access import require_speech_access, scope_identifier, tts_revision_headers
from .voice_catalog import (
    VoiceCatalogQuery,
    catalog_entries,
    catalog_models,
    listed_models,
    model_modes,
    query_catalog,
    supported_languages,
)
from .voice_catalog_metadata import catalog_overrides, update_catalog_metadata
from .voice_catalog_schemas import CatalogVoiceChanges as CatalogVoiceChanges
from .voice_catalog_schemas import (
    CatalogVoiceUpdate,
    VoiceCollectionCreate,
    VoiceCollectionUpdate,
    canonical_voice_key,
)
from .voice_collections import create_collection, list_collections, update_collection
from .voice_library import ensure_bundled_voice, voice_payloads
from .voice_queries import voices_in_workspace


def register_voice_catalog_routes(app, context):
    services, guards = context.services, context.guards
    database = services.database

    def failure(error):
        if isinstance(error, (KeyError, WorkspaceNotFound, SessionNotFound, ProjectNotFound)):
            return guards.error_response("not_found", "Voice or collection not found.", 404)
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(error.code, str(error), 409)
        return guards.error_response("validation_error", str(error), 422)

    def errors(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (
                KeyError,
                ValueError,
                TypeError,
                RevisionConflict,
                IdempotencyConflict,
                IdempotencyInProgress,
                WorkspaceNotFound,
                SessionNotFound,
                ProjectNotFound,
            ) as error:
                return failure(error)

        return wrapped

    def roots(*, write=False):
        if write and "session_id" in request.args:
            raise ValueError("Session scope is only available for voice catalogue reads.")
        return scope_identifier(request.args.get("workspace_id")), (
            None if write else scope_identifier(request.args.get("session_id"))
        )

    def proof(session, access, workspace_id=None, session_id=None, *, action=WorkspaceAction.READ):
        return require_speech_access(
            session, access, workspace_id, session_id=session_id, action=action
        ).workspace

    def collection_header(session, collection_id, workspace_id):
        if (
            collection_id
            and session.scalar(
                select(VoiceCollection.id).where(
                    VoiceCollection.id == collection_id,
                    VoiceCollection.workspace_id == workspace_id,
                )
            )
            is None
        ):
            raise KeyError(collection_id)

    def envelope(proved):
        return {
            "workspace_id": proved.id,
            "can_edit": WorkspaceAction.EDIT in proved.allowed_actions,
        }

    def recheck(access, workspace_id, session_id=None):
        with database.snapshot_session() as session:
            return proof(session, access, workspace_id, session_id)

    def maybe_initialize(access, workspace_id, session_id, collection_id=None):
        def eligible(session):
            proved = proof(session, access, workspace_id, session_id)
            collection_header(session, collection_id, proved.id)
            return proved, (
                proved.kind == "personal"
                and WorkspaceAction.EDIT in proved.allowed_actions
                and credential_namespace(session, proved.id).legacy_host_access
                and not installation_mode_in_session(session).enabled
            )

        with database.snapshot_session() as session:
            proved, permitted = eligible(session)
        if not permitted:
            return
        with database.snapshot_session() as session:
            fresh, permitted = eligible(session)
            permitted = permitted and fresh.id == proved.id
        if permitted:
            ensure_bundled_voice(
                database, services.paths, services.artifacts, workspace_id=proved.id
            )

    def snapshot(
        access,
        workspace_id=None,
        session_id=None,
        *,
        action=WorkspaceAction.READ,
        collection_id=None,
        include_entries=True,
    ):
        voices, collections, overrides = [], [], {}
        with database.snapshot_session() as session:
            proved = proof(session, access, workspace_id, session_id, action=action)
            collection_header(session, collection_id, proved.id)
            legacy = credential_namespace(session, proved.id).legacy_host_access
            approved = not legacy or installation_mode_in_session(session).enabled
            settings = services.tts_catalogue._settings(db_session=session, workspace_id=proved.id)
            if include_entries:
                voices = voice_payloads(
                    session,
                    services.paths,
                    voices_in_workspace(session, workspace_id=proved.id),
                    workspace_id=proved.id,
                )
                collections = list_collections(session, workspace_id=proved.id)
                overrides = catalog_overrides(session, workspace_id=proved.id)
        catalog, _ = services.tts_catalogue.snapshot(
            refresh=False,
            include_profiles=False,
            workspace_id=proved.id,
            settings_snapshot=settings,
            approved_only=approved,
            allow_host_adc=legacy and not approved,
        )
        entries = (
            catalog_entries(voices, catalog, collections, overrides) if include_entries else []
        )
        return (
            proved,
            entries,
            collections if include_entries else [],
            catalog,
            (settings[1], settings[3]),
        )

    def preflight(access, workspace_id, *, collection_id=None):
        with database.snapshot_session() as session:
            proved = proof(session, access, workspace_id, action=WorkspaceAction.EDIT)
            collection_header(session, collection_id, proved.id)
        return proved

    def mutate(
        operation, body, callback, *, access, workspace_id, collection_id=None, revisions=None
    ):
        key = request.headers.get("Idempotency-Key", "")
        try:
            services.idempotency.validate_key(key)
        except ValueError as error:
            return guards.error_response("idempotency_key_required", str(error), 400)
        with database.immediate_session() as session:
            proved = proof(session, access, workspace_id, action=WorkspaceAction.EDIT)
            collection_header(session, collection_id, proved.id)
            legacy = credential_namespace(session, proved.id).legacy_host_access
            principal = guards.principal()
            if not legacy:
                principal = replace(
                    principal, subject=f"workspace:{proved.id}:user:{access.actor.user_id}"
                )
            reservation = services.idempotency.begin(
                session,
                principal=principal,
                operation_id=operation,
                idempotency_key=key,
                payload=body,
            )
            if reservation.response is not None:
                result, status = reservation.response
                if (
                    not legacy
                    and (result.get("workspace_id") != proved.id or "can_edit" not in result)
                ) or ("workspace_id" in result and result["workspace_id"] != proved.id):
                    raise KeyError("receipt")
                response = jsonify(result)
                response.status_code = status
                response.headers["Idempotency-Replayed"] = "true"
                return response
            if revisions is not None and tts_revision_headers(session, proved.id) != revisions:
                raise RevisionConflict("The speech catalogue changed in another client.")
            result, status = callback(session, proved.id)
            result.update(envelope(proved))
            services.idempotency.complete(
                session,
                reservation,
                response=result,
                status_code=status,
                resource_kind="voice_catalog",
                resource_id=result.get("id"),
            )
        return jsonify(result), status

    @app.get("/api/v1/voice-catalog", endpoint="query_voice_catalog")
    @guards.require_scope("app.read")
    @errors
    def catalog_query():
        query = VoiceCatalogQuery.model_validate(request.args.to_dict())
        access = guards.access_context()
        maybe_initialize(access, query.workspace_id, query.session_id, query.collection_id)
        proved, entries, collections, _, _ = snapshot(
            access, query.workspace_id, query.session_id, collection_id=query.collection_id
        )
        query = query.model_copy(update={"workspace_id": proved.id})
        result = query_catalog(entries, query)
        result["collections"] = [
            {k: c[k] for k in ("id", "name", "revision", "member_count")} for c in collections
        ]
        result.update(envelope(recheck(access, proved.id, query.session_id)))
        return jsonify(result)

    @app.get("/api/v1/voice-catalog/capabilities", endpoint="voice_catalog_capabilities")
    @guards.require_scope("app.read")
    @errors
    def capabilities():
        workspace_id, session_id = roots()
        access = guards.access_context()
        proved, _, _, catalog, _ = snapshot(access, workspace_id, session_id, include_entries=False)
        service_id = normalize_service_id(request.args.get("service_id"))
        model_id = str(request.args.get("model") or "").casefold()
        result = {
            "schema_version": "1",
            "voice_profile_schema_version": 1,
            "features": [
                "catalog_search",
                "collections",
                "voice_profiles",
                "auditions",
                "reference_promotion",
                "passive_casting",
            ],
            "markup": {
                "format": "speech_xml",
                "version": 1,
                "guide": "generation-controls",
            },
            "models": [
                {
                    "service_id": s["id"],
                    "service_name": s.get("name"),
                    "model": m["id"],
                    "available": s.get("available"),
                    "listed": m["id"] in listed_models(s),
                    "availability_reason": s.get("availability_reason"),
                    "modes": model_modes(s, m),
                    "languages": supported_languages(s, m),
                    "license": m.get("license"),
                    "usage_note": m.get("usage_note"),
                }
                for s in catalog.get("services", [])
                if not service_id or normalize_service_id(s["id"]) == service_id
                for m in catalog_models(s)
                if not model_id or str(m["id"]).casefold() == model_id
            ],
        }
        result.update(envelope(recheck(access, proved.id, session_id)))
        return jsonify(result)

    @app.get("/api/v1/voice-collections", endpoint="list_voice_collections")
    @guards.require_scope("app.read")
    @errors
    def collection_list():
        workspace_id, session_id = roots()
        access = guards.access_context()
        with database.snapshot_session() as session:
            proved = proof(session, access, workspace_id, session_id)
            result = {"items": list_collections(session, workspace_id=proved.id)}
        result.update(envelope(recheck(access, proved.id, session_id)))
        return jsonify(result)

    @app.post("/api/v1/voice-collections", endpoint="create_voice_collection")
    @guards.require_scope("app.write")
    @errors
    def collection_create():
        workspace_id, _ = roots(write=True)
        access = guards.access_context()
        proved = preflight(access, workspace_id)
        body = VoiceCollectionCreate.model_validate(request.get_json(silent=True) or {})
        return mutate(
            "createVoiceCollection",
            body.model_dump(mode="json"),
            lambda session, wid: (create_collection(session, body, workspace_id=wid), 201),
            access=access,
            workspace_id=proved.id,
        )

    @app.patch("/api/v1/voice-collections/<collection_id>", endpoint="update_voice_collection")
    @guards.require_scope("app.write")
    @errors
    def collection_update(collection_id):
        workspace_id, _ = roots(write=True)
        access = guards.access_context()
        proved = preflight(access, workspace_id, collection_id=collection_id)
        body = VoiceCollectionUpdate.model_validate(request.get_json(silent=True) or {})
        known, revisions = set(), None
        if any(ref.kind == "provider" for ref in body.add_members):
            _, entries, _, _, revisions = snapshot(
                access,
                proved.id,
                action=WorkspaceAction.EDIT,
                collection_id=collection_id,
            )
            known = {item["key"] for item in entries}

        def update(session, wid):
            for ref in body.add_members:
                if ref.kind == "provider" and canonical_voice_key(ref) not in known:
                    raise ValueError("Provider voice is not present in the current catalog.")
            return update_collection(session, collection_id, body, workspace_id=wid), 200

        return mutate(
            "updateVoiceCollection",
            {"collection_id": collection_id, **body.model_dump(mode="json", exclude_unset=True)},
            update,
            access=access,
            workspace_id=proved.id,
            collection_id=collection_id,
            revisions=revisions,
        )

    @app.patch("/api/v1/voice-catalog/metadata", endpoint="update_catalog_voice_metadata")
    @guards.require_scope("app.write")
    @errors
    def update_metadata():
        workspace_id, _ = roots(write=True)
        access = guards.access_context()
        proved = preflight(access, workspace_id)
        body = CatalogVoiceUpdate.model_validate(request.get_json(silent=True) or {})
        if body.reference.kind != "provider":
            raise ValueError("Use the managed voice endpoint to edit a saved reference voice.")
        key = canonical_voice_key(body.reference)
        _, entries, _, _, revisions = snapshot(access, proved.id, action=WorkspaceAction.EDIT)

        def update(session, wid):
            if not any(item["key"] == key for item in entries):
                raise KeyError(key)
            return update_catalog_metadata(session, services.paths, body, workspace_id=wid), 200

        return mutate(
            "updateCatalogVoiceMetadata",
            body.model_dump(mode="json", exclude_unset=True),
            update,
            access=access,
            workspace_id=proved.id,
            revisions=revisions,
        )
