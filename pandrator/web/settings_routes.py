"""Public global defaults/settings adapters with actual scoped authority."""

from __future__ import annotations

from flask import jsonify, request

from .access.contracts import AccessDenied
from .accounts.actor_authorization import ActorAuthorizationDenied
from .domain_blueprints import DomainBlueprints
from .global_settings import SettingPreconditionError
from .route_context import RouteContext
from .schemas import SettingUpdate
from .settings_access import SettingsScopeInvalid


def register_settings_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth

    def reject_session():
        if "session_id" in request.args:
            raise SettingsScopeInvalid("Session scope is only available for defaults.")

    @app.get("/api/v1/defaults/<section>")
    @require_auth
    def global_default_get(section: str):
        try:
            result = services.global_settings.defaults(
                section,
                workspace_id=request.args.get("workspace_id"),
                session_id=request.args.get("session_id"),
                access_context=context.guards.access_context(),
            )
        except KeyError:
            return error_response("not_found", "Settings section not found.", 404)
        except SettingsScopeInvalid as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.get("/api/v1/settings/<setting_key>")
    @require_auth
    def setting_get(setting_key: str):
        try:
            reject_session()
            result = services.global_settings.get(
                setting_key,
                workspace_id=request.args.get("workspace_id"),
                access_context=context.guards.access_context(),
            )
        except KeyError:
            return error_response("not_found", "Setting not found.", 404)
        except SettingsScopeInvalid as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.put("/api/v1/settings/<setting_key>")
    @require_auth
    def setting_put(setting_key: str):
        if not setting_key or len(setting_key) > 120:
            return error_response("validation_error", "Invalid setting key.", 422)
        payload = SettingUpdate.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            reject_session()
            result = services.global_settings.replace(
                setting_key,
                payload.value,
                raw_etag,
                workspace_id=request.args.get("workspace_id"),
                access_context=context.guards.access_context(),
            )
        except KeyError:
            return error_response("not_found", "Setting not found.", 404)
        except SettingPreconditionError as error:
            return error_response(error.code, str(error), error.status_code)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        except (AccessDenied, ActorAuthorizationDenied):
            raise
        except (OSError, RuntimeError) as error:
            return error_response("credential_unavailable", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response
