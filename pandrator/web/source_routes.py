"""HTTP routes for source library, documents, and ingestion."""

from __future__ import annotations

from flask import g, jsonify, request

from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import SessionRecord, SourceAsset
from .route_context import RouteContext
from .schemas import SourceAttachRequest, SourceReuseRequest, SourceUpdateRequest, SourceUrlRequest
from .settings_policy import RevisionConflict as WorkspaceRevisionConflict
from .source_document_reads import list_revision_words, list_session_documents


def register_source_library_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    idempotency: MutationIdempotency,
) -> None:
    services = context.services
    database = services.database
    sessions = services.sessions
    source_library = services.source_library
    require_auth = context.guards.require_auth
    error_response = context.guards.error_response
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    abandon_idempotency = idempotency.abandon

    @app.get("/api/v1/sources")
    @require_auth
    def source_library_list():
        try:
            return jsonify(
                {
                    "items": source_library.list(
                        include_trashed=request.args.get("include_trashed") == "true",
                        view=request.args.get("view", "full"),
                    )
                }
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 400)

    @app.get("/api/v1/sources/<source_asset_id>/references")
    @require_auth
    def source_library_references(source_asset_id: str):
        try:
            return jsonify(
                source_library.references(
                    source_asset_id,
                    limit=int(request.args.get("limit", 50)),
                    offset=int(request.args.get("offset", 0)),
                )
            )
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 400)

    @app.patch("/api/v1/sources/<source_asset_id>")
    @require_auth
    def source_library_update(source_asset_id: str):
        payload = SourceUpdateRequest.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current source revision.",
                428,
            )
        try:
            result = source_library.rename(source_asset_id, expected, payload.display_name)
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/sources/<source_asset_id>")
    @require_auth
    def source_library_trash(source_asset_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current source revision.",
                428,
            )
        try:
            result = source_library.set_state(source_asset_id, expected, "trashed")
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("source_in_use", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sources/<source_asset_id>/restore")
    @require_auth
    def source_library_restore(source_asset_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current source revision.",
                428,
            )
        try:
            result = source_library.set_state(source_asset_id, expected, "current")
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/sources")
    @require_auth
    def session_source_list(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"items": source_library.list(session_id=session_id)})

    @app.post("/api/v1/sessions/<session_id>/sources")
    @require_auth
    def session_source_attach(session_id: str):
        payload = SourceAttachRequest.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            raw_etag = request.headers.get(
                "If-Match",
                "",
            ).strip('W/" ')
            try:
                expected_session_revision = int(raw_etag)
            except ValueError:
                return error_response(
                    "precondition_required",
                    "If-Match must contain the current session revision.",
                    428,
                )
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="attachSessionSource",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "source_asset_id": (payload.source_asset_id),
                            "role": payload.role,
                            "expected_session_revision": (expected_session_revision),
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("session_revision") is not None:
                            response.headers["ETag"] = f'"{result["session_revision"]}"'
                        return response
                    session_record = db_session.get(
                        SessionRecord,
                        session_id,
                    )
                    if session_record is None:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "not_found",
                            "Session not found.",
                            404,
                        )
                    if session_record.revision != expected_session_revision:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "revision_conflict",
                            "The session changed before its source was attached.",
                            409,
                            {"current_revision": (session_record.revision)},
                        )
                    if (
                        db_session.get(
                            SourceAsset,
                            payload.source_asset_id,
                        )
                        is None
                    ):
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "not_found",
                            "Source asset not found.",
                            404,
                        )
                    result = source_library.attach(
                        session_id,
                        payload.source_asset_id,
                        role=payload.role,
                        expected_session_revision=(expected_session_revision),
                        db_session=db_session,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="session_source",
                        resource_id=str(result["id"]),
                    )
                    g.audit_resource_kind = "session_source"
                    g.audit_resource_id = str(result["id"])
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                if isinstance(error, WorkspaceRevisionConflict):
                    return error_response(
                        "revision_conflict",
                        str(error),
                        409,
                    )
                return idempotency_failure(error)
            response = jsonify(result)
            response.status_code = 201
            response.headers["ETag"] = f'"{result["session_revision"]}"'
            return response
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag) if raw_etag else None
        except ValueError:
            return error_response(
                "precondition_required", "If-Match must contain the current session revision.", 428
            )
        try:
            with database.immediate_session() as db_session:
                record = db_session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                result = source_library.attach(
                    session_id,
                    payload.source_asset_id,
                    role=payload.role,
                    expected_session_revision=record.revision if expected is None else expected,
                    db_session=db_session,
                )
        except KeyError:
            return error_response("not_found", "Session or source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["session_revision"]}"'
        return response, 201


def register_source_document_routes(app: DomainBlueprints, context: RouteContext) -> None:
    database = context.services.database
    source_library = context.services.source_library
    require_auth = context.guards.require_auth
    error_response = context.guards.error_response

    @app.post("/api/v1/sessions/<session_id>/sources/adopt-subtitles")
    @require_auth
    def session_source_adopt_subtitles(session_id: str):
        payload = SourceAttachRequest.model_validate(request.get_json(silent=True) or {})
        if payload.role != "primary":
            return error_response(
                "validation_error", "Only the primary subtitle source can be adopted.", 422
            )
        raw_revision = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_revision) if raw_revision else None
            result = source_library.adopt_subtitles(
                session_id, payload.source_asset_id, expected_session_revision=expected
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except KeyError:
            return error_response(
                "not_found",
                "Attach this subtitle source as the session's primary source first.",
                404,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["session_revision"]}"'
        return response, 200 if result["reused"] else 201

    @app.delete("/api/v1/sessions/<session_id>/sources/<attachment_id>")
    @require_auth
    def session_source_detach(session_id: str, attachment_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the attachment revision.",
                428,
            )
        try:
            source_library.detach(session_id, attachment_id, expected)
        except KeyError:
            return error_response("not_found", "Session source attachment not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        return "", 204

    @app.get("/api/v1/sessions/<session_id>/documents")
    @require_auth
    def session_documents(session_id: str):
        try:
            result = list_session_documents(database, session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify(result)

    @app.get("/api/v1/document-revisions/<revision_id>/words")
    @require_auth
    def revision_words(revision_id: str):
        try:
            cursor = max(0, int(request.args.get("cursor") or 0))
            limit = max(1, min(1000, int(request.args.get("limit") or 500)))
        except ValueError:
            return error_response("validation_error", "Invalid pagination value.", 422)
        try:
            result = list_revision_words(database, revision_id, cursor=cursor, limit=limit)
        except KeyError:
            return error_response("not_found", "Document revision not found.", 404)
        return jsonify(result)


def register_source_ingestion_routes(app: DomainBlueprints, context: RouteContext) -> None:
    sessions = context.services.sessions
    artifacts = context.services.artifacts
    jobs = context.services.jobs
    require_auth = context.guards.require_auth
    error_response = context.guards.error_response

    @app.post("/api/v1/sessions/<session_id>/sources/url")
    @require_auth
    def source_download_url(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        payload = SourceUrlRequest.model_validate(request.get_json(silent=True) or {})
        job = jobs.enqueue(
            "source.download_url",
            {"session_id": session_id, "url": payload.url},
            session_id=session_id,
        )
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/sessions/<session_id>/sources/reuse")
    @require_auth
    def source_reuse(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        payload = SourceReuseRequest.model_validate(request.get_json(silent=True) or {})
        try:
            artifacts.resolve(payload.artifact_id)
        except KeyError:
            return error_response("not_found", "Reusable source artifact not found.", 404)
        job = jobs.enqueue(
            "source.reuse",
            {"session_id": session_id, "artifact_id": payload.artifact_id},
            session_id=session_id,
        )
        return jsonify(_job_payload(job)), 202
