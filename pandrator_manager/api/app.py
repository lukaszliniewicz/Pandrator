"""Authenticated loopback Flask adapter for manager application use cases."""

from __future__ import annotations

import hmac
import html
import ipaddress
import logging
import sqlite3
import subprocess
import threading
import time
from functools import wraps
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    make_response,
    redirect,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import __version__
from ..application import ManagerApplication
from ..auth import (
    ManagerAutomationRateLimiter,
    ManagerAutomationService,
    RecoverySessionManager,
    derive_browser_session_keys,
)
from ..diagnostics import build_diagnostic_bundle
from ..errors import ManagerError, NotFoundError
from ..models import API_VERSION
from ..network import (
    EndpointExposure,
    NetworkConfiguration,
    private_network_candidates,
    save_network_configuration,
)
from ..processes import CommandRunner, CommandSpec
from ..runtime_specs import (
    PANDRATOR_API_SERVICE,
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_WORKER_SERVICE,
    pandrator_runtime_specs,
)
from ..supervisor import ProcessSupervisor
from .openapi import build_openapi
from .schemas import (
    ApplicationNetworkRequest,
    AutomationEnrollmentGrantRequest,
    AutomationTokenRequest,
    LegacyImportRequest,
    OperationRequest,
    PlanRequest,
    RecoveryExchangeRequest,
    ReleasePlanRequest,
    RuntimeRequest,
    UninstallPlanRequest,
)

RECOVERY_COOKIE = "pandrator_manager_session"


def _is_loopback(value: str | None) -> bool:
    candidate = str(value or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped and mapped.is_loopback))


def _host_name(value: str) -> str:
    try:
        return str(urlsplit(f"//{value}").hostname or "")
    except ValueError:
        return ""


def _tail(path: Path, maximum_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - maximum_bytes))
            payload = handle.read(maximum_bytes)
    except FileNotFoundError:
        return ""
    return payload.decode("utf-8", errors="replace")


def _browser_handoff_url(
    response: requests.Response,
    browser_base_url: str,
) -> str:
    base_url = browser_base_url.rstrip("/")
    if response.status_code in {404, 405}:
        # Migrated Pandrator builds predate the manager-authenticated,
        # single-use handoff. Their owner-password login remains the safe
        # compatibility path.
        return f"{base_url}/"
    if response.status_code != 200:
        raise ManagerError(
            "application_launch_failed",
            "Pandrator is running, but the browser sign-in handoff was "
            f"rejected (HTTP {response.status_code}).",
            http_status=409,
        )
    try:
        token = str(response.json()["token"]).strip()
    except (KeyError, TypeError, ValueError) as error:
        raise ManagerError(
            "application_launch_failed",
            "Pandrator returned an invalid browser sign-in handoff.",
            http_status=409,
        ) from error
    if not token:
        raise ManagerError(
            "application_launch_failed",
            "Pandrator returned an invalid browser sign-in handoff.",
            http_status=409,
        )
    return f"{base_url}/#bootstrap={token}"


