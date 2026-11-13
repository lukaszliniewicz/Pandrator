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
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from flask import Flask, g, jsonify, request, session
from pydantic import ValidationError
from werkzeug.datastructures import ImmutableMultiDict
from werkzeug.exceptions import HTTPException

from pandrator.logic.model_acquisition import ModelAcquisitionDenied

from .abandoned_requests import (
    RequestAbandoned,
    bind_disconnect_probe,
    reset_disconnect_probe,
)
from .access.artifact_policy import ArtifactNotFound
from .access.contracts import AccessContext, AccessDenied, ProjectNotFound, WorkspaceNotFound
from .access.session_policy import SessionNotFound
from .accounts.actor_authorization import ActorAuthorizationDenied
from .accounts.administration import AdministratorRequired, require_administrator_actor
from .accounts.execution_actor import UserActor, actor_from_principal, current_execution_actor
from .accounts.model_acquisition import model_acquisition_for_actor
from .application_services import ApplicationServices
from .auth import NetworkZone, Principal
from .credentials import contains_inline_secret
from .public_links.contracts import ProjectLinkContext, PublicLinkError
from .update_maintenance import update_maintenance_active

ViewFunction = TypeVar("ViewFunction", bound=Callable[..., Any])
# Flask reuses the app context (and therefore ``g``) for nested request
# contexts of the same app. Keep lifecycle resources on the request
# environment instead, where each request context owns its own slot.
_ACTOR_TOKENS_ENVIRON_KEY = "pandrator.execution_actor_tokens"
_ACQUISITION_CONTEXT_ENVIRON_KEY = "pandrator.model_acquisition_context"
_DISCONNECT_PROBE_TOKEN_ENVIRON_KEY = "pandrator.disconnect_probe_token"
_WAITRESS_DISCONNECT_ENVIRON_KEY = "waitress.client_disconnected"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_TRACEPARENT = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
)
# ``g`` is app-context local, so nested request contexts on the same app share
# it. Request-global overrides are therefore snapshotted per request and
# restored on teardown; an inner request can never expose its principal,
# request id, or audit target to the outer request.
_REQUEST_GLOBALS_ENVIRON_KEY = "pandrator.request_globals_snapshot"
_LIFECYCLE_GLOBALS = (
    "audit_plan_digest",
    "audit_plan_id",
    "audit_resource_id",
    "audit_resource_kind",
    "principal",
    "principal_resolved",
    "project_link_context",
    "request_id",
    "request_started",
    "traceparent",
)


def _snapshot_lifecycle_globals() -> dict[str, tuple[bool, Any]]:
    return {
        name: (name in g, getattr(g, name, None))
        for name in _LIFECYCLE_GLOBALS
    }


def _restore_lifecycle_globals(
    snapshot: dict[str, tuple[bool, Any]] | None,
) -> None:
    if not snapshot:
        return
    for name, (present, value) in snapshot.items():
        if present:
            setattr(g, name, value)
        else:
            g.pop(name, None)


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


