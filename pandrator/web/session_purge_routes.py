"""HTTP registration for session deletion and trash policy."""

from __future__ import annotations

from flask import jsonify, request

from .domain_blueprints import DomainBlueprints
from .route_context import RouteContext
from .session_purge import PurgeBlocked, SessionPurgeService
from .sessions import RevisionConflict


def register_session_purge_routes(app: DomainBlueprints, context: RouteContext) -> None:
    service = SessionPurgeService(context.services.database, context.services.paths)
    error_response = context.guards.error_response
    require_scope = context.guards.require_scope

    @app.get("/api/v1/sessions/<session_id>/purge-preview")
    @require_scope("app.read")
    def session_purge_preview(session_id: str):
        try:
            return jsonify(service.preview(session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("unsafe_storage", str(error), 409)

    @app.post("/api/v1/sessions/<session_id>/purge")
    @require_scope("app.write")
    def session_purge(session_id: str):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"expected_revision", "impact_token"}:
            return error_response("invalid_request", "expected_revision and impact_token are required.", 400)
        revision = body.get("expected_revision")
        token = body.get("impact_token")
        if isinstance(revision, bool) or not isinstance(revision, int) or not isinstance(token, str) or len(token) != 64:
            return error_response("invalid_request", "expected_revision and impact_token are required.", 400)
        try:
            result = service.purge(session_id, revision, token)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except PurgeBlocked as error:
            return error_response("session_busy", str(error), 409, {"blockers": error.blockers})
        except ValueError as error:
            return error_response("unsafe_storage", str(error), 409)
        return jsonify(result)

    @app.get("/api/v1/session-trash-policy")
    @require_scope("app.read")
    def session_trash_policy_get():
        return jsonify(service.policy())

    @app.patch("/api/v1/session-trash-policy")
    @require_scope("app.write")
    def session_trash_policy_patch():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"expected_revision", "days"}:
            return error_response("invalid_request", "expected_revision and days are required.", 400)
        revision = body["expected_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int):
            return error_response("invalid_request", "expected_revision must be an integer.", 400)
        try:
            return jsonify(service.update_policy(revision, body["days"]))
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("invalid_request", str(error), 400)