def create_api(
    application: ManagerApplication,
    supervisor: ProcessSupervisor,
    *,
    client_secret: str,
    recovery_sessions: RecoverySessionManager | None = None,
    shutdown_callback: Callable[[], None] | None = None,
    manager_exposure: EndpointExposure | None = None,
    application_exposure: EndpointExposure | None = None,
    application_environment: dict[str, str] | None = None,
    automation_rate_limiter: ManagerAutomationRateLimiter | None = None,
) -> Flask:
    api = Flask(
        "pandrator_manager",
        static_folder=None,
    )
    api.config.update(
        MAX_CONTENT_LENGTH=1024 * 1024,
        JSON_SORT_KEYS=True,
    )
    selected_manager_exposure = manager_exposure or EndpointExposure(port=0)
    application_exposure_state = {
        "value": application_exposure or EndpointExposure(port=8097)
    }
    if selected_manager_exposure.proxy_hops:
        api.wsgi_app = ProxyFix(
            api.wsgi_app,
            x_for=selected_manager_exposure.proxy_hops,
            x_proto=selected_manager_exposure.proxy_hops,
            x_host=selected_manager_exposure.proxy_hops,
            x_port=selected_manager_exposure.proxy_hops,
        )
    if recovery_sessions is None:
        security_context, csrf_secret = derive_browser_session_keys(
            client_secret,
            selected_manager_exposure.model_dump(mode="json"),
        )
        private_http = (
            selected_manager_exposure.mode.value == "private_network"
        )
        sessions = RecoverySessionManager(
            store=application.store,
            security_context=security_context,
            csrf_secret=csrf_secret,
            session_ttl_seconds=12 * 60 * 60,
            remembered_idle_ttl_seconds=(
                7 * 24 * 60 * 60 if private_http else 30 * 24 * 60 * 60
            ),
            remembered_absolute_ttl_seconds=(
                30 * 24 * 60 * 60 if private_http else 90 * 24 * 60 * 60
            ),
        )
    else:
        sessions = recovery_sessions
    recovery_static = Path(__file__).resolve().parent.parent / "recovery_ui" / "static"
    mutation_lock = threading.Lock()
    automation = ManagerAutomationService(
        application.store,
        manager_instance_id=str(application.instance_id or ""),
        canonical_recovery_origin=str(
            selected_manager_exposure.public_url or ""
        ),
        enabled=(
            selected_manager_exposure.mode.value == "https_proxy"
        ),
    )
    recovery_rate_limiter = (
        automation_rate_limiter or ManagerAutomationRateLimiter()
    )

    automation_read_endpoints = {
        "status",
        "inventory",
        "capabilities",
        "components",
        "component",
        "doctor",
        "services",
        "service",
        "application_status",
        "releases",
        "manager_update",
        "operation",
        "operation_tasks",
        "activity",
        "automation_principal",
    }
    automation_plan_endpoints = {
        "plans",
        "release_plans",
        "uninstall_plans",
    }
    automation_runtime_endpoints = {
        "application_start",
        "application_stop",
        "application_restart",
        "runtime_start",
        "runtime_stop",
        "runtime_restart",
    }
    automation_mutate_endpoints = {
        "cancel_operation",
    }

    def automation_scope_for_request() -> str | None:
        endpoint = str(request.endpoint or "")
        if endpoint == "operations":
            return (
                "manager.read"
                if request.method == "GET"
                else "manager.mutate"
            )
        if endpoint in automation_read_endpoints:
            return "manager.read"
        if endpoint in automation_plan_endpoints:
            return "manager.read"
        if endpoint in automation_runtime_endpoints:
            return "manager.runtime"
        if endpoint in automation_mutate_endpoints:
            return "manager.mutate"
        return None

    def session_policy() -> dict:
        return {
            "mode": selected_manager_exposure.mode.value,
            "insecure_private_http": (
                selected_manager_exposure.mode.value == "private_network"
            ),
            "transient_ttl_seconds": sessions.session_ttl_seconds,
            "remembered_idle_ttl_seconds": (
                sessions.remembered_idle_ttl_seconds
            ),
            "remembered_absolute_ttl_seconds": (
                sessions.remembered_absolute_ttl_seconds
            ),
        }

    def session_payload(selected) -> dict:
        active_sessions = sessions.sessions(selected.session_id)
        return {
            "csrf_token": selected.csrf_token,
            "session": {
                "id": selected.record_id,
                "remembered": selected.remembered,
                "created_at": selected.created_at,
                "last_seen_at": selected.last_seen_at,
                "expires_at": selected.expires_at,
                "absolute_expires_at": selected.absolute_expires_at,
                "user_agent": selected.user_agent,
            },
            "active_session_count": len(active_sessions),
            "policy": session_policy(),
        }

    def set_session_cookie(response, selected) -> None:
        max_age = None
        if selected.remembered:
            max_age = max(
                1,
                int(selected.absolute_expires_at - time.time()),
            )
        response.set_cookie(
            RECOVERY_COOKIE,
            selected.session_id,
            httponly=True,
            secure=selected_manager_exposure.secure_cookies,
            samesite="Strict",
            path="/",
            max_age=max_age,
        )

    def clear_session_cookie(response) -> None:
        response.delete_cookie(
            RECOVERY_COOKIE,
            secure=selected_manager_exposure.secure_cookies,
            httponly=True,
            samesite="Strict",
            path="/",
        )

    @api.after_request
    def security_headers(response):
        if application.instance_id:
            response.headers["X-Pandrator-Manager-Instance"] = (
                application.instance_id
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; connect-src 'self'; "
            "style-src 'self'; script-src 'self'"
        )
        if selected_manager_exposure.secure_cookies and request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith("/recovery"):
            response.headers["Cache-Control"] = "no-store"
        if request.path in {
            "/v1/session",
            "/v1/browser-sessions",
            "/v1/recovery/exchange",
            "/v1/diagnostics/bundle",
        } or request.path.startswith("/v1/automation/"):
            response.headers["Cache-Control"] = "no-store"
        principal = getattr(
            g,
            "manager_automation_principal",
            None,
        )
        if isinstance(principal, dict):
            application.context.event_sink.emit(
                "automation.request",
                {
                    "subject": str(principal.get("subject") or "")[:200],
                    "client_id": str(
                        principal.get("client_id") or ""
                    )[:80],
                    "scopes": list(principal.get("scopes") or ()),
                    "method": request.method,
                    "path": request.path[:300],
                    "status_code": response.status_code,
                    "request_id": str(
                        request.headers.get("X-Request-ID") or ""
                    )[:120],
                    "traceparent": str(
                        request.headers.get("traceparent") or ""
                    )[:160],
                },
            )
        return response

    def idempotent(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                return handler(*args, **kwargs)
            key = request.headers.get("Idempotency-Key", "").strip()
            if not key:
                return (
                    jsonify(
                        error={
                            "code": "idempotency_key_required",
                            "message": "Idempotency-Key is required.",
                        }
                    ),
                    400,
                )

            def redact(value):
                if isinstance(value, dict):
                    return {
                        str(key): (
                            "<redacted>"
                            if any(
                                marker in str(key).lower()
                                for marker in ("password", "secret", "token")
                            )
                            else redact(item)
                        )
                        for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [redact(item) for item in value]
                return value

            request_payload = {
                "method": request.method,
                "path": request.path,
                "body": redact(request.get_json(silent=True)),
            }
            with mutation_lock:
                replay = application.store.api_idempotency_result(
                    key,
                    request_payload,
                )
                if replay is not None:
                    payload, status_code = replay
                    return jsonify(payload), status_code
                result = make_response(handler(*args, **kwargs))
                if result.status_code < 400:
                    payload = result.get_json(silent=True)
                    if isinstance(payload, dict):
                        application.store.record_api_idempotency(
                            key,
                            request_payload,
                            payload,
                            result.status_code,
                        )
                return result

        return wrapped

    @api.before_request
    def enforce_boundary():
        if (
            not selected_manager_exposure.remote_enabled
            and not _is_loopback(request.remote_addr)
        ):
            return jsonify(error={"code": "loopback_required"}), 403
        host = _host_name(request.host).lower().rstrip(".")
        if host not in selected_manager_exposure.allowed_hosts:
            return jsonify(error={"code": "invalid_host"}), 400
        origin = request.headers.get("Origin")
        if origin:
            parsed_origin = urlsplit(origin)
            expected_origin = (
                selected_manager_exposure.public_url
                if selected_manager_exposure.remote_enabled
                else f"{request.scheme}://{request.host}"
            )
            parsed_expected = urlsplit(str(expected_origin))
            try:
                invalid_origin = (
                    parsed_origin.scheme not in {"http", "https"}
                    or parsed_origin.scheme != parsed_expected.scheme
                    or parsed_origin.hostname != parsed_expected.hostname
                    or parsed_origin.port != parsed_expected.port
                )
            except ValueError:
                invalid_origin = True
            if invalid_origin:
                return jsonify(error={"code": "invalid_origin"}), 403

        public = {
            "health",
            "recovery_index",
            "recovery_asset",
            "recovery_exchange",
            "automation_identity",
            "automation_token",
        }
        if request.endpoint in public:
            return None
        authorization = request.headers.get("Authorization", "")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        bearer_valid = (
            bool(bearer)
            and _is_loopback(request.remote_addr)
            and hmac.compare_digest(bearer, client_secret)
        )
        automation_principal = (
            None
            if bearer_valid or not bearer
            else automation.authenticate(bearer)
        )
        session_id = request.cookies.get(RECOVERY_COOKIE)
        selected_session = sessions.authenticate(session_id)
        requires_csrf = request.method not in {"GET", "HEAD", "OPTIONS"}
        supplied_csrf = request.headers.get("X-CSRF-Token", "")
        if (
            request.endpoint == "automation_authorize"
            and request.method == "POST"
        ):
            supplied_csrf = str(
                request.form.get("csrf_token") or supplied_csrf
            )
        session_valid = selected_session is not None and (
            not requires_csrf
            or hmac.compare_digest(
                selected_session.csrf_token,
                supplied_csrf,
            )
        )
        g.recovery_session = selected_session if session_valid else None
        g.manager_bearer_authenticated = bearer_valid
        g.manager_automation_principal = automation_principal
        if automation_principal is not None:
            retry_after = recovery_rate_limiter.consume(
                str(automation_principal.get("client_id") or "")
            )
            if retry_after is not None:
                response = jsonify(
                    error={
                        "code": "automation_rate_limited",
                        "message": (
                            "The Manager recovery client exceeded its "
                            "request rate limit."
                        ),
                    }
                )
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response
            required_scope = automation_scope_for_request()
            if required_scope is None:
                return (
                    jsonify(
                        error={
                            "code": "automation_route_denied",
                            "message": (
                                "This Manager route is not available to "
                                "automation principals."
                            ),
                        }
                    ),
                    403,
                )
            if required_scope not in set(
                automation_principal.get("scopes") or ()
            ):
                return (
                    jsonify(
                        error={
                            "code": "scope_denied",
                            "message": (
                                "The Manager automation principal lacks "
                                f"{required_scope}."
                            ),
                        }
                    ),
                    403,
                )
            return None
        if not bearer_valid and not session_valid:
            response = jsonify(error={"code": "authentication_required"})
            if session_id and selected_session is None:
                clear_session_cookie(response)
            return response, 401
        return None

    @api.errorhandler(ManagerError)
    def manager_error(error: ManagerError):
        return (
            jsonify(
                error={
                    "code": error.code,
                    "message": error.message,
                    "details": error.details or {},
                }
            ),
            error.http_status,
        )

    @api.errorhandler(ValidationError)
    def validation_error(error: ValidationError):
        return (
            jsonify(
                error={
                    "code": "invalid_request",
                    "message": "Request validation failed.",
                    "details": error.errors(
                        include_url=False,
                        include_context=False,
                    ),
                }
            ),
            400,
        )

    @api.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        return (
            jsonify(
                error={
                    "code": error.name.lower().replace(" ", "_"),
                    "message": error.description,
                }
            ),
            int(error.code or 500),
        )

    @api.errorhandler(Exception)
    def unexpected_error(error: Exception):
        logging.exception("Unhandled manager API error", exc_info=error)
        return (
            jsonify(
                error={
                    "code": "internal_error",
                    "message": "The manager could not complete the request.",
                }
            ),
            500,
        )

    @api.get("/v1/health")
    def health():
        return jsonify(
            status="ok",
            service="pandrator-manager",
            protocol_version=API_VERSION,
            version=__version__,
            instance_id=application.instance_id,
        )

    @api.get("/v1/automation/identity")
    def automation_identity():
        return jsonify(automation.identity())

    @api.route(
        "/v1/automation/authorize",
        methods=["GET", "POST"],
    )
    def automation_authorize():
        if request.method == "GET":
            try:
                pending = automation.begin_authorization(
                    {
                        "client_id": request.args.get("client_id"),
                        "client_name": request.args.get("client_name"),
                        "subject": request.args.get("subject"),
                        "application_instance_id": request.args.get(
                            "application_instance_id"
                        ),
                        "canonical_application_origin": request.args.get(
                            "canonical_application_origin"
                        ),
                        "canonical_recovery_origin": request.args.get(
                            "canonical_recovery_origin"
                        ),
                        "requested_scopes": request.args.get(
                            "scope",
                            "",
                        ),
                        "expires_in_seconds": request.args.get(
                            "expires_in_seconds",
                            "604800",
                        ),
                        "code_challenge": request.args.get(
                            "code_challenge"
                        ),
                        "code_challenge_method": request.args.get(
                            "code_challenge_method"
                        ),
                        "redirect_uri": request.args.get("redirect_uri"),
                        "state": request.args.get("state"),
                    }
                )
            except (PermissionError, ValueError) as error:
                return (
                    jsonify(
                        error={
                            "code": "invalid_automation_request",
                            "message": str(error),
                        }
                    ),
                    400,
                )
            selected_session = g.recovery_session
            return Response(
                (
                    "<!doctype html><html lang=\"en\"><head>"
                    "<meta charset=\"utf-8\"><meta name=\"viewport\" "
                    "content=\"width=device-width,initial-scale=1\">"
                    "<title>Authorize Pandrator recovery agent</title>"
                    "<link rel=\"stylesheet\" href=\"/recovery/styles.css\">"
                    "</head><body><main><section class=\"panel\">"
                    "<p class=\"eyebrow\">Agent access</p>"
                    "<h1>Authorize recovery access?</h1>"
                    f"<p><strong>{html.escape(pending.client_name)}</strong> "
                    "is requesting a separate, expiring Manager credential.</p>"
                    "<dl>"
                    f"<dt>Subject</dt><dd>{html.escape(pending.subject)}</dd>"
                    f"<dt>Application</dt><dd>{html.escape(pending.canonical_application_origin)}</dd>"
                    f"<dt>Recovery endpoint</dt><dd>{html.escape(pending.canonical_recovery_origin)}</dd>"
                    f"<dt>Scopes</dt><dd>{html.escape(', '.join(pending.scopes))}</dd>"
                    f"<dt>Expires in</dt><dd>{pending.expires_in_seconds // 3600} hours</dd>"
                    "</dl><p>This credential cannot change network settings, "
                    "read arbitrary files, reveal the permanent Manager "
                    "credential, or bypass exact operation plans.</p>"
                    "<form method=\"post\" action=\"/v1/automation/authorize\">"
                    f"<input type=\"hidden\" name=\"authorization_nonce\" value=\"{html.escape(pending.nonce)}\">"
                    f"<input type=\"hidden\" name=\"csrf_token\" value=\"{html.escape(selected_session.csrf_token)}\">"
                    "<button class=\"button primary\" name=\"decision\" "
                    "value=\"approve\" type=\"submit\">Authorize</button> "
                    "<button class=\"button secondary\" name=\"decision\" "
                    "value=\"deny\" type=\"submit\">Deny</button>"
                    "</form></section></main></body></html>"
                ),
                content_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )

        nonce = str(
            request.form.get("authorization_nonce") or ""
        )
        decision = str(request.form.get("decision") or "")
        try:
            if decision == "approve":
                pending, grant_code = automation.approve(nonce)
                callback = automation.callback_url(
                    pending,
                    code=grant_code,
                )
            else:
                pending = automation.deny(nonce)
                if pending is None:
                    raise ValueError(
                        "The Manager automation authorization expired."
                    )
                callback = automation.callback_url(
                    pending,
                    error="access_denied",
                )
        except (PermissionError, ValueError) as error:
            return (
                jsonify(
                    error={
                        "code": "invalid_automation_request",
                        "message": str(error),
                    }
                ),
                400,
            )
        if callback.startswith("urn:"):
            return Response(
                (
                    "<!doctype html><html lang=\"en\"><head>"
                    "<meta charset=\"utf-8\"><title>Manager agent access</title>"
                    "<link rel=\"stylesheet\" href=\"/recovery/styles.css\">"
                    "</head><body><main><section class=\"panel\">"
                    "<h1>Authorization result</h1>"
                    f"<p><code>{html.escape(callback)}</code></p>"
                    "<p>Return this value to the waiting trusted CLI.</p>"
                    "</section></main></body></html>"
                ),
                content_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
        return redirect(callback, code=302)

    @api.post("/v1/automation/enrollment-grants")
    def automation_enrollment_grant():
        payload = AutomationEnrollmentGrantRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        try:
            result = automation.create_grant(
                payload.model_dump(mode="python")
            )
        except PermissionError as error:
            return (
                jsonify(
                    error={
                        "code": "automation_disabled",
                        "message": str(error),
                    }
                ),
                403,
            )
        except ValueError as error:
            return (
                jsonify(
                    error={
                        "code": "invalid_automation_request",
                        "message": str(error),
                    }
                ),
                400,
            )
        return jsonify(result), 201

    @api.post("/v1/automation/token")
    def automation_token():
        payload = AutomationTokenRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        try:
            result = automation.exchange(
                payload.model_dump(mode="python")
            )
        except PermissionError as error:
            return (
                jsonify(
                    error={
                        "code": "automation_disabled",
                        "message": str(error),
                    }
                ),
                403,
            )
        except (UnicodeError, ValueError) as error:
            return (
                jsonify(
                    error={
                        "code": "invalid_grant",
                        "message": str(error),
                    }
                ),
                400,
            )
        return jsonify(result)

    @api.get("/v1/automation/principal")
    def automation_principal():
        principal = getattr(
            g,
            "manager_automation_principal",
            None,
        )
        if not isinstance(principal, dict):
            return (
                jsonify(
                    error={
                        "code": "automation_authentication_required",
                        "message": (
                            "A Manager recovery automation credential "
                            "is required."
                        ),
                    }
                ),
                401,
            )
        return jsonify(automation.project_principal(principal))

    @api.get("/v1/automation/clients")
    def automation_clients():
        if (
            g.recovery_session is None
            and not g.manager_bearer_authenticated
        ):
            return (
                jsonify(
                    error={
                        "code": "owner_control_required",
                        "message": (
                            "An authorized recovery browser or local Manager "
                            "client is required."
                        ),
                    }
                ),
                403,
            )
        return jsonify(items=automation.clients())

    @api.delete("/v1/automation/clients/<client_id>")
    @idempotent
    def automation_client_revoke(client_id: str):
        if (
            g.recovery_session is None
            and not g.manager_bearer_authenticated
        ):
            return (
                jsonify(
                    error={
                        "code": "owner_control_required",
                        "message": (
                            "An authorized recovery browser or local Manager "
                            "client is required."
                        ),
                    }
                ),
                403,
            )
        if not automation.revoke(client_id):
            raise NotFoundError(
                "Manager automation client was not found.",
                {"client_id": client_id},
            )
        return jsonify(revoked=True, client_id=client_id)

    @api.get("/v1/status")
    def status():
        return jsonify(application.status().model_dump(mode="json"))

    @api.get("/v1/inventory")
    def inventory():
        return jsonify(
            health={
                "status": "ok",
                "service": "pandrator-manager",
                "protocol_version": API_VERSION,
                "version": __version__,
                "instance_id": application.instance_id,
            },
            status=application.status().model_dump(mode="json"),
            components=application.list_components(),
            services=[
                service.model_dump(mode="json")
                for service in supervisor.snapshot()
            ],
        )

    @api.get("/v1/capabilities")
    def capabilities():
        status_payload = application.status()
        return jsonify(
            manager_version=status_payload.manager_version,
            api_versions=status_payload.api_versions,
            schema_version=status_payload.schema_version,
            features=list(status_payload.capabilities),
            tray_required=False,
        )

    @api.get("/v1/openapi.json")
    def openapi():
        return jsonify(build_openapi())

    @api.get("/v1/components")
    def components():
        return jsonify(items=application.list_components())

    @api.get("/v1/doctor")
    def doctor():
        return jsonify(
            application.doctor(supervisor=supervisor).model_dump(mode="json")
        )

    @api.get("/v1/diagnostics/bundle")
    def diagnostics_bundle():
        bundle = build_diagnostic_bundle(application, supervisor)
        return send_file(
            BytesIO(bundle.payload),
            mimetype="application/zip",
            as_attachment=True,
            download_name=bundle.filename,
            max_age=0,
        )

    @api.get("/v1/legacy")
    def legacy():
        report = application.legacy_report()
        return jsonify(
            available=report is not None,
            report=(
                report.model_dump(mode="json")
                if report is not None
                else None
            ),
        )

    @api.post("/v1/legacy/import")
    @idempotent
    def legacy_import():
        payload = LegacyImportRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        return jsonify(
            application.import_legacy(
                source_digest=payload.source_digest,
                confirmed=payload.confirmed,
            )
        )

    @api.get("/v1/components/<component_id>")
    def component(component_id: str):
        item = next(
            (
                value
                for value in application.list_components()
                if value["definition"]["id"] == component_id
            ),
            None,
        )
        if item is None:
            raise NotFoundError(
                "Component was not found.",
                {"component_id": component_id},
            )
        return jsonify(item)

    @api.get("/v1/services")
    def services():
        return jsonify(
            items=[
                service.model_dump(mode="json")
                for service in supervisor.snapshot()
            ]
        )

    @api.get("/v1/services/<service_id>")
    def service(service_id: str):
        item = next(
            (
                value
                for value in supervisor.snapshot()
                if value.id == service_id
            ),
            None,
        )
        if item is None:
            raise NotFoundError(
                "Managed service was not found.",
                {"service_id": service_id},
            )
        return jsonify(item.model_dump(mode="json"))

    def application_snapshot() -> dict:
        component = next(
            (
                item
                for item in application.list_components()
                if item["definition"]["id"] == "pandrator"
            ),
            None,
        )
        services = {
            service.id: service
            for service in supervisor.snapshot()
            if service.id
            in {
                PANDRATOR_API_SERVICE,
                PANDRATOR_MCP_SERVICE,
                PANDRATOR_WORKER_SERVICE,
            }
        }
        api_service = services.get(PANDRATOR_API_SERVICE)
        mcp_service = services.get(PANDRATOR_MCP_SERVICE)
        worker_service = services.get(PANDRATOR_WORKER_SERVICE)
        state = (
            str(component["inspection"]["state"])
            if component is not None
            else "absent"
        )
        installed = state in {"present", "degraded"}
        api_running = bool(api_service and api_service.process)
        worker_running = bool(worker_service and worker_service.process)
        mcp_running = bool(mcp_service and mcp_service.process)
        mcp_healthy = bool(
            mcp_running
            and mcp_service
            and mcp_service.health
            and mcp_service.health.state.value == "healthy"
        )
        healthy = bool(
            api_running
            and worker_running
            and api_service
            and api_service.health
            and api_service.health.state.value == "healthy"
        )
        return {
            "installed": installed,
            "component_state": state,
            "running": api_running and worker_running,
            "healthy": healthy,
            "endpoint": api_service.endpoint if api_service else None,
            "mcp_available": mcp_service is not None,
            "mcp_running": mcp_running,
            "mcp_healthy": mcp_healthy,
            "mcp_endpoint": (
                f"{str(mcp_service.endpoint).rstrip('/')}/mcp"
                if mcp_service and mcp_service.endpoint
                else None
            ),
            "browser_url": application_exposure_state["value"].browser_base_url,
            "services": [
                service.model_dump(mode="json")
                for service in (api_service, mcp_service, worker_service)
                if service is not None
            ],
        }

    def emit_application_event(
        event_type: str,
        action: str,
        *,
        error: str | None = None,
    ) -> None:
        payload = {"action": action}
        if error:
            payload["error"] = str(error)
        application.context.event_sink.emit(
            event_type,
            payload,
            component_id="pandrator",
        )

    def ensure_application_installed() -> None:
        current = application_snapshot()
        if not current["installed"]:
            raise ManagerError(
                "application_not_installed",
                "Install Pandrator before starting it.",
                http_status=409,
            )
        if current["component_state"] == "degraded":
            raise ManagerError(
                "application_repair_required",
                "Pandrator is incomplete. Review a repair before starting it.",
                http_status=409,
            )

    def lifecycle_guard(action: str):
        """Reject direct runtime mutations while durable maintenance runs."""

        def decorate(handler):
            @wraps(handler)
            def wrapped(*args, **kwargs):
                if not application.lifecycle_lock.acquire(blocking=False):
                    raise ManagerError(
                        "maintenance_in_progress",
                        "Manager maintenance is active; retry the runtime action "
                        "after it completes.",
                        {"action": action},
                        409,
                    )
                try:
                    active = application.store.list_operations(
                        active_only=True,
                        limit=1,
                    )
                    if active:
                        operation = active[0]
                        raise ManagerError(
                            "maintenance_in_progress",
                            "Manager maintenance is active; retry the runtime "
                            "action after it completes.",
                            {
                                "action": action,
                                "active_operation_id": operation.id,
                                "active_operation_state": operation.state.value,
                            },
                            409,
                        )
                    return handler(*args, **kwargs)
                finally:
                    application.lifecycle_lock.release()

            return wrapped

        return decorate

    def refresh_application_specs() -> None:
        application_service_ids = {
            PANDRATOR_API_SERVICE,
            PANDRATOR_MCP_SERVICE,
            PANDRATOR_WORKER_SERVICE,
        }
        active = {
            service.id
            for service in supervisor.snapshot()
            if service.process is not None
            and service.id in application_service_ids
        }
        if active.intersection(
            {PANDRATOR_API_SERVICE, PANDRATOR_WORKER_SERVICE}
        ):
            return
        if PANDRATOR_MCP_SERVICE in active:
            supervisor.stop(PANDRATOR_MCP_SERVICE)
        specifications = pandrator_runtime_specs(
            application.context.layout,
            exposure=application_exposure_state["value"],
            preferences={
                **application.pandrator_runtime_environment(),
                **(application_environment or {}),
            },
        )
        missing = [
            specification.executable
            for specification in specifications
            if not Path(specification.executable).is_file()
        ]
        if missing:
            raise ManagerError(
                "application_runtime_missing",
                "Pandrator's private runtime is missing. Review a Pandrator "
                "repair to restore it.",
                {"missing": missing},
                409,
            )
        selected_ids = {specification.service_id for specification in specifications}
        for service_id in application_service_ids - selected_ids:
            if supervisor.spec(service_id) is not None:
                supervisor.unregister(service_id)
        for specification in specifications:
            supervisor.replace_spec(specification)

    def start_mcp_if_available() -> str | None:
        if supervisor.spec(PANDRATOR_MCP_SERVICE) is None:
            return None
        try:
            supervisor.start(PANDRATOR_MCP_SERVICE)
        except Exception as error:
            logging.exception("Pandrator MCP could not be started")
            emit_application_event(
                "application.mcp_start_failed",
                "start",
                error=str(error),
            )
            return str(error) or "Pandrator MCP could not be started."
        return None

    @lifecycle_guard("start")
    def start_application() -> dict:
        emit_application_event("application.action_requested", "start")
        try:
            ensure_application_installed()
            refresh_application_specs()
            supervisor.start(PANDRATOR_WORKER_SERVICE)
        except Exception as error:
            emit_application_event(
                "application.action_failed",
                "start",
                error=str(error),
            )
            if isinstance(error, ManagerError):
                raise
            raise ManagerError(
                "application_start_failed",
                str(error) or "Pandrator could not be started.",
                http_status=409,
            ) from error
        mcp_error = start_mcp_if_available()
        emit_application_event("application.started", "start", error=mcp_error)
        if application_environment is not None:
            application_environment.pop("PANDRATOR_OWNER_PASSWORD", None)
        return application_snapshot()

    @lifecycle_guard("stop")
    def stop_application() -> dict:
        emit_application_event("application.action_requested", "stop")
        try:
            failures: list[str] = []
            for service_id in (
                PANDRATOR_MCP_SERVICE,
                PANDRATOR_WORKER_SERVICE,
                PANDRATOR_API_SERVICE,
            ):
                if supervisor.spec(service_id) is None:
                    continue
                try:
                    supervisor.stop(service_id)
                except Exception as error:
                    failures.append(f"{service_id}: {error}")
            if failures:
                raise RuntimeError("; ".join(failures))
        except Exception as error:
            emit_application_event(
                "application.action_failed",
                "stop",
                error=str(error),
            )
            raise ManagerError(
                "application_stop_failed",
                str(error) or "Pandrator could not be stopped.",
                http_status=409,
            ) from error
        emit_application_event("application.stopped", "stop")
        return application_snapshot()

    @lifecycle_guard("launch")
    def launch_url() -> str:
        current = application_snapshot()
        if not current["running"] or not current["healthy"]:
            current = start_application()
        endpoint = str(current.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ManagerError(
                "application_endpoint_missing",
                "Pandrator started without a browser endpoint.",
                http_status=409,
            )
        with requests.Session() as session:
            session.trust_env = False
            try:
                response = session.post(
                    f"{endpoint}/api/v1/auth/manager-browser-bootstrap",
                    headers={"Authorization": f"Bearer {client_secret}"},
                    timeout=(3, 10),
                )
                if response.status_code in {404, 405}:
                    # Pandrator versions before 0.7.0 used the scoped
                    # automation handoff for browser launches as well.
                    response = session.post(
                        f"{endpoint}/api/v1/auth/manager-bootstrap",
                        headers={
                            "Authorization": f"Bearer {client_secret}"
                        },
                        timeout=(3, 10),
                    )
            except requests.RequestException as error:
                emit_application_event(
                    "application.action_failed",
                    "open",
                    error=str(error),
                )
                raise ManagerError(
                    "application_launch_failed",
                    "Pandrator is running, but the browser sign-in handoff failed.",
                    http_status=409,
                ) from error
        try:
            selected_url = _browser_handoff_url(
                response,
                application_exposure_state["value"].browser_base_url,
            )
        except ManagerError as error:
            emit_application_event(
                "application.action_failed",
                "open",
                error=error.message,
            )
            raise
        emit_application_event("application.launch_ready", "open")
        return selected_url

    @api.get("/v1/application")
    def application_status():
        return jsonify(application_snapshot())

    @api.post("/v1/application/start")
    @idempotent
    def application_start():
        return jsonify(start_application())

    @api.post("/v1/application/stop")
    @idempotent
    def application_stop():
        return jsonify(stop_application())

    @api.post("/v1/application/restart")
    @idempotent
    @lifecycle_guard("restart")
    def application_restart():
        emit_application_event("application.action_requested", "restart")
        try:
            ensure_application_installed()
            failures: list[str] = []
            for service_id in (
                PANDRATOR_MCP_SERVICE,
                PANDRATOR_WORKER_SERVICE,
                PANDRATOR_API_SERVICE,
            ):
                if supervisor.spec(service_id) is not None:
                    try:
                        supervisor.stop(service_id)
                    except Exception as error:
                        failures.append(f"{service_id}: {error}")
            if failures:
                raise RuntimeError("; ".join(failures))
            refresh_application_specs()
            supervisor.start(PANDRATOR_WORKER_SERVICE)
        except Exception as error:
            emit_application_event(
                "application.action_failed",
                "restart",
                error=str(error),
            )
            if isinstance(error, ManagerError):
                raise
            raise ManagerError(
                "application_restart_failed",
                str(error) or "Pandrator could not be restarted.",
                http_status=409,
            ) from error
        mcp_error = start_mcp_if_available()
        emit_application_event("application.restarted", "restart", error=mcp_error)
        return jsonify(application_snapshot())

    @api.post("/v1/application/launch")
    @idempotent
    def application_launch():
        selected_url = launch_url()
        return jsonify(
            application=application_snapshot(),
            launch_url=selected_url,
        )

    def owner_authentication_initialized() -> bool:
        database = application.context.layout.data / "pandrator.sqlite3"
        if not database.is_file():
            return False
        try:
            with sqlite3.connect(
                f"{database.resolve(strict=True).as_uri()}?mode=ro",
                uri=True,
                timeout=2,
            ) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='owner_account'"
                ).fetchone()
                if table is None:
                    return False
                row = connection.execute(
                    "SELECT COUNT(*) FROM owner_account"
                ).fetchone()
                return bool(row and int(row[0]) > 0)
        except (OSError, sqlite3.Error, ValueError):
            return False

    def network_snapshot() -> dict:
        manager_payload = selected_manager_exposure.model_dump(mode="json")
        application_payload = application_exposure_state["value"].model_dump(
            mode="json"
        )
        return {
            "manager": {
                **manager_payload,
                "browser_url": (
                    selected_manager_exposure.browser_base_url
                    if selected_manager_exposure.remote_enabled
                    else request.host_url.rstrip("/")
                ),
                "remote_enabled": selected_manager_exposure.remote_enabled,
            },
            "application": {
                **application_payload,
                "browser_url": application_exposure_state[
                    "value"
                ].browser_base_url,
                "private_network_candidates": private_network_candidates(
                    application_exposure_state["value"].port or 8097
                ),
                "remote_enabled": application_exposure_state[
                    "value"
                ].remote_enabled,
                "owner_authentication_initialized": (
                    owner_authentication_initialized()
                ),
            },
        }

    def initialize_owner_password(password: str, *, replace: bool) -> None:
        specifications = pandrator_runtime_specs(
            application.context.layout,
            exposure=application_exposure_state["value"],
            preferences=application.pandrator_runtime_environment(),
        )
        api_spec = next(
            specification
            for specification in specifications
            if specification.service_id == "pandrator.api"
        )
        try:
            command_index = api_spec.arguments.index("serve")
        except ValueError as error:
            raise ManagerError(
                "application_runtime_invalid",
                "Pandrator's private runtime does not expose its authentication "
                "command.",
                http_status=409,
            ) from error
        arguments = (
            *api_spec.arguments[:command_index],
            "auth",
            "init",
            *(("--replace",) if replace else ()),
        )
        try:
            CommandRunner(
                base_environment=application.context.environment
            ).run(
                CommandSpec(
                    argv=(api_spec.executable, *arguments),
                    cwd=Path(api_spec.cwd) if api_spec.cwd else None,
                    env={"PANDRATOR_OWNER_PASSWORD": password},
                    timeout_seconds=5 * 60,
                    label="initialize Pandrator owner authentication",
                )
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ManagerError(
                "owner_authentication_failed",
                "Pandrator owner authentication could not be initialized.",
                http_status=409,
            ) from error

    @api.get("/v1/network")
    def network_status():
        return jsonify(network_snapshot())

    @api.put("/v1/network/application")
    @idempotent
    def network_application_update():
        payload = ApplicationNetworkRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        exposure = payload.exposure
        if exposure.port == 0:
            raise ManagerError(
                "application_port_required",
                "Pandrator requires a fixed application port.",
                http_status=422,
            )
        application_installed = bool(application_snapshot()["installed"])
        if exposure.remote_enabled and not application_installed:
            raise ManagerError(
                "application_not_installed",
                "Install Pandrator before enabling access from other devices.",
                http_status=409,
            )
        password = (
            payload.owner_password.get_secret_value()
            if payload.owner_password is not None
            else ""
        )
        initialized = owner_authentication_initialized()
        if exposure.remote_enabled and not initialized and not password:
            raise ManagerError(
                "owner_password_required",
                "Choose an owner password before exposing Pandrator to other "
                "devices.",
                http_status=422,
            )
        if (
            password
            and not request.is_secure
            and not _is_loopback(request.remote_addr)
        ):
            raise ManagerError(
                "secure_transport_required",
                "Set the owner password locally or through HTTPS; it must not "
                "be sent over plain remote HTTP.",
                http_status=409,
            )
        was_running = bool(application_snapshot()["running"])
        if was_running and not payload.restart_if_running:
            raise ManagerError(
                "application_stop_required",
                "Stop Pandrator before changing its network access.",
                http_status=409,
            )
        if was_running:
            stop_application()
        previous = application_exposure_state["value"]
        application_exposure_state["value"] = exposure
        try:
            if password:
                initialize_owner_password(
                    password,
                    replace=payload.replace_owner_password or initialized,
                )
            save_network_configuration(
                application.context.layout,
                NetworkConfiguration(
                    manager=selected_manager_exposure,
                    application=exposure,
                ),
            )
            if application_installed:
                refresh_application_specs()
                if was_running:
                    start_application()
        except Exception:
            application_exposure_state["value"] = previous
            try:
                save_network_configuration(
                    application.context.layout,
                    NetworkConfiguration(
                        manager=selected_manager_exposure,
                        application=previous,
                    ),
                )
            except Exception:
                logging.exception(
                    "Could not restore the previous network configuration"
                )
            if application_installed:
                try:
                    refresh_application_specs()
                except Exception:
                    logging.exception(
                        "Could not restore the previous Pandrator network spec"
                    )
            raise
        application.context.event_sink.emit(
            "application.network_updated",
            {
                "mode": exposure.mode.value,
                "browser_url": exposure.browser_base_url,
            },
            component_id="pandrator",
        )
        return jsonify(network_snapshot())

    @api.post("/v1/plans")
    @idempotent
    def plans():
        payload = PlanRequest.model_validate(request.get_json(silent=False) or {})
        plan = application.plan(
            kind=payload.kind,
            desired=payload.desired,
            expected_revision=payload.expected_revision,
        )
        return jsonify(plan.model_dump(mode="json")), 201

    @api.route("/v1/releases", methods=["GET"])
    def releases():
        current = {}
        for product in ("pandrator", "pandrator-manager"):
            selected = application.store.accepted_release(product)
            if selected is not None:
                current[product] = {
                    key: value
                    for key, value in selected.items()
                    if key != "envelope"
                }
        return jsonify(
            items=application.store.release_slots(),
            current=current,
        )

    @api.get("/v1/releases/manager-update")
    def manager_update():
        return jsonify(application.manager_update())

    @api.post("/v1/releases/plans")
    @idempotent
    def release_plans():
        payload = ReleasePlanRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        plan = application.release_plan(
            payload.manifest,
            expected_revision=payload.expected_revision,
            offline=payload.offline,
            start_after_activation=payload.start_after_activation,
        )
        return jsonify(plan.model_dump(mode="json")), 201

    @api.post("/v1/uninstall/plans")
    @idempotent
    def uninstall_plans():
        payload = UninstallPlanRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        plan = application.uninstall_plan(
            expected_revision=payload.expected_revision,
            purge_data=payload.purge_data,
            export_data=payload.export_data,
        )
        return jsonify(plan.model_dump(mode="json")), 201

    @api.route("/v1/operations", methods=["GET", "POST"])
    @idempotent
    def operations():
        if request.method == "GET":
            return jsonify(
                items=[
                    operation.model_dump(mode="json")
                    for operation in application.store.list_operations()
                ]
            )
        payload = OperationRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        idempotency_key = request.headers["Idempotency-Key"].strip()
        operation, created = application.submit_operation(
            plan_id=payload.plan_id,
            plan_digest=payload.plan_digest,
            accepted_confirmations=payload.accepted_confirmations,
            idempotency_key=idempotency_key,
        )
        return (
            jsonify(operation.model_dump(mode="json")),
            202 if created else 200,
        )

    @api.get("/v1/operations/<operation_id>")
    def operation(operation_id: str):
        return jsonify(
            application.store.get_operation(operation_id).model_dump(mode="json")
        )

    @api.get("/v1/operations/<operation_id>/tasks")
    def operation_tasks(operation_id: str):
        # Resolve the operation first so an unknown ID has the same typed 404
        # behavior as the record endpoint.
        application.store.get_operation(operation_id)
        return jsonify(
            items=[
                task.model_dump(mode="json")
                for task in application.store.operation_tasks(operation_id)
            ]
        )

    @api.post("/v1/operations/<operation_id>/cancel")
    @idempotent
    def cancel_operation(operation_id: str):
        application.store.request_cancellation(operation_id)
        application.context.event_sink.emit(
            "operation.cancel_requested",
            {"operation_id": operation_id},
            operation_id=operation_id,
        )
        return jsonify(status="cancellation_requested", operation_id=operation_id)

    @api.get("/v1/events")
    def events():
        after = request.args.get("after")
        if after is None:
            after = request.headers.get("Last-Event-ID", "0")
        try:
            cursor = max(0, int(after))
        except ValueError:
            return jsonify(error={"code": "invalid_cursor"}), 400
        first, _last = application.store.event_bounds()
        if first is not None and cursor and cursor < first - 1:
            return (
                jsonify(
                    error={
                        "code": "event_cursor_expired",
                        "snapshot_required": True,
                        "first_available_cursor": first,
                    }
                ),
                409,
            )

        @stream_with_context
        def stream():
            current = cursor
            heartbeat = time.monotonic()
            while True:
                rows = application.store.events_after(current, limit=100)
                if rows:
                    for event in rows:
                        current = event.cursor
                        yield (
                            f"id: {event.cursor}\n"
                            f"event: {event.event_type}\n"
                            f"data: {event.model_dump_json()}\n\n"
                        )
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                time.sleep(0.5)

        return Response(
            stream(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @api.get("/v1/activity")
    def activity():
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 200))
        except ValueError:
            return jsonify(error={"code": "invalid_activity_limit"}), 400
        return jsonify(
            items=[
                event.model_dump(mode="json")
                for event in application.store.recent_events(limit=limit)
            ]
        )

    @api.get("/v1/logs")
    def logs():
        service_id = request.args.get("service_id", "")
        known = {service.id for service in supervisor.snapshot()}
        if service_id not in known:
            raise NotFoundError(
                "Managed service was not found.",
                {"service_id": service_id},
            )
        try:
            maximum = max(
                1024,
                min(int(request.args.get("bytes", "65536")), 1024 * 1024),
            )
        except ValueError:
            return jsonify(error={"code": "invalid_log_limit"}), 400
        path = (
            application.context.layout.logs
            / "services"
            / f"{service_id}.log"
        )
        return jsonify(service_id=service_id, tail=_tail(path, maximum))

    @lifecycle_guard("runtime")
    def runtime_action(action: str):
        payload = RuntimeRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        selected = payload.service_ids or tuple(
            service.id for service in supervisor.snapshot()
        )
        if (
            action in {"start", "restart"}
            and {
                PANDRATOR_API_SERVICE,
                PANDRATOR_MCP_SERVICE,
                PANDRATOR_WORKER_SERVICE,
            }.intersection(selected)
        ):
            # The daemon registers a conservative fallback before a first
            # Pandrator installation exists. Recompose that launch contract
            # after activation so the generic runtime API, CLI, and MCP paths
            # use the active slot's locked Pixi environment just like the
            # guided application-start endpoint does.
            ensure_application_installed()
            refresh_application_specs()
        results = []
        for service_id in selected:
            method = getattr(supervisor, action)
            application.context.event_sink.emit(
                "runtime.action_requested",
                {"action": action, "service_id": service_id},
                service_id=service_id,
            )
            try:
                service = method(service_id)
            except KeyError:
                application.context.event_sink.emit(
                    "runtime.action_failed",
                    {
                        "action": action,
                        "service_id": service_id,
                        "error": "Managed service was not found.",
                    },
                    service_id=service_id,
                )
                raise NotFoundError(
                    "Managed service was not found.",
                    {"service_id": service_id},
                ) from None
            except RuntimeError as error:
                application.context.event_sink.emit(
                    "runtime.action_failed",
                    {
                        "action": action,
                        "service_id": service_id,
                        "error": str(error),
                    },
                    service_id=service_id,
                )
                raise ManagerError(
                    "runtime_action_failed",
                    str(error) or "The managed service action failed.",
                    {"service_id": service_id, "action": action},
                    409,
                ) from error
            results.append(service.model_dump(mode="json"))
            application.context.event_sink.emit(
                "runtime.action_completed",
                {"action": action, "service_id": service_id},
                component_id=service.component_id,
                service_id=service_id,
            )
        return jsonify(items=results)

    @api.post("/v1/runtime/start")
    @idempotent
    def runtime_start():
        return runtime_action("start")

    @api.post("/v1/runtime/stop")
    @idempotent
    def runtime_stop():
        return runtime_action("stop")

    @api.post("/v1/runtime/restart")
    @idempotent
    def runtime_restart():
        return runtime_action("restart")

    @api.post("/v1/runtime/stop-manager")
    @idempotent
    def stop_manager():
        if shutdown_callback is None:
            return jsonify(error={"code": "unsupported"}), 501
        shutdown_callback()
        return jsonify(status="manager_stopping")

    @api.post("/v1/recovery-sessions")
    @idempotent
    def recovery_session():
        token = sessions.mint_launch_token()
        base_url = (
            selected_manager_exposure.browser_base_url
            if selected_manager_exposure.remote_enabled
            else request.host_url.rstrip("/")
        )
        return jsonify(
            url=f"{base_url}/recovery#token={token}",
            expires_in=sessions.token_ttl_seconds,
        )

    @api.post("/v1/recovery/exchange")
    def recovery_exchange():
        payload = RecoveryExchangeRequest.model_validate(
            request.get_json(silent=False) or {}
        )
        previous_session = sessions.authenticate(
            request.cookies.get(RECOVERY_COOKIE)
        )
        session = sessions.exchange(
            payload.token,
            remember=payload.remember,
            user_agent=request.headers.get("User-Agent", ""),
        )
        if session is None:
            return jsonify(error={"code": "invalid_or_expired_token"}), 401
        if previous_session is not None:
            sessions.revoke(previous_session.session_id)
        response = make_response(jsonify(session_payload(session)))
        set_session_cookie(response, session)
        return response

    @api.get("/v1/session")
    def current_browser_session():
        selected = getattr(g, "recovery_session", None)
        if selected is None:
            return jsonify(error={"code": "browser_session_required"}), 401
        return jsonify(session_payload(selected))

    @api.delete("/v1/session")
    def sign_out_browser_session():
        selected = getattr(g, "recovery_session", None)
        if selected is None:
            return jsonify(error={"code": "browser_session_required"}), 401
        revoked = sessions.revoke(selected.session_id)
        response = make_response(jsonify(revoked=revoked))
        clear_session_cookie(response)
        return response

    @api.get("/v1/browser-sessions")
    def list_browser_sessions():
        selected = getattr(g, "recovery_session", None)
        if selected is None:
            return jsonify(error={"code": "browser_session_required"}), 401
        return jsonify(items=sessions.sessions(selected.session_id))

    @api.delete("/v1/browser-sessions")
    def forget_browser_sessions():
        selected = getattr(g, "recovery_session", None)
        if selected is None:
            return jsonify(error={"code": "browser_session_required"}), 401
        revoked = sessions.revoke_all()
        response = make_response(jsonify(revoked=revoked))
        clear_session_cookie(response)
        return response

    @api.get("/recovery")
    @api.get("/recovery/")
    def recovery_index():
        return send_from_directory(recovery_static, "index.html")

    @api.get("/recovery/<path:asset>")
    def recovery_asset(asset: str):
        return send_from_directory(recovery_static, asset)

    return api
