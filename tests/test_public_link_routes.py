"""Focused wire-contract checks for the public-link HTTP adapters."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from flask import Flask, jsonify, request

from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.domain_blueprints import DomainBlueprints
from pandrator.web.public_links.contracts import (
    ProjectLinkContext,
    PublicLinkError,
    RedeemedProjectLink,
)
from pandrator.web.route_context import RouteContext


def _route_module(monkeypatch):
    helper_name = "pandrator.web.public_link_http"
    try:
        importlib.import_module(helper_name)
    except ModuleNotFoundError as error:
        if error.name != helper_name:
            raise
        # The parent-owned cookie helper is landing concurrently. This local
        # import shim follows its specified name contract for adapter tests only.
        helper = types.ModuleType(helper_name)
        helper.cookie_name = lambda link_id: f"pandrator_link_{UUID(link_id).hex}"
        monkeypatch.setitem(sys.modules, helper_name, helper)
    return importlib.import_module("pandrator.web.public_link_routes")


class FakeIdentity:
    def snapshot(self, *, observed_origin):
        return types.SimpleNamespace(canonical_origin="https://pandrator.test")


class FakeServices:
    identity = FakeIdentity()


class FakeGuards:
    def security_event(self, event: str, status: int) -> None:
        pass

    def __init__(self, app):
        self.app = app
        self.settings_calls = 0

    @staticmethod
    def _scopes() -> frozenset[str]:
        raw = request.headers.get("X-Scopes", "app.read app.write app.admin")
        return frozenset(raw.replace(",", " ").split())

    @staticmethod
    def _actor(scopes: frozenset[str] | None = None) -> UserActor:
        principal = request.headers.get("X-Principal", "owner_session")
        link_id = request.view_args.get("link_id") if request.view_args else None
        subject = f"project-link:{link_id}" if principal == "project_link" else "owner-session"
        return UserActor(
            user_id="00000000-0000-4000-8000-000000000001",
            auth_revision=1,
            subject=subject,
            principal_kind=principal,
            scopes=scopes if scopes is not None else FakeGuards._scopes(),
            browser_session_id="00000000-0000-4000-8000-000000000002",
        )

    def access_context(self):
        return AccessContext(self._actor())

    def project_link_context(self):
        if request.headers.get("X-Principal") != "project_link":
            return None
        link_id = request.headers.get("X-Bound-Link")
        if link_id is None:
            link_id = request.view_args.get("link_id") if request.view_args else None
        return ProjectLinkContext(
            self._actor(),
            {"id": link_id},
            "csrf-hash",
        )

    def error_response(self, code, message, status, details=None):
        return jsonify({"error": {"code": code, "message": message, "details": details}}), status

    def require_scope(self, *required):
        def decorate(function):
            def wrapped(*args, **kwargs):
                missing = sorted(set(required) - self._scopes())
                if missing:
                    return self.error_response("scope_denied", "Required scope is missing.", 403)
                return function(*args, **kwargs)

            wrapped.__name__ = function.__name__
            return wrapped

        return decorate


class FakeLinkService:
    def __init__(self):
        self.calls = []
        self.redeem_error = None
        self.expiry_seconds = 3600

    @staticmethod
    def _settings():
        return {"enabled": False, "public_origin": None, "revision": 0}

    def settings(self, access):
        self.calls.append(("settings", access))
        return self._settings()

    def configure(self, access, **kwargs):
        self.calls.append(("configure", access, kwargs))
        return {
            "enabled": kwargs["enabled"],
            "public_origin": kwargs["public_origin"],
            "revision": kwargs["expected_revision"] + 1,
        }

    def list_links(self, access, project_id):
        self.calls.append(("list", access, project_id))
        return {"items": []}

    def create_link(self, access, project_id, **kwargs):
        self.calls.append(("create", access, project_id, kwargs))
        return {
            "link": {"id": str(uuid4()), "project_id": project_id},
            "url": "https://pandrator.test/share/example#token=only-on-create",
            "already_created": False,
        }

    def revoke_link(self, access, link_id, *, expected_revision):
        self.calls.append(("revoke", access, link_id, expected_revision))
        return {"link": {"id": link_id, "revision": expected_revision + 1}}

    def redeem(self, link_id, secret):
        self.calls.append(("redeem", link_id, secret))
        if self.redeem_error is not None:
            raise self.redeem_error
        expires_at = (datetime.now(UTC) + timedelta(seconds=self.expiry_seconds)).isoformat()
        return RedeemedProjectLink(
            raw_browser_session="raw-browser-session-must-not-be-json",
            csrf_token="csrf-token-for-tab",
            link={"id": link_id, "expires_at": expires_at},
        )


class FakeReviewService:
    def __init__(self):
        self.calls = []

    def preview(self, access, link_id, *, offset, limit):
        self.calls.append(("preview", access, link_id, offset, limit))
        return {"link_id": link_id, "items": []}

    def submit(self, access, link_id, **kwargs):
        self.calls.append(("submit", access, link_id, kwargs))
        return {"submission_id": str(uuid4()), "status": "pending"}

    def list_submissions(self, access, project_id, **kwargs):
        self.calls.append(("list", access, project_id, kwargs))
        return {"items": [], **kwargs}

    def get_submission(self, access, submission_id, **kwargs):
        self.calls.append(("get", access, submission_id, kwargs))
        return {"submission_id": submission_id}

    def accept(self, access, submission_id, **kwargs):
        self.calls.append(("accept", access, submission_id, kwargs))
        return {"submission_id": submission_id, "status": "accepted"}

    def reject(self, access, submission_id, **kwargs):
        self.calls.append(("reject", access, submission_id, kwargs))
        return {"submission_id": submission_id, "status": "rejected"}


@pytest.fixture
def http(monkeypatch):
    _route_module(monkeypatch)
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
        SESSION_COOKIE_SECURE=False,
        PANDRATOR_PROXY_HOPS=2,
    )
    links, reviews = FakeLinkService(), FakeReviewService()
    guards = FakeGuards(app)
    route_context = RouteContext(FakeServices(), guards, Path("."))
    domains = DomainBlueprints(app)
    importlib.import_module("pandrator.web.public_link_routes").register_public_link_routes(
        domains, route_context, links, reviews
    )
    domains.register(app)
    return app.test_client(), links, reviews


def _link_id() -> str:
    return str(uuid4())


def _headers(*scopes: str, **extra: str) -> dict[str, str]:
    return {"X-Scopes": " ".join(scopes), **extra}


def _assert_private_response(response):
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_admin_settings_require_runtime_canonical_origin(http):
    client, links, _reviews = http
    invalid_revision = client.put(
        "/api/v1/admin/public-sharing",
        json={"expected_revision": True, "enabled": False, "public_origin": None},
    )
    assert invalid_revision.status_code == 422
    assert not [call for call in links.calls if call[0] == "configure"]

    mismatch = client.put(
        "/api/v1/admin/public-sharing",
        json={"expected_revision": 0, "enabled": True, "public_origin": "https://other.test"},
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"]["code"] == "public_origin_mismatch"
    assert not [call for call in links.calls if call[0] == "configure"]
    _assert_private_response(mismatch)

    updated = client.put(
        "/api/v1/admin/public-sharing",
        json={
            "expected_revision": 0,
            "enabled": True,
            "public_origin": "https://PANDRATOR.test",
        },
    )
    assert updated.status_code == 200
    assert updated.get_json()["public_origin"] == "https://pandrator.test"
    assert updated.get_json()["hosting"] == {
        "observed_origin": "http://localhost",
        "canonical_origin": "https://pandrator.test",
        "secure_transport": False,
        "secure_cookies": False,
        "trusted_hosts": ["localhost", "127.0.0.1", "[::1]"],
        "proxy_hops": 2,
    }
    _assert_private_response(updated)


def test_project_routes_have_scoped_authority_and_strict_bounded_bodies(http):
    client, links, _reviews = http
    project_id = _link_id()
    body = {
        "label": "External editor",
        "mode": "manage_project",
        "expires_in_days": 7,
        "target_artifact_id": None,
        "request_key": str(uuid4()),
    }
    admin_only = client.post(
        f"/api/v1/projects/{project_id}/links",
        json=body,
        headers=_headers("app.admin"),
    )
    assert admin_only.status_code == 403
    assert not [call for call in links.calls if call[0] == "create"]
    _assert_private_response(admin_only)

    invalid = client.post(
        f"/api/v1/projects/{project_id}/links",
        json={**body, "token": "must-not-be-echoed"},
        headers=_headers("app.read", "app.write"),
    )
    assert invalid.status_code == 422
    assert "must-not-be-echoed" not in invalid.get_data(as_text=True)

    oversized = client.post(
        f"/api/v1/projects/{project_id}/links",
        data=b" " * (8 * 1024 + 1),
        content_type="application/json",
        headers=_headers("app.read", "app.write"),
    )
    assert oversized.status_code == 413
    _assert_private_response(oversized)

    created = client.post(
        f"/api/v1/projects/{project_id}/links",
        json=body,
        headers=_headers("app.read", "app.write"),
    )
    assert created.status_code == 200
    call = next(call for call in links.calls if call[0] == "create")
    assert call[2] == project_id
    assert call[3] == {
        "label": "External editor",
        "mode": "manage_project",
        "expires_in_days": 7,
        "target_artifact_id": None,
        "request_key": body["request_key"],
    }
    assert (
        client.get(f"/api/v1/projects/{project_id}/links", headers=_headers("app.read")).status_code
        == 200
    )
    _assert_private_response(created)


def test_redemption_checks_origin_transport_and_returns_only_link_cookie(http):
    client, links, _reviews = http
    link_id = _link_id()
    body = {"token": "bearer-secret"}
    wrong_origin = client.post(
        f"/api/v1/public-links/{link_id}/redeem",
        json=body,
        headers={"Origin": "http://attacker.test"},
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.get_json()["error"]["code"] == "link_unavailable"
    assert not [call for call in links.calls if call[0] == "redeem"]
    _assert_private_response(wrong_origin)

    oversized = client.post(
        f"/api/v1/public-links/{link_id}/redeem",
        data=b" " * (4 * 1024 + 1),
        content_type="application/json",
    )
    assert oversized.status_code == 413
    assert not [call for call in links.calls if call[0] == "redeem"]
    _assert_private_response(oversized)

    remote_http = client.post(
        f"/api/v1/public-links/{link_id}/redeem",
        json=body,
        environ_base={"REMOTE_ADDR": "198.51.100.24"},
    )
    assert remote_http.status_code == 403
    assert not [call for call in links.calls if call[0] == "redeem"]

    redeemed = client.post(f"/api/v1/public-links/{link_id}/redeem", json=body)
    assert redeemed.status_code == 200
    payload = redeemed.get_json()
    assert payload["csrf_token"] == "csrf-token-for-tab"
    assert payload["link"]["id"] == link_id
    assert "raw-browser-session-must-not-be-json" not in redeemed.get_data(as_text=True)
    cookie_header = redeemed.headers.get("Set-Cookie", "")
    assert f"pandrator_link_{UUID(link_id).hex}" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header
    assert "Path=/api/v1" in cookie_header
    assert "Max-Age=" in cookie_header
    max_age = int(cookie_header.split("Max-Age=", 1)[1].split(";", 1)[0])
    assert 1 <= max_age <= 3600
    assert "Secure" not in cookie_header
    _assert_private_response(redeemed)


def test_redemption_rate_limit_isolated_to_link_failures(http):
    client, links, _reviews = http
    links.redeem_error = PublicLinkError("link_unavailable", 403)
    path = f"/api/v1/public-links/{_link_id()}/redeem"
    for _ in range(5):
        failure = client.post(path, json={"token": "invalid"})
        assert failure.status_code == 403
        assert failure.get_json()["error"]["code"] == "link_unavailable"
    limited = client.post(path, json={"token": "invalid"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    _assert_private_response(limited)


def test_guest_review_requires_the_matching_project_link_actor_and_uuid_key(http):
    client, _links, reviews = http
    link_id = _link_id()
    review_path = f"/api/v1/public-links/{link_id}/review"
    owner = client.get(review_path, headers=_headers("app.read", "app.admin"))
    assert owner.status_code == 403
    assert not reviews.calls

    mismatch = client.get(
        review_path,
        headers=_headers(
            "app.read", "app.write", X_Principal="project_link", X_Bound_Link=_link_id()
        ),
    )
    assert mismatch.status_code == 403
    assert not reviews.calls

    guest_headers = _headers(
        "app.read", "app.write", X_Principal="project_link", X_Bound_Link=link_id
    )
    preview = client.get(review_path + "?offset=3&limit=25", headers=guest_headers)
    assert preview.status_code == 200
    assert reviews.calls[-1][0] == "preview"
    assert reviews.calls[-1][3:] == (3, 25)
    assert reviews.calls[-1][1].actor.principal_kind == "project_link"
    _assert_private_response(preview)

    body = {
        "expected_source_hash": "a" * 64,
        "changes": [{"segment_id": "segment-1", "text": "Corrected line"}],
        "reviewer_label": "Reviewer",
    }
    missing_key = client.post(
        f"/api/v1/public-links/{link_id}/submissions",
        json=body,
        headers=guest_headers,
    )
    assert missing_key.status_code == 422
    assert len(reviews.calls) == 1

    oversized = client.post(
        f"/api/v1/public-links/{link_id}/submissions",
        data=b" " * (256 * 1024 + 1),
        content_type="application/json",
        headers=guest_headers,
    )
    assert oversized.status_code == 413
    assert len(reviews.calls) == 1
    _assert_private_response(oversized)

    key = str(uuid4())
    submitted = client.post(
        f"/api/v1/public-links/{link_id}/submissions",
        json=body,
        headers={**guest_headers, "Idempotency-Key": key},
    )
    assert submitted.status_code == 200
    assert reviews.calls[-1][0] == "submit"
    assert reviews.calls[-1][3]["request_key"] == key
    assert reviews.calls[-1][3]["changes"] == body["changes"]
    _assert_private_response(submitted)


def test_operator_review_routes_require_project_scopes_and_idempotency_keys(http):
    client, _links, reviews = http
    project_id, submission_id = _link_id(), _link_id()
    path = f"/api/v1/projects/{project_id}/link-submissions?status=accepted&offset=2&limit=10"
    denied = client.get(path, headers=_headers("app.admin"))
    assert denied.status_code == 403
    assert not reviews.calls

    listed = client.get(path, headers=_headers("app.read", "app.write"))
    assert listed.status_code == 200
    assert reviews.calls[-1][0] == "list"
    assert reviews.calls[-1][3] == {
        "status": "accepted",
        "offset": 2,
        "limit": 10,
    }
    assert (
        client.get(
            f"/api/v1/link-submissions/{submission_id}",
            headers=_headers("app.read", "app.write"),
        ).status_code
        == 200
    )

    reject_path = f"/api/v1/link-submissions/{submission_id}/reject"
    rejected_without_key = client.post(
        reject_path,
        json={"expected_revision": 1},
        headers=_headers("app.read", "app.write"),
    )
    assert rejected_without_key.status_code == 422
    assert not [call for call in reviews.calls if call[0] == "reject"]

    key = str(uuid4())
    accepted = client.post(
        f"/api/v1/link-submissions/{submission_id}/accept",
        json={
            "expected_revision": 1,
            "expected_source_hash": "b" * 64,
            "expected_document_revision": 4,
            "expected_selected_artifact_id": project_id,
        },
        headers={**_headers("app.read", "app.write"), "Idempotency-Key": key},
    )
    assert accepted.status_code == 200
    assert reviews.calls[-1][0] == "accept"
    assert reviews.calls[-1][3] == {
        "expected_revision": 1,
        "expected_source_hash": "b" * 64,
        "expected_document_revision": 4,
        "expected_selected_artifact_id": project_id,
        "idempotency_key": key,
    }
    _assert_private_response(accepted)


def test_openapi_contract_lists_the_fixed_public_link_operations():
    from pandrator.web.public_link_openapi import PUBLIC_LINK_SCHEMAS, public_link_paths

    paths = public_link_paths()
    operation_ids = {
        operation["operationId"] for methods in paths.values() for operation in methods.values()
    }
    assert operation_ids == {
        "getPublicSharingSettings",
        "configurePublicSharing",
        "listProjectLinks",
        "createProjectLink",
        "revokeProjectLink",
        "redeemProjectLink",
        "getPublicTranslationReview",
        "submitPublicTranslationReview",
        "listLinkSubmissions",
        "getLinkSubmission",
        "acceptLinkSubmission",
        "rejectLinkSubmission",
        "leaveProjectLink",
        "getProjectLinkServices",
    }
    assert paths["/api/v1/public-links/{linkId}/review"]["get"]["security"] == [
        {"projectLinkSession": []}
    ]
    assert paths["/api/v1/public-links/{linkId}/redeem"]["post"]["security"] == []
    logout = paths["/api/v1/public-links/{linkId}/logout"]["post"]
    assert "204" in logout["responses"] and "200" not in logout["responses"]
    assert {p["name"] for p in logout["parameters"]} == {
        "linkId",
        "X-Pandrator-Project-Link",
        "X-CSRF-Token",
    }
    for model in PUBLIC_LINK_SCHEMAS.values():
        model.model_json_schema()
