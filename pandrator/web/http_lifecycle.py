"""Cross-cutting HTTP lifecycle, authentication, and error handling."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
import time
import traceback
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from flask import Flask, g, jsonify, request, session
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from .application_services import ApplicationServices
from .auth import ALL_SCOPES, Principal, normalize_scopes
from .credentials import contains_inline_secret

ViewFunction = TypeVar("ViewFunction", bound=Callable[..., Any])
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_TRACEPARENT = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
)


def load_or_create_flask_secret(paths) -> str:
    """Load the per-installation Flask secret or create it with tight permissions."""

    target = paths.root / ".flask-secret"
    try:
        secret = target.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    except OSError:
        pass
    secret = secrets.token_hex(32)
    target.write_text(secret, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return secret


def frontend_script_policy(static_dir: Path) -> str:
    """Build CSP hashes for the small inline bootstrap scripts in the SPA shell."""

    index = static_dir / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        return "'self'"
    hashes = []
    for script in re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        digest = base64.b64encode(
            hashlib.sha256(script.encode("utf-8")).digest()
        ).decode("ascii")
        hashes.append(f"'sha256-{digest}'")
    return " ".join(["'self'", *hashes])


def _endpoint_name() -> str:
    """Return the view name without its Blueprint namespace."""

    return str(request.endpoint or "").rsplit(".", 1)[-1]


class ApiGuards:
    """Request guards shared by domain Blueprints."""

    def __init__(
        self,
        app: Flask,
        services: ApplicationServices,
        *,
        testing: bool,
        script_policy: str,
    ):
        self.app = app
        self.services = services
        self.testing = testing
        self.script_policy = script_policy

    def error_response(
        self,
        code: str,
        message: str,
        status: int,
        details: Any = None,
    ):
        redactor = self.services.redactor
        return jsonify(
            {
                "error": {
                    "code": code,
                    "message": redactor.redact(message),
                    "details": redactor.redact_value(details),
                    "request_id": getattr(g, "request_id", ""),
                }
            }
        ), status

    def inline_credential_error(self, value: Any):
        if contains_inline_secret(value):
            return self.error_response(
                "validation_error",
                "API keys and other credentials must be saved in provider settings.",
                422,
            )
        return None

    @staticmethod
    def _network_zone() -> str:
        try:
            address = ipaddress.ip_address(str(request.remote_addr or ""))
        except ValueError:
            return "public"
        if address.is_loopback:
            return "loopback"
        if address.is_private:
            return "private"
        return "public"

    def principal(self) -> Principal | None:
        if getattr(g, "principal_resolved", False):
            return getattr(g, "principal", None)
        g.principal_resolved = True
        header = request.headers.get("Authorization", "")
        zone = self._network_zone()
        if header:
            if not header.lower().startswith("bearer "):
                g.principal = None
                return None
            identity = self.services.identity.snapshot(
                observed_origin=request.url_root
            )
            g.principal = self.services.auth.resolve_api_token(
                header.split(" ", 1)[1].strip(),
                network_zone=zone,
                target_instance_id=identity.instance_id,
                canonical_origin=identity.canonical_origin,
            )
            return g.principal
        if not session.get("authenticated"):
            g.principal = None
            return None
        raw_kind = str(session.get("principal_kind") or "owner_session")
        kind = (
            raw_kind
            if raw_kind
            in {
                "owner_session",
                "api_token",
                "manager_bootstrap",
                "automation_client",
                "service",
            }
            else "owner_session"
        )
        raw_scopes = session.get("principal_scopes")
        try:
            scopes = (
                normalize_scopes(raw_scopes)
                if raw_scopes is not None
                else ALL_SCOPES
            )
        except ValueError:
            g.principal = None
            return None
        g.principal = Principal(
            subject=str(session.get("principal_subject") or "owner")[:200],
            kind=kind,
            scopes=scopes,
            token_id=None,
            network_zone=zone,
            target_instance_id=self.services.identity.instance_id,
            client_id=(
                str(session.get("principal_client_id"))
                if session.get("principal_client_id")
                else None
            ),
        )
        return g.principal

    def bearer_authenticated(self) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        principal = self.principal()
        return principal is not None and principal.token_id is not None

    def authenticated(self) -> bool:
        return self.principal() is not None

    @staticmethod
    def required_scope() -> str:
        method = request.method.upper()
        path = request.path
        endpoint = _endpoint_name()
        if path.startswith("/api/v1/manager/"):
            if method in {"GET", "HEAD"} or path.endswith("/plans"):
                return "manager.read"
            if "/runtime/" in path:
                return "manager.runtime"
            return "manager.mutate"
        if (
            path.startswith("/api/v1/auth/tokens")
            or path.startswith("/api/v1/auth/automation-clients")
            or path.startswith("/api/v1/audit/")
        ):
            return "app.admin"
        if path.startswith("/api/v1/credentials"):
            return (
                "app.credentials.read"
                if method in {"GET", "HEAD"}
                else "app.credentials.write"
            )
        if path.startswith("/api/v1/providers") and method not in {
            "GET",
            "HEAD",
        }:
            return "app.credentials.write"
        if method in {"GET", "HEAD", "OPTIONS"}:
            return "app.read"
        if (
            method == "POST"
            and path.endswith("/workflow-plans")
        ):
            # Planning persists an immutable preview, but does not change
            # session state or start work.
            return "app.read"
        if (
            method == "POST"
            and "/workflow-plans/" in path
            and path.endswith("/execute")
        ):
            return "app.run"
        if (
            path.endswith("/cancel")
            or endpoint in {"job_cancel", "work_cancel", "training_cancel"}
        ):
            return "app.cancel"
        if path == "/api/v1/jobs":
            return "app.admin"
        run_markers = (
            "/run",
            "/generation-runs",
            "/agent-runs",
            "/preview",
            "/transcribe",
            "/training",
            "/rvc/convert",
            "/sources/url",
            "/pdf/apply",
            "/bundle",
            "/session-bundles/import",
            "/models/refresh",
            "/providers/",
        )
        if any(marker in path for marker in run_markers):
            return "app.run"
        return "app.write"

    def require_scope(self, *required_scopes: str):
        required = frozenset(required_scopes)

        def decorator(function: ViewFunction) -> ViewFunction:
            @wraps(function)
            def wrapped(*args, **kwargs):
                principal = self.principal()
                if principal is None:
                    return self.error_response(
                        "authentication_required",
                        "Authentication is required.",
                        401,
                    )
                missing = sorted(
                    scope
                    for scope in required
                    if not principal.has_scope(scope)
                )
                if missing:
                    return self.error_response(
                        "scope_denied",
                        "The authenticated principal lacks the required scope.",
                        403,
                        {"required_scopes": missing},
                    )
                return function(*args, **kwargs)

            return wrapped  # type: ignore[return-value]

        return decorator

    def require_auth(self, function: ViewFunction) -> ViewFunction:
        @wraps(function)
        def wrapped(*args, **kwargs):
            principal = self.principal()
            if principal is None:
                return self.error_response(
                    "authentication_required",
                    "Authentication is required.",
                    401,
                )
            scope = self.required_scope()
            if not principal.has_scope(scope):
                return self.error_response(
                    "scope_denied",
                    "The authenticated principal lacks the required scope.",
                    403,
                    {"required_scopes": [scope]},
                )
            return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def register(self) -> None:
        """Install lifecycle hooks and error handlers on the Flask app."""

        app = self.app
        paths = self.services.paths

        @app.before_request
        def _request_context():
            supplied_request_id = str(
                request.headers.get("X-Request-ID") or ""
            ).strip()
            g.request_id = (
                supplied_request_id
                if _REQUEST_ID.fullmatch(supplied_request_id)
                else str(uuid.uuid4())
            )
            supplied_traceparent = str(
                request.headers.get("traceparent") or ""
            ).strip()
            g.traceparent = (
                supplied_traceparent
                if _TRACEPARENT.fullmatch(supplied_traceparent)
                else None
            )
            g.request_started = time.perf_counter()
            g.principal_resolved = False
            g.principal = None
            endpoint = _endpoint_name()
            if (
                (paths.root / "maintenance.json").is_file()
                and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and endpoint
                not in {
                    "auth_login",
                    "auth_logout",
                    "auth_bootstrap",
                    "auth_manager_bootstrap",
                    "job_cancel",
                    "work_cancel",
                    "training_cancel",
                }
            ):
                return self.error_response(
                    "maintenance",
                    "Pandrator is draining work for an update. Try again after it restarts.",
                    503,
                )
            if (
                request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and endpoint
                not in {
                    "auth_login",
                    "auth_bootstrap",
                    "auth_manager_bootstrap",
                    "auth_automation_authorize",
                    "auth_automation_token",
                    "health",
                }
            ):
                if self.bearer_authenticated():
                    return None
                if session.get("authenticated"):
                    supplied = request.headers.get("X-CSRF-Token", "")
                    expected = str(session.get("csrf_token") or "")
                    if not expected or not secrets.compare_digest(supplied, expected):
                        return self.error_response(
                            "csrf_failed",
                            "The CSRF token is missing or invalid.",
                            403,
                        )
            return None

        @app.after_request
        def _security_headers(response):
            response.headers["X-Request-ID"] = getattr(g, "request_id", "")
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers[
                "Permissions-Policy"
            ] = "camera=(), geolocation=(), microphone=(self)"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'self'; "
                "img-src 'self' data: blob:; media-src 'self' blob:; "
                "connect-src 'self'; style-src 'self' 'unsafe-inline'; "
                f"script-src {self.script_policy}"
            )
            if request.path.startswith("/api/"):
                response.headers["Cache-Control"] = "private, no-store"
            principal = self.principal()
            if principal is not None:
                try:
                    duration_ms = int(
                        (
                            time.perf_counter()
                            - float(getattr(g, "request_started", 0.0))
                        )
                        * 1_000
                    )
                    self.services.audit.record(
                        principal=principal,
                        request_id=getattr(g, "request_id", ""),
                        traceparent=getattr(g, "traceparent", None),
                        action=_endpoint_name() or "unknown",
                        method=request.method,
                        path=request.path,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                        idempotency_key=request.headers.get(
                            "Idempotency-Key"
                        ),
                        plan_id=getattr(g, "audit_plan_id", None),
                        plan_digest=getattr(g, "audit_plan_digest", None),
                        resource_kind=getattr(
                            g,
                            "audit_resource_kind",
                            None,
                        ),
                        resource_id=getattr(
                            g,
                            "audit_resource_id",
                            None,
                        ),
                    )
                except Exception as error:
                    self.app.logger.error(
                        "Audit projection failed: %s",
                        self.services.redactor.redact(error),
                    )
            return response

        @app.errorhandler(ValidationError)
        def _validation_error(error):
            return self.error_response(
                "validation_error",
                "The request payload is invalid.",
                422,
                error.errors(include_input=False),
            )

        @app.errorhandler(HTTPException)
        def _http_error(error):
            return self.error_response(
                error.name.lower().replace(" ", "_"),
                error.description,
                error.code or 500,
            )

        @app.errorhandler(Exception)
        def _unexpected_error(error):
            if self.testing:
                raise error
            redactor = self.services.redactor
            safe_message = redactor.redact(error)
            safe_trace = redactor.redact(traceback.format_exc())
            app.logger.error(
                "Unhandled API error: %s\n%s",
                safe_message,
                safe_trace,
            )
            return self.error_response(
                "internal_error",
                "An unexpected error occurred.",
                500,
            )
