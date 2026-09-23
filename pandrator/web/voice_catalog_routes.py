"""Catalog search, persistent collections, and provider voice annotations."""

from __future__ import annotations

from typing import Literal

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Voice, VoiceCatalogOverride, utcnow
from .settings_policy import RevisionConflict
from .voice_catalog import (
    VoiceCatalogQuery,
    catalog_entries,
    catalog_models,
    listed_models,
    model_modes,
    query_catalog,
    supported_languages,
)
from .voice_catalog_schemas import (
    VoiceCollectionCreate,
    VoiceCollectionUpdate,
    VoiceProfile,
    VoiceReference,
    canonical_voice_key,
)
from .voice_collections import create_collection, list_collections, update_collection
from .voice_library import (
    ensure_bundled_voice,
    validate_profile_evidence,
    voice_payloads,
)


class CatalogVoiceChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    voice_category: Literal["male", "female", "androgynous", "unspecified"] | None = (
        None
    )
    profile: VoiceProfile | None = None

    @model_validator(mode="after")
    def validate_changes(self):
        if not self.model_fields_set:
            raise ValueError("At least one metadata field is required.")
        if self.name is not None and not self.name.strip():
            raise ValueError("Voice name cannot be blank.")
        return self


class CatalogVoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: VoiceReference
    expected_revision: int = Field(ge=0)
    changes: CatalogVoiceChanges


def register_voice_catalog_routes(app, context):
    services, guards = context.services, context.guards

    def failure(error):
        if isinstance(error, KeyError):
            return guards.error_response(
                "not_found", "Voice or collection not found.", 404
            )
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(error.code, str(error), 409)
        return guards.error_response("validation_error", str(error), 422)

    def snapshot():
        ensure_bundled_voice(services.database, services.paths, services.artifacts)
        catalog, _ = services.tts_catalogue.snapshot(refresh=False)
        with services.database.session() as session:
            voices = voice_payloads(
                session, services.paths, session.scalars(select(Voice)).all()
            )
            collections = list_collections(session)
            overrides = {
                row.voice_key: {
                    "name": row.name,
                    "description": row.description,
                    "voice_category": row.voice_category,
                    "profile": row.profile_json,
                    "revision": row.revision,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in session.scalars(select(VoiceCatalogOverride)).all()
            }
        return (
            catalog_entries(voices, catalog, collections, overrides),
            collections,
            catalog,
        )

    def mutate(operation, body, callback):
        key = request.headers.get("Idempotency-Key", "")
        try:
            services.idempotency.validate_key(key)
        except ValueError as error:
            return guards.error_response("idempotency_key_required", str(error), 400)
        with services.database.immediate_session() as session:
            reservation = services.idempotency.begin(
                session,
                principal=guards.principal(),
                operation_id=operation,
                idempotency_key=key,
                payload=body,
            )
            if reservation.response is not None:
                result, status = reservation.response
                response = jsonify(result)
                response.status_code = status
                response.headers["Idempotency-Replayed"] = "true"
                return response
            result, status = callback(session)
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
    def catalog_query():
        try:
            query = VoiceCatalogQuery.model_validate(request.args.to_dict())
            entries, collections, _ = snapshot()
            result = query_catalog(entries, query)
            result["collections"] = [
                {k: c[k] for k in ("id", "name", "revision", "member_count")}
                for c in collections
            ]
            return jsonify(result)
        except (ValueError, TypeError) as error:
            return failure(error)

    @app.get(
        "/api/v1/voice-catalog/capabilities", endpoint="voice_catalog_capabilities"
    )
    @guards.require_scope("app.read")
    def capabilities():
        catalog, _ = services.tts_catalogue.snapshot(refresh=False)
        return jsonify(
            {
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
                    for m in catalog_models(s)
                ],
            }
        )

    @app.get("/api/v1/voice-collections", endpoint="list_voice_collections")
    @guards.require_scope("app.read")
    def collection_list():
        with services.database.session() as session:
            return jsonify({"items": list_collections(session)})

    @app.post("/api/v1/voice-collections", endpoint="create_voice_collection")
    @guards.require_scope("app.write")
    def collection_create():
        try:
            body = VoiceCollectionCreate.model_validate(
                request.get_json(silent=True) or {}
            )
            return mutate(
                "createVoiceCollection",
                body.model_dump(mode="json"),
                lambda session: (create_collection(session, body), 201),
            )
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)

    @app.patch(
        "/api/v1/voice-collections/<collection_id>", endpoint="update_voice_collection"
    )
    @guards.require_scope("app.write")
    def collection_update(collection_id):
        try:
            body = VoiceCollectionUpdate.model_validate(
                request.get_json(silent=True) or {}
            )
            entries, _, _ = snapshot()
            known = {item["key"] for item in entries}

            def update(session):
                for ref in body.add_members:
                    if ref.kind == "provider" and canonical_voice_key(ref) not in known:
                        raise ValueError(
                            "Provider voice is not present in the current catalog."
                        )
                return update_collection(session, collection_id, body), 200

            return mutate(
                "updateVoiceCollection",
                {
                    "collection_id": collection_id,
                    **body.model_dump(mode="json", exclude_unset=True),
                },
                update,
            )
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)

    @app.patch(
        "/api/v1/voice-catalog/metadata", endpoint="update_catalog_voice_metadata"
    )
    @guards.require_scope("app.write")
    def update_metadata():
        try:
            body = CatalogVoiceUpdate.model_validate(
                request.get_json(silent=True) or {}
            )
            if body.reference.kind != "provider":
                raise ValueError(
                    "Use the managed voice endpoint to edit a saved reference voice."
                )
            key = canonical_voice_key(body.reference)
            entries, _, _ = snapshot()

            def update(session):
                if not any(item["key"] == key for item in entries):
                    raise KeyError(key)
                row = session.get(VoiceCatalogOverride, key)
                if (row.revision if row else 0) != body.expected_revision:
                    raise RevisionConflict(
                        "The voice metadata changed in another client."
                    )
                if row is None:
                    row = VoiceCatalogOverride(
                        voice_key=key,
                        voice_ref_json=body.reference.model_dump(exclude_none=True),
                        revision=0,
                    )
                    session.add(row)
                changes = body.changes.model_dump(mode="json", exclude_unset=True)
                validate_profile_evidence(
                    session, services.paths, changes.get("profile")
                )
                if (
                    "voice_category" in changes
                    and changes["voice_category"] != row.voice_category
                    and "profile" not in changes
                    and row.profile_json
                ):
                    profile = dict(row.profile_json)
                    evidence = dict(profile.get("evidence") or {})
                    evidence.pop("voice_category", None)
                    profile["evidence"] = evidence
                    row.profile_json = profile
                for name, value in changes.items():
                    setattr(
                        row,
                        "profile_json" if name == "profile" else name,
                        value.strip() if name == "name" and value else value,
                    )
                row.revision += 1
                row.updated_at = utcnow()
                session.flush()
                return {
                    "key": key,
                    "reference": row.voice_ref_json,
                    "revision": row.revision,
                    "name": row.name,
                    "description": row.description,
                    "voice_category": row.voice_category,
                    "profile": row.profile_json,
                }, 200

            return mutate(
                "updateCatalogVoiceMetadata",
                body.model_dump(mode="json", exclude_unset=True),
                update,
            )
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)
