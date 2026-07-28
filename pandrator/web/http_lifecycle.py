"""Cross-cutting HTTP lifecycle, authentication, and error handling."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import traceback
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from flask import Flask, g, jsonify, request, session
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from .application_services import ApplicationServices
from .credentials import contains_inline_secret


ViewFunction = TypeVar("ViewFunction", bound=Callable[..., Any])


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
        jobs = self.services.jobs
        return jsonify(
            {
                "error": {
                    "code": code,
                    "message": jobs.secret_redactor.redact(message),
                    "details": jobs.redact_diagnostic(details),
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

    def bearer_authenticated(self) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        return self.services.auth.verify_api_token(header.split(" ", 1)[1].strip())

    def authenticated(self) -> bool:
        return bool(session.get("authenticated")) or self.bearer_authenticated()

    def require_auth(self, function: ViewFunction) -> ViewFunction:
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not self.authenticated():
                return self.error_response(
                    "authentication_required",
                    "Authentication is required.",
                    401,
                )
            return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def register(self) -> None:
        """Install lifecycle hooks and error handlers on the Flask app."""

        app = self.app
        paths = self.services.paths

        @app.before_request
        def _request_context():
            g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
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
            jobs = self.services.jobs
            safe_message = jobs.secret_redactor.redact(error)
            safe_trace = jobs.secret_redactor.redact(traceback.format_exc())
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