def _request_log_context() -> tuple[str, str, str, str]:
    """Correlate failures without logging query strings or request bodies."""
    return (
        datetime.now(UTC).isoformat(timespec="milliseconds"),
        str(getattr(g, "request_id", "")),
        request.method,
        str(request.url_rule.rule) if request.url_rule else "unmatched",
    )


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
    def _network_zone() -> NetworkZone:
        try:
            address = ipaddress.ip_address(str(request.remote_addr or ""))
        except ValueError:
            return "public"
        if address.is_loopback:
            return "loopback"
        if address.is_private:
            return "private"
        return "public"

    def _bind_execution_actor(self, actor: UserActor | None) -> None:
        """Bind a resolved request actor and retain its token for teardown.

        Request contexts for the same app reuse the app context, so nested
        request contexts share ``g``. A stack kept on ``g`` would therefore
        lose an outer request's token or reset it out of order. The request
        environment is per request context, so each request restores its own
        bindings in reverse order, even when it nests inside another request.
        """

        tokens = request.environ.get(_ACTOR_TOKENS_ENVIRON_KEY)
        if tokens is None:
            tokens = []
            request.environ[_ACTOR_TOKENS_ENVIRON_KEY] = tokens
        tokens.append(current_execution_actor.set(actor))

    def principal(self) -> Principal | None:
        if getattr(g, "principal_resolved", False):
            return getattr(g, "principal", None)
        g.principal_resolved = True
        header = request.headers.get("Authorization", "")
        zone = self._network_zone()
        link_id = None
        if request.headers.get("X-Pandrator-Project-Link") is not None or "project_link_id" in request.args:
            from .public_link_http import selector

            try:
                link_id = selector(request)
            except PublicLinkError:
                g.principal = None
                return None
        if link_id is not None:
            # A selected guest identity never borrows the normal login cookie.
            if header:
                g.principal = None
                return None
            from .public_link_http import cookie_name, request_allowed

            links = self.app.extensions.get("pandrator_public_links")
            raw = request.cookies.get(cookie_name(link_id), "")
            if links is None or not raw:
                g.principal = None
                return None
            try:
                shared = links.session_context(link_id, raw)
            except (PublicLinkError, PermissionError):
                g.principal = None
                return None
            actor = shared.actor
            g.project_link_context = shared
            g.principal = Principal(
                subject=actor.subject, kind="project_link", scopes=actor.scopes,
                token_id=None, network_zone=zone,
                target_instance_id=self.services.identity.instance_id,
                client_id=None, user_id=actor.user_id,
                auth_revision=actor.auth_revision,
                browser_session_id=actor.browser_session_id,
            )
            self._bind_execution_actor(actor)
            try:
                if not request_allowed(request, shared, self.services.database):
                    raise AccessDenied()
            except PublicLinkError:
                raise AccessDenied() from None
            return g.principal
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
            if g.principal is not None and g.principal.kind == "project_link":
                g.principal = None
                return None
            if g.principal is not None and not self.services.auth.solo_access_allowed(
                g.principal.user_id, g.principal.auth_revision
            ):
                g.principal = None
            if g.principal is not None:
                try:
                    self._bind_execution_actor(actor_from_principal(g.principal))
                except (TypeError, ValueError):
                    g.principal = None
            return g.principal
        raw_session = session.get("auth_session")
        if not session.get("authenticated") or not isinstance(raw_session, str):
            g.principal = None
            return None
        resolved = self.services.auth.browser_sessions.resolve(raw_session)
        if resolved is not None and resolved.kind == "project_link":
            g.principal = None
            return None
        if resolved is None or not self.services.auth.solo_access_allowed(
            resolved.user_id, resolved.auth_revision
        ):
            g.principal = None
            return None
        g.principal = Principal(
            subject=resolved.subject,
            kind=resolved.kind,
            scopes=resolved.scopes,
            token_id=None,
            network_zone=zone,
            target_instance_id=self.services.identity.instance_id,
            client_id=resolved.client_id,
            user_id=resolved.user_id,
            auth_revision=resolved.auth_revision,
            browser_session_id=resolved.session_id,
        )
        try:
            self._bind_execution_actor(actor_from_principal(g.principal))
        except (TypeError, ValueError):
            g.principal = None
        return g.principal

    def project_link_context(self) -> ProjectLinkContext | None:
        """Return only the explicitly selected, validated guest credential."""
        self.principal()
        shared = getattr(g, "project_link_context", None)
        return shared if isinstance(shared, ProjectLinkContext) else None

    def security_event(self, event: str, status: int) -> None:
        sink = self.app.extensions.get("pandrator_security_events")
        if sink is not None:
            sink.emit(
                event, client_ip=request.remote_addr, status=status,
                request_id=getattr(g, "request_id", None),
                route=request.url_rule.rule if request.url_rule else "/api/v1/auth/login",
                method=request.method,
            )

    def access_context(self) -> AccessContext:
        """Pass the actual HTTP actor into a caller-owned resource-policy unit."""
        principal = self.principal()
        actor = actor_from_principal(principal) if principal is not None else None
        if not isinstance(actor, UserActor):
            raise AccessDenied()
        return AccessContext(actor)

    def bearer_authenticated(self) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        principal = self.principal()
        return principal is not None and principal.token_id is not None

    @staticmethod
    def administrative_request() -> bool:
        method = request.method.upper()
        path = request.path
        if method not in {"GET", "HEAD"} and path.startswith("/api/v1/manager/"):
            return True
        if method == "POST" and path in {
            "/api/v1/rvc/models",
            "/api/v1/services/tts/xtts/models",
            "/api/v1/training",
        }:
            return True
        if method == "POST" and path.startswith("/api/v1/training/") and path.endswith("/retry"):
            return True
        return method == "DELETE" and path.startswith(
            "/api/v1/services/tts/xtts/models/"
        )

    def administrator_error(self, *required_scopes: str):
        try:
            require_administrator_actor(
                self.services.database,
                required_scopes=frozenset(required_scopes),
            )
        except PermissionError:
            return self.error_response(
                "administrator_required",
                "An active Administrator credential with the required scopes is required.",
                403,
                {"required_scopes": sorted({"app.admin", *required_scopes})},
            )
        return None

    def authenticated(self) -> bool:
        return self.principal() is not None

    @staticmethod
    def required_scope() -> str:
        method = request.method.upper()
        path = request.path
        endpoint = _endpoint_name()
        if path.startswith("/api/v1/admin/"):
            return "app.admin"
        if path.startswith("/api/v1/manager/"):
            if method in {"GET", "HEAD"} or path.endswith("/plans"):
                return "manager.read"
            if "/runtime/" in path:
                return "manager.runtime"
            return "manager.mutate"
        if path.startswith(("/api/v1/auth/tokens", "/api/v1/auth/automation-clients")):
            return "app.credentials.read" if method in {"GET", "HEAD"} else "app.credentials.write"
        if path.startswith("/api/v1/audit/"):
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
            # Capturing execution inputs requires the same resource RUN
            # authority as the explicit planning route.
            return "app.run"
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
        if endpoint == "job_video_tail_decision":
            return "app.run"
        if method == "POST" and endpoint in {
            "artifact_audio_preview", "artifact_video_preview", "artifact_waveform",
            "output_mix_preview",
            "create_comparison_export",
        }:
            return "app.run"
        if method == "POST" and endpoint == "agent_run_accept":
            return "app.write"
        if path == "/api/v1/sounds/generate" or path.endswith(("/sound-plan/render", "/sound-plan/resolve")):
            return "app.run"
        if path == "/api/v1/jobs":
            return "app.admin"
        run_markers = (
            "/run",
            "/generation-runs",
            "/generation-admissions",
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
                if "app.admin" in required or self.administrative_request():
                    denied = self.administrator_error(*required)
                    if denied is not None:
                        return denied
                denied = self._private_job_access(principal, kwargs)
                if denied is not None:
                    return denied
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
            if scope == "app.admin" or self.administrative_request():
                denied = self.administrator_error(scope)
                if denied is not None:
                    return denied
            denied = self._private_job_access(principal, kwargs)
            if denied is not None:
                return denied
            return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def _private_job_access(self, principal: Principal, arguments: dict):
        """Generic job/work routes must not bypass temporary-job ownership."""
        identifier = arguments.get("job_id")
        if not identifier:
            return None
        from sqlalchemy import select

        from .access.private_transcription_policy import (
            PrivateWorkAction,
            private_job_visibility_clause,
        )
        from .models import Job

        scope = self.required_scope()
        action = {
            "app.run": PrivateWorkAction.RUN,
            "app.cancel": PrivateWorkAction.CANCEL,
        }.get(scope, PrivateWorkAction.READ)
        with self.services.database.snapshot_session() as db_session:
            allowed = private_job_visibility_clause(db_session, self.access_context(), action)
            visible = db_session.scalar(
                select(Job.id).where(Job.id == identifier, allowed)
            )
            if visible is None:
                if request.path.startswith("/api/v1/jobs/"):
                    message = "Job not found."
                elif request.path.startswith("/api/v1/purge-inspections/"):
                    message = "Purge inspection not found."
                else:
                    message = "Work item not found."
                return self.error_response("not_found", message, 404)
        return None

    def register(self) -> None:
        """Install lifecycle hooks and error handlers on the Flask app."""

        app = self.app
        paths = self.services.paths

        @app.before_request
        def _request_context():
            request.environ[_REQUEST_GLOBALS_ENVIRON_KEY] = (
                _snapshot_lifecycle_globals()
            )
            self._bind_execution_actor(None)
            # Only reads are abandoned; a mutation runs to its commit, and
            # api_server keeps waitress from skipping a queued one.
            disconnect_probe = request.environ.get(_WAITRESS_DISCONNECT_ENVIRON_KEY)
            if request.method in ("GET", "HEAD") and callable(disconnect_probe):
                request.environ[_DISCONNECT_PROBE_TOKEN_ENVIRON_KEY] = bind_disconnect_probe(
                    disconnect_probe
                )
            acquisition_context = model_acquisition_for_actor(self.services.database)
            acquisition_context.__enter__()
            request.environ[_ACQUISITION_CONTEXT_ENVIRON_KEY] = acquisition_context
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
                request.headers.get("X-Pandrator-Project-Link") is not None
                or "project_link_id" in request.args
            ) and endpoint != "public_link_redeem":
                if self.principal() is None:
                    return self.error_response(
                        "authentication_required",
                        "This shared link has expired or was revoked.", 401,
                    )
                # Transport identity is consumed before business-query validation.
                # Keep the resolved context for CSRF even on query-selected writes.
                if "project_link_id" in request.args:
                    request.__dict__["args"] = ImmutableMultiDict(
                        (key, value) for key, value in request.args.items(multi=True)
                        if key != "project_link_id"
                    )
            if (
                update_maintenance_active(paths.root)
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
                    "account_password_reset_redeem",
                    "public_link_redeem",
                    "health",
                }
            ):
                shared = getattr(g, "project_link_context", None)
                if shared is not None:
                    from .public_link_http import csrf_valid

                    if not csrf_valid(shared, request.headers.get("X-CSRF-Token", "")):
                        return self.error_response("csrf_failed", "The CSRF token is missing or invalid.", 403)
                    return None
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

        @app.teardown_request
        def _restore_execution_actor(_error):
            # The actor stack is popped first so an exception from the
            # acquisition context cannot strand the request's actor bindings.
            actor_tokens = request.environ.pop(_ACTOR_TOKENS_ENVIRON_KEY, None)
            globals_before = request.environ.pop(
                _REQUEST_GLOBALS_ENVIRON_KEY, None
            )
            probe_token = request.environ.pop(_DISCONNECT_PROBE_TOKEN_ENVIRON_KEY, None)
            try:
                acquisition_context = request.environ.pop(
                    _ACQUISITION_CONTEXT_ENVIRON_KEY, None
                )
                if acquisition_context is not None:
                    acquisition_context.__exit__(None, None, None)
            finally:
                try:
                    if actor_tokens:
                        for actor_token in reversed(actor_tokens):
                            current_execution_actor.reset(actor_token)
                finally:
                    try:
                        _restore_lifecycle_globals(globals_before)
                    finally:
                        if probe_token is not None:
                            reset_disconnect_probe(probe_token)

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
            if _endpoint_name() == "artifact_content":
                # Project files are untrusted documents, even when an account
                # with broader authority opens them. A script-src 'self' policy
                # alone permits scripts uploaded beside an HTML/SVG document.
                # This applies to document navigation; media/fetch consumers
                # keep their normal parent-document policy and range support.
                response.headers["Content-Security-Policy"] += "; sandbox allow-downloads"
            if request.path.startswith(("/api/", "/share/")):
                response.headers["Cache-Control"] = "private, no-store"
            if request.path.startswith(("/share/", "/api/v1/public-links/")):
                response.headers["X-Robots-Tag"] = "noindex, nofollow"
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
                        "Audit projection failed timestamp=%s request_id=%s method=%s route=%s: %s",
                        *_request_log_context(),
                        self.services.redactor.redact(error),
                    )
            return response

        @app.errorhandler(ValidationError)
        def _validation_error(error):
            return self.error_response(
                "validation_error",
                "The request payload is invalid.",
                422,
                error.errors(include_input=False, include_context=False),
            )

        @app.errorhandler(ModelAcquisitionDenied)
        def _model_acquisition_denied(error):
            return self.error_response("model_acquisition_required", str(error), 403)

        @app.errorhandler(AdministratorRequired)
        def _administrator_required(error):
            return self.error_response("administrator_required", str(error), 403)

        @app.errorhandler(ActorAuthorizationDenied)
        def _actor_authorization_denied(_error):
            return self.error_response(
                "scope_denied",
                "The authenticated credential is no longer authorized for this operation.",
                403,
            )

        @app.errorhandler(RequestAbandoned)
        def _request_abandoned(_error):
            app.logger.info(
                "Request abandoned by client timestamp=%s request_id=%s method=%s route=%s",
                *_request_log_context(),
            )
            return self.error_response(
                "client_closed_request",
                "The client closed the connection before the response was ready.",
                499,
            )

        @app.errorhandler(AccessDenied)
        def _access_denied(error):
            return self.error_response("scope_denied", str(error), 403)

        @app.errorhandler(WorkspaceNotFound)
        @app.errorhandler(ProjectNotFound)
        @app.errorhandler(SessionNotFound)
        @app.errorhandler(ArtifactNotFound)
        def _resource_not_found(_error):
            return self.error_response("not_found", "Resource not found.", 404)

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
                "Unhandled API error timestamp=%s request_id=%s method=%s route=%s: %s\n%s",
                *_request_log_context(),
                safe_message,
                safe_trace,
            )
            return self.error_response(
                "internal_error",
                "An unexpected error occurred.",
                500,
            )
