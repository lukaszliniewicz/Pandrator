"""HTTP routes for session settings."""

from __future__ import annotations

from flask import g, jsonify, request

from .access.contracts import ProjectAction
from .access.generation_resource_policy import require_generation_resource_access
from .credentials import redact_inline_secrets
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_session_access import require_session_action
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import SessionRecord, SessionSetting
from .project_settings_policy import public_project_settings
from .route_context import RouteContext
from .schemas import SessionSettingsUpdate
from .settings_policy import RevisionConflict as WorkspaceRevisionConflict


def register_session_settings_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    idempotency: MutationIdempotency,
) -> None:
    services = context.services

    def public_response(value):
        return jsonify(public_project_settings(value))

    database = services.database
    workspace_settings = services.workspace_settings
    error_response = context.guards.error_response
    inline_credential_error = context.guards.inline_credential_error
    require_auth = context.guards.require_auth
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    abandon_idempotency = idempotency.abandon

    def save_tts_settings(session_id, expected, value, key, *, patch=False):
        from .project_tts_settings import write_project_tts_settings

        try:
            written = write_project_tts_settings(
                database, services.paths, workspace_settings,
                session_id=session_id, expected_revision=expected, value=value, patch=patch,
                idempotency=services.idempotency,
                principal=idempotency.principal() if key is not None else None,
                idempotency_key=key,
            )
        except KeyError:
            return error_response("not_found", "Session or voice not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        response = public_response(written.value)
        response.status_code = written.status_code
        if written.replayed:
            response.headers["Idempotency-Replayed"] = "true"
        else:
            g.audit_resource_kind = "session_settings"
            g.audit_resource_id = f"{session_id}:tts"
        if written.value.get("revision") is not None:
            response.headers["ETag"] = f'"{written.value["revision"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    @require_session_action(context, ProjectAction.READ)
    def session_settings_get(session_id: str, section: str):
        try:
            result = workspace_settings.get(session_id, section)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = public_response(redact_inline_secrets(result))
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.put("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    @require_session_action(context, ProjectAction.EDIT)
    def session_settings_put(session_id: str, section: str):
        payload = SessionSettingsUpdate.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.value):
            return rejected
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current settings revision.",
                428,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if section == "tts":
            return save_tts_settings(session_id, expected, payload.value, idempotency_key)
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    require_generation_resource_access(db_session, "session", session_id, ProjectAction.EDIT)
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="putSessionSettings",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "section": section,
                            "expected_revision": expected,
                            "value": payload.value,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = public_response(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
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
                    current = db_session.get(
                        SessionSetting,
                        (session_id, section),
                    )
                    current_revision = current.revision if current is not None else 0
                    if current_revision != expected:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "revision_conflict",
                            "Session settings changed in another client.",
                            409,
                            {"current_revision": (current_revision)},
                        )
                    result = workspace_settings.update(
                        session_id,
                        section,
                        expected,
                        payload.value,
                        db_session=db_session,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="session_settings",
                        resource_id=f"{session_id}:{section}",
                    )
                    g.audit_resource_kind = "session_settings"
                    g.audit_resource_id = f"{session_id}:{section}"
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
            response = public_response(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            result = workspace_settings.update(session_id, section, expected, payload.value)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = public_response(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.patch("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    @require_session_action(context, ProjectAction.EDIT)
    def session_settings_patch(session_id: str, section: str):
        payload = SessionSettingsUpdate.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.value):
            return rejected
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current settings revision.",
                428,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if section == "tts":
            return save_tts_settings(session_id, expected, payload.value, idempotency_key, patch=True)
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    require_generation_resource_access(db_session, "session", session_id, ProjectAction.EDIT)
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="patchSessionSettings",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "section": section,
                            "expected_revision": expected,
                            "value": payload.value,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = public_response(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    session_record = db_session.get(SessionRecord, session_id)
                    if session_record is None:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "not_found",
                            "Session not found.",
                            404,
                        )
                    current = db_session.get(
                        SessionSetting,
                        (session_id, section),
                    )
                    current_revision = current.revision if current is not None else 0
                    if current_revision != expected:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "revision_conflict",
                            "Session settings changed in another client.",
                            409,
                            {"current_revision": (current_revision)},
                        )
                    result = workspace_settings.patch(
                        session_id,
                        section,
                        expected,
                        payload.value,
                        db_session=db_session,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="session_settings",
                        resource_id=f"{session_id}:{section}",
                    )
                    g.audit_resource_kind = "session_settings"
                    g.audit_resource_id = f"{session_id}:{section}"
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
            response = public_response(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            result = workspace_settings.patch(session_id, section, expected, payload.value)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = public_response(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/settings/resolve")
    @require_auth
    @require_session_action(context, ProjectAction.READ)
    def session_settings_resolve(session_id: str):
        body = request.get_json(silent=True) or {}
        sections = body.get("sections") if isinstance(body.get("sections"), list) else None
        overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
        if rejected := inline_credential_error(overrides):
            return rejected
        try:
            value, digest = workspace_settings.resolve(
                session_id, sections=sections, run_override=overrides
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        # Output assemblies hash only the assembly-material subset (audio plus
        # a few output keys). Expose that comparable digest so callers can tell
        # a current assembly from a stale one; the full resolve digest never
        # equals an assembly settings_hash.
        assembly_digest = None
        try:
            from .workspace import output_assembly_settings_hash

            if sections is None or {"audio", "output"}.issubset(set(sections)):
                assembly_digest = output_assembly_settings_hash(value)
        except (TypeError, ValueError, AttributeError):
            assembly_digest = None
        return public_response(
            {
                "value": value,
                "settings_hash": digest,
                "assembly_settings_hash": assembly_digest,
            }
        )
