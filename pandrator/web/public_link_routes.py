"""HTTP adapters for public project links and pinned translation review."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from uuid import UUID

from flask import jsonify, make_response, request
from pydantic import TypeAdapter, ValidationError
from werkzeug.exceptions import RequestEntityTooLarge

from .access.contracts import AccessDenied, ProjectNotFound
from .auth import LoginThrottle
from .domain_blueprints import DomainBlueprints
from .identity import canonical_origin
from .public_link_http import cookie_name
from .public_link_schemas import (
    LinkSubmissionsQuery,
    ProjectLinkCreateRequest,
    ProjectLinkRevokeRequest,
    PublicLinkRedeemRequest,
    PublicReviewQuery,
    PublicSharingSettingsRequest,
    PublicTranslationSubmissionRequest,
    SubmissionAcceptRequest,
    SubmissionDetailQuery,
    SubmissionRejectRequest,
    UuidString,
)
from .public_links.contracts import ProjectLinkContext, PublicLinkError
from .public_links.reviews import PublicTranslationReviewService
from .public_links.service import PublicLinkService
from .route_context import RouteContext

_UUID_ADAPTER = TypeAdapter(UuidString)


def register_public_link_routes(
    app: DomainBlueprints,
    context: RouteContext,
    links: PublicLinkService,
    reviews: PublicTranslationReviewService,
) -> None:
    """Attach the bounded public-link API to the owning domain blueprints."""

    guards = context.guards
    # This throttle is intentionally private to this app's link-redemption route.
    redemption_throttle = LoginThrottle(max_attempts=5, initial_block_seconds=30.0)

    def private_api(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            response = make_response(function(*args, **kwargs))
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            return response

        return wrapped

    def safe_errors(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except PublicLinkError as error:
                return guards.error_response(
                    error.code,
                    error.code.replace("_", " "),
                    error.status_code,
                )
            except ValidationError:
                return guards.error_response("validation_error", "Invalid request.", 422)
            except RequestEntityTooLarge:
                return guards.error_response(
                    "request_too_large", "Request body exceeds the size limit.", 413
                )
            except PermissionError:
                return guards.error_response(
                    "access_denied", "This operation is not authorized.", 403
                )
            except ProjectNotFound:
                return guards.error_response("not_found", "Project not found.", 404)

        return wrapped

    def parse_json(model, max_bytes: int):
        request.max_content_length = max_bytes
        return model.model_validate(request.get_json(silent=True) or {})

    def parse_uuid(value: str) -> str:
        _UUID_ADAPTER.validate_python(value)
        return str(UUID(value))

    def idempotency_key() -> str:
        raw = request.headers.get("Idempotency-Key")
        if raw is None:
            raw = ""
        return parse_uuid(raw)

    def request_origin() -> str | None:
        try:
            return canonical_origin(request.url_root)
        except (TypeError, ValueError):
            return None

    def normalized_origin(value: str | None) -> str | None:
        if not isinstance(value, str) or value != value.strip():
            return None
        try:
            return canonical_origin(value)
        except (TypeError, ValueError):
            return None

    def hosting() -> dict[str, Any]:
        observed_origin = request_origin()
        if observed_origin is None:
            raise PublicLinkError("invalid_request_origin", 400)
        identity = context.services.identity.snapshot(observed_origin=request.url_root)
        configured_hosts = guards.app.config.get("TRUSTED_HOSTS", [])
        trusted_hosts = (
            [str(host) for host in configured_hosts]
            if isinstance(configured_hosts, (list, tuple, set))
            else []
        )
        proxy_hops = guards.app.config.get("PANDRATOR_PROXY_HOPS", 0)
        try:
            proxy_hops = max(0, int(proxy_hops))
        except (TypeError, ValueError):
            proxy_hops = 0
        return {
            "observed_origin": observed_origin,
            "canonical_origin": identity.canonical_origin,
            "secure_transport": request.is_secure,
            "secure_cookies": bool(guards.app.config.get("SESSION_COOKIE_SECURE", False)),
            "trusted_hosts": trusted_hosts,
            "proxy_hops": proxy_hops,
        }

    def settings_response(settings: dict[str, Any]):
        return jsonify({**settings, "hosting": hosting()})

    def link_browser_context(link_id: str):
        project_link = guards.project_link_context()
        if (
            not isinstance(project_link, ProjectLinkContext)
            or project_link.link.get("id") != link_id
        ):
            raise AccessDenied()
        access = guards.access_context()
        if access.actor != project_link.actor:
            raise AccessDenied()
        return access

    def client_ip_key() -> str:
        try:
            return str(ipaddress.ip_address(str(request.remote_addr or "")))
        except ValueError:
            return "unknown"

    def loopback_client() -> bool:
        try:
            return ipaddress.ip_address(str(request.remote_addr or "")).is_loopback
        except ValueError:
            return False

    def check_origin_and_transport():
        origin = request.headers.get("Origin")
        if origin is not None:
            if origin != origin.strip():
                return False
            parsed_origin = normalized_origin(origin)
            observed = request_origin()
            if parsed_origin is None or observed is None or parsed_origin != observed:
                return False
        if not request.is_secure and not loopback_client():
            return False
        return True

    @app.get("/api/v1/admin/public-sharing")
    @private_api
    @guards.require_scope("app.admin")
    @safe_errors
    def public_sharing_get():
        settings = links.settings(guards.access_context())
        return settings_response(settings)

    @app.put("/api/v1/admin/public-sharing")
    @private_api
    @guards.require_scope("app.admin")
    @safe_errors
    def public_sharing_put():
        payload = parse_json(PublicSharingSettingsRequest, 8 * 1024)
        public_origin = payload.public_origin
        if payload.enabled:
            requested = normalized_origin(public_origin)
            canonical = context.services.identity.snapshot(
                observed_origin=request.url_root
            ).canonical_origin
            if requested is None or requested != canonical:
                return guards.error_response(
                    "public_origin_mismatch",
                    "Public sharing origin must match this installation's canonical origin.",
                    409,
                )
            public_origin = canonical
        settings = links.configure(
            guards.access_context(),
            expected_revision=payload.expected_revision,
            enabled=payload.enabled,
            public_origin=public_origin,
        )
        return settings_response(settings)

    @app.get("/api/v1/projects/<project_id>/links")
    @private_api
    @guards.require_scope("app.read")
    @safe_errors
    def project_links_get(project_id: str):
        project_id = parse_uuid(project_id)
        return jsonify(links.list_links(guards.access_context(), project_id))

    @app.post("/api/v1/projects/<project_id>/links")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def project_links_post(project_id: str):
        project_id = parse_uuid(project_id)
        payload = parse_json(ProjectLinkCreateRequest, 8 * 1024)
        return jsonify(
            links.create_link(
                guards.access_context(),
                project_id,
                label=payload.label,
                mode=payload.mode,
                expires_in_days=payload.expires_in_days,
                target_artifact_id=(
                    parse_uuid(payload.target_artifact_id)
                    if payload.target_artifact_id is not None
                    else None
                ),
                request_key=parse_uuid(payload.request_key),
            )
        )

    @app.post("/api/v1/project-links/<link_id>/revoke")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def project_link_revoke(link_id: str):
        link_id = parse_uuid(link_id)
        payload = parse_json(ProjectLinkRevokeRequest, 8 * 1024)
        return jsonify(
            links.revoke_link(
                guards.access_context(),
                link_id,
                expected_revision=payload.expected_revision,
            )
        )

    @app.post("/api/v1/public-links/<link_id>/redeem")
    @private_api
    @safe_errors
    def public_link_redeem(link_id: str):
        request.max_content_length = 4 * 1024
        link_id = parse_uuid(link_id)
        payload = parse_json(PublicLinkRedeemRequest, 4 * 1024)
        if not check_origin_and_transport():
            raise PublicLinkError("link_unavailable", 403)
        ip_key = client_ip_key()
        retry_after = redemption_throttle.retry_after(ip_key)
        if retry_after > 0:
            guards.security_event("project_link_redemption_throttled", 429)
            response = make_response(
                guards.error_response("rate_limited", "Too many redemption attempts.", 429)
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        try:
            redeemed = links.redeem(link_id, payload.token)
        except PublicLinkError as error:
            guards.security_event("project_link_redemption_failed", error.status_code)
            if error.code == "link_unavailable":
                redemption_throttle.record_failure(ip_key)
            raise
        try:
            expires_at = datetime.fromisoformat(
                str(redeemed.link["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None:
                raise ValueError
            max_age = min(
                86400,
                int((expires_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()),
            )
        except (KeyError, TypeError, ValueError):
            raise PublicLinkError("link_unavailable", 403) from None
        if max_age < 1:
            raise PublicLinkError("link_unavailable", 403)
        response = make_response(
            jsonify({"link": redeemed.link, "csrf_token": redeemed.csrf_token})
        )
        response.set_cookie(
            cookie_name(link_id),
            redeemed.raw_browser_session,
            max_age=max_age,
            httponly=True,
            secure=request.is_secure,
            samesite="Lax",
            path="/api/v1",
        )
        redemption_throttle.reset(ip_key)
        return response

    @app.post("/api/v1/public-links/<link_id>/logout")
    @guards.require_scope("app.read")
    @private_api
    @safe_errors
    def public_link_logout(link_id: str):
        link_id = parse_uuid(link_id)
        link_browser_context(link_id)
        raw = request.cookies.get(cookie_name(link_id), "")
        if raw:
            context.services.auth.browser_sessions.revoke(raw)
        response = make_response("", 204)
        response.delete_cookie(cookie_name(link_id), path="/api/v1")
        return response

    @app.get("/api/v1/public-links/<link_id>/services/<kind>")
    @guards.require_scope("app.read")
    @private_api
    @safe_errors
    def public_link_services(link_id: str, kind: str):
        from .public_links.resources import project_link_catalogue

        link_id = parse_uuid(link_id)
        link_browser_context(link_id)
        shared = guards.project_link_context()
        if shared is None:
            raise PublicLinkError("link_unavailable", 401)
        session_id = parse_uuid(request.args.get("session_id", ""))
        return jsonify(project_link_catalogue(context.services, shared, session_id, kind))

    @app.get("/api/v1/public-links/<link_id>/review")
    @private_api
    @guards.require_scope("app.read")
    @safe_errors
    def public_translation_review_get(link_id: str):
        link_id = parse_uuid(link_id)
        query = PublicReviewQuery.model_validate(request.args.to_dict())
        access = link_browser_context(link_id)
        return jsonify(
            reviews.preview(
                access,
                link_id,
                offset=query.offset,
                limit=query.limit,
            )
        )

    @app.post("/api/v1/public-links/<link_id>/submissions")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def public_translation_submission_post(link_id: str):
        link_id = parse_uuid(link_id)
        payload = parse_json(PublicTranslationSubmissionRequest, 256 * 1024)
        request_key = idempotency_key()
        access = link_browser_context(link_id)
        return jsonify(
            reviews.submit(
                access,
                link_id,
                expected_source_hash=payload.expected_source_hash,
                changes=[change.model_dump() for change in payload.changes],
                reviewer_label=payload.reviewer_label,
                request_key=request_key,
            )
        )

    @app.get("/api/v1/projects/<project_id>/link-submissions")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def link_submissions_get(project_id: str):
        project_id = parse_uuid(project_id)
        query = LinkSubmissionsQuery.model_validate(request.args.to_dict())
        return jsonify(
            reviews.list_submissions(
                guards.access_context(),
                project_id,
                status=query.status,
                offset=query.offset,
                limit=query.limit,
            )
        )

    @app.get("/api/v1/link-submissions/<submission_id>")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def link_submission_get(submission_id: str):
        submission_id = parse_uuid(submission_id)
        query = SubmissionDetailQuery.model_validate(request.args.to_dict())
        return jsonify(
            reviews.get_submission(
                guards.access_context(),
                submission_id,
                offset=query.offset,
                limit=query.limit,
            )
        )

    @app.post("/api/v1/link-submissions/<submission_id>/accept")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def link_submission_accept(submission_id: str):
        submission_id = parse_uuid(submission_id)
        payload = parse_json(SubmissionAcceptRequest, 8 * 1024)
        key = idempotency_key()
        return jsonify(
            reviews.accept(
                guards.access_context(),
                submission_id,
                expected_revision=payload.expected_revision,
                expected_source_hash=payload.expected_source_hash,
                expected_document_revision=payload.expected_document_revision,
                expected_selected_artifact_id=parse_uuid(payload.expected_selected_artifact_id),
                idempotency_key=key,
            )
        )

    @app.post("/api/v1/link-submissions/<submission_id>/reject")
    @private_api
    @guards.require_scope("app.read", "app.write")
    @safe_errors
    def link_submission_reject(submission_id: str):
        submission_id = parse_uuid(submission_id)
        payload = parse_json(SubmissionRejectRequest, 8 * 1024)
        key = idempotency_key()
        return jsonify(
            reviews.reject(
                guards.access_context(),
                submission_id,
                expected_revision=payload.expected_revision,
                idempotency_key=key,
            )
        )
