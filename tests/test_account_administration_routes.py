from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote
from uuid import uuid4

import pytest
from flask import Flask, Request, request
from sqlalchemy import func, select

from pandrator.web.account_routes import register_account_routes
from pandrator.web.accounts.auth_contracts import PrincipalKind
from pandrator.web.accounts.auth_models import (
    BrowserAuthSession,
    PasswordResetGrant,
    UserCredential,
)
from pandrator.web.accounts.auth_state import account_state_in_session, initial_user_id
from pandrator.web.accounts.execution_actor import HostActor, current_execution_actor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.passwords import hash_password
from pandrator.web.application_services import ApplicationServices
from pandrator.web.auth import LoginThrottle
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.model_base import utcnow
from pandrator.web.models import ApiIdempotency, ApiToken, AuditEvent
from pandrator.web.route_context import RouteContext

_ADMIN_SCOPES = frozenset({"app.admin"})
_READ_SCOPES = frozenset({"app.read"})
_NEW_PASSWORD = "Synthetic new password 2026!"


@pytest.fixture
def account_application(tmp_path: Path) -> Iterator[tuple[Flask, ApplicationServices]]:
    services = ApplicationServices.build(data_root=tmp_path / "account-administration-data")
    services.auth.initialize_owner("Synthetic initial administrator password")
    flask_app = Flask("account-administration-tests")
    flask_app.config.update(TESTING=True, SECRET_KEY="synthetic-test-only-secret")
    guards = ApiGuards(flask_app, services, testing=True, script_policy="'self'")
    guards.register()
    register_account_routes(
        flask_app,
        RouteContext(services=services, guards=guards, static_dir=tmp_path),
    )
    try:
        yield flask_app, services
    finally:
        services.close()


def _owner_id(services: ApplicationServices) -> str:
    with services.database.session() as session:
        return initial_user_id(session)


def _api_token(
    services: ApplicationServices,
    *,
    scopes: frozenset[str] = _ADMIN_SCOPES,
    principal_kind: PrincipalKind = "api_token",
) -> str:
    user_id = _owner_id(services)
    with services.database.session() as session:
        state = account_state_in_session(session, user_id)
    _record, raw_token = services.auth.create_api_token(
        "synthetic account route token",
        scopes=scopes,
        principal_kind=principal_kind,
        created_by="synthetic-test",
        user_id=state.user_id,
        auth_revision=state.auth_revision,
    )
    return raw_token


def _headers(raw_token: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {raw_token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _add_user(
    services: ApplicationServices,
    *,
    display_name: str = "Synthetic target",
    login_name: str | None = None,
    administrator: bool = False,
    active: bool = True,
    password_hash: str | None = None,
) -> str:
    identifier = str(uuid4())
    with services.database.immediate_session() as session:
        user = User(
            id=identifier,
            legacy_subject=None,
            display_name=display_name,
            is_active=active,
            is_instance_admin=administrator,
        )
        session.add(user)
        session.flush([user])
        credential = UserCredential(
            user_id=identifier,
            login_name=login_name or f"user-{identifier[:8]}",
            password_hash=password_hash,
            auth_revision=1,
        )
        workspace = Workspace(
            kind="personal",
            name=display_name,
            personal_owner_user_id=identifier,
            state="active",
            revision=1,
        )
        session.add_all([credential, workspace])
        session.flush([credential, workspace])
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=identifier,
                role="owner",
            )
        )
    return identifier


def _error(response):
    payload = response.get_json()
    assert payload is not None and isinstance(payload.get("error"), dict)
    return payload["error"]


def test_scope_and_bootstrap_refusals_precede_json_and_domain_work(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    original_user_count = None
    with services.database.session() as session:
        original_user_count = session.scalar(select(func.count()).select_from(User))

    unauthenticated = client.post(
        "/api/v1/admin/users",
        data=b"{malformed",
        content_type="application/json",
    )
    assert unauthenticated.status_code == 401
    assert _error(unauthenticated)["code"] == "authentication_required"

    read_token = _api_token(services, scopes=_READ_SCOPES)
    scoped = client.post(
        "/api/v1/admin/users",
        headers=_headers(read_token),
        data=b"{malformed",
        content_type="application/json",
    )
    assert scoped.status_code == 403
    assert _error(scoped)["code"] == "scope_denied"

    bootstrap_token = _api_token(
        services,
        scopes=_READ_SCOPES,
        principal_kind="manager_bootstrap",
    )
    bootstrap = client.post(
        "/api/v1/admin/users",
        headers=_headers(bootstrap_token),
        data=b"{malformed",
        content_type="application/json",
    )
    assert bootstrap.status_code == 403
    assert _error(bootstrap)["code"] == "scope_denied"

    admin_token = _api_token(services)
    owner_id = _owner_id(services)
    with services.database.immediate_session() as session:
        owner = session.get(User, owner_id)
        assert owner is not None
        owner.is_instance_admin = False
    role_refusal = client.post(
        "/api/v1/admin/users",
        headers=_headers(admin_token),
        data=b"{malformed",
        content_type="application/json",
    )
    assert role_refusal.status_code == 401
    assert _error(role_refusal)["code"] == "authentication_required"

    with services.database.session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == original_user_count
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0


def test_http_host_actor_cannot_become_administrator_before_body_validation(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    token = _api_token(services)

    @flask_app.before_request
    def bind_host_actor_for_this_request():
        if request.headers.get("X-Test-Host-Actor") == "yes":
            current_execution_actor.set(HostActor("pandrator-cli"))

    response = flask_app.test_client().post(
        "/api/v1/admin/users",
        headers={**_headers(token), "X-Test-Host-Actor": "yes"},
        data=b"{malformed",
        content_type="application/json",
    )
    assert response.status_code == 403
    assert _error(response)["code"] == "administrator_required"
    with services.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0


def test_account_json_is_read_before_any_immediate_writer(
    account_application: tuple[Flask, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    token = _api_token(services)
    target_id = _add_user(services)
    original_immediate_session = services.database.immediate_session
    original_get_json = Request.get_json
    immediate_writer_active = False
    json_reads = 0

    @contextmanager
    def tracked_immediate_session(*args: Any, **kwargs: Any):
        nonlocal immediate_writer_active
        with original_immediate_session(*args, **kwargs) as session:
            immediate_writer_active = True
            try:
                yield session
            finally:
                immediate_writer_active = False

    def assert_json_read_without_writer(flask_request: Request, *args: Any, **kwargs: Any) -> Any:
        nonlocal json_reads
        assert not immediate_writer_active
        json_reads += 1
        return original_get_json(flask_request, *args, **kwargs)

    monkeypatch.setattr(services.database, "immediate_session", tracked_immediate_session)
    monkeypatch.setattr(Request, "get_json", assert_json_read_without_writer)

    created = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, "prewriter-create-0001"),
        json={"login_name": "prewriter-user", "display_name": "Prewriter user"},
    )
    updated = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, "prewriter-update-0001"),
        json={"expected_auth_revision": 1, "display_name": "Prewriter renamed"},
    )
    reset = client.post(
        f"/api/v1/admin/users/{target_id}/password-reset",
        headers=_headers(token),
        json={"expected_auth_revision": 1},
    )
    redemption = client.post(
        "/api/v1/auth/password-reset",
        json={"token": "not-a-valid-reset", "new_password": _NEW_PASSWORD},
    )

    assert created.status_code == 201
    assert updated.status_code == 200
    assert reset.status_code == 200
    assert redemption.status_code == 400
    assert json_reads == 4


@pytest.mark.parametrize(
    ("path", "body", "administrator_only"),
    [
        (
            "/api/v1/admin/users",
            {"login_name": "oversized-user", "display_name": "m" * (17 * 1024)},
            True,
        ),
        (
            "/api/v1/auth/password-reset",
            {"token": "t" * (17 * 1024), "new_password": _NEW_PASSWORD},
            False,
        ),
    ],
)
def test_oversized_account_metadata_and_reset_json_return_413(
    account_application: tuple[Flask, ApplicationServices],
    path: str,
    body: dict[str, Any],
    administrator_only: bool,
) -> None:
    flask_app, services = account_application
    headers = _headers(_api_token(services), "oversized-json-0001") if administrator_only else {}

    response = flask_app.test_client().post(path, headers=headers, json=body)

    assert response.status_code == 413


def test_admin_credential_revoked_during_json_read_is_rechecked_before_writes(
    account_application: tuple[Flask, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flask_app, services = account_application
    owner_id = _owner_id(services)
    token = _api_token(services)
    with services.database.session() as session:
        token_row = session.scalar(
            select(ApiToken).where(
                ApiToken.user_id == owner_id,
                ApiToken.label == "synthetic account route token",
            )
        )
        assert token_row is not None
        token_id = token_row.id

    original_get_json = Request.get_json
    revoked_while_reading = False

    def read_then_revoke_token(flask_request: Request, *args: Any, **kwargs: Any) -> Any:
        nonlocal revoked_while_reading
        payload = original_get_json(flask_request, *args, **kwargs)
        if flask_request.path == "/api/v1/admin/users":
            with services.database.immediate_session() as session:
                token_row = session.get(ApiToken, token_id)
                assert token_row is not None
                token_row.revoked_at = utcnow()
            revoked_while_reading = True
        return payload

    monkeypatch.setattr(Request, "get_json", read_then_revoke_token)
    response = flask_app.test_client().post(
        "/api/v1/admin/users",
        headers=_headers(token, "revoked-during-read-0001"),
        json={"login_name": "revoked-during-read", "display_name": "Must not exist"},
    )

    assert revoked_while_reading
    assert response.status_code == 403
    assert _error(response)["code"] == "administrator_required"
    with services.database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(UserCredential)
                .where(UserCredential.login_name == "revoked-during-read")
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0


def test_list_and_create_return_explicit_summaries_and_create_is_idempotent(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    owner_id = _owner_id(services)
    token = _api_token(services)

    listed = client.get("/api/v1/admin/users", headers=_headers(token))
    assert listed.status_code == 200
    listing = listed.get_json()
    assert listing is not None
    assert listing["initial_user_id"] == owner_id
    assert listing["named_user_access"] is False
    owner_summary = next(item for item in listing["items"] if item["user_id"] == owner_id)
    assert set(owner_summary) == {
        "user_id",
        "login_name",
        "display_name",
        "role",
        "is_active",
        "auth_revision",
        "password_configured",
    }
    assert owner_summary["password_configured"] is True

    body = {"login_name": "  New.User  ", "display_name": " New user "}
    key = "create-account-0001"
    created = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, key),
        json=body,
    )
    assert created.status_code == 201
    created_summary = created.get_json()
    assert created_summary is not None
    created_user_id = created_summary["user_id"]
    assert created_summary["login_name"] == "new.user"
    assert created_summary["display_name"] == "New user"
    assert created_summary["role"] == "user"
    assert created_summary["password_configured"] is False

    replay = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, key),
        json=body,
    )
    assert replay.status_code == 201
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.get_json() == created_summary

    conflict = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, key),
        json={**body, "display_name": "Changed payload"},
    )
    assert conflict.status_code == 409
    assert _error(conflict)["code"] == "idempotency_conflict"

    explicit_admin = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, "create-admin-0001"),
        json={
            "login_name": "new-admin",
            "display_name": "New Administrator",
            "role": "administrator",
        },
    )
    assert explicit_admin.status_code == 201
    admin_summary = explicit_admin.get_json()
    assert admin_summary is not None and admin_summary["role"] == "administrator"
    assert admin_summary["password_configured"] is False

    secret_marker = "synthetic-secret-must-not-be-accepted"
    forbidden_password = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, "create-secret-0001"),
        json={
            "login_name": "secret-user",
            "display_name": "Secret user",
            "password": secret_marker,
        },
    )
    assert forbidden_password.status_code == 422
    assert secret_marker not in forbidden_password.get_data(as_text=True)

    with services.database.session() as session:
        user = session.get(User, created_user_id)
        credential = session.get(UserCredential, created_user_id)
        workspace = session.scalar(
            select(Workspace).where(Workspace.personal_owner_user_id == created_user_id)
        )
        assert user is not None and user.display_name == "New user"
        assert credential is not None and credential.login_name == "new.user"
        assert workspace is not None and workspace.kind == "personal"
        assert session.get(WorkspaceMembership, (workspace.id, created_user_id)) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(UserCredential)
                .where(UserCredential.login_name == "secret-user")
            )
            == 0
        )


def test_update_replay_stale_revision_and_security_revocation(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    token = _api_token(services)
    target_id = _add_user(services)
    browser = services.auth.browser_sessions.issue(
        target_id,
        1,
        subject=f"user:{target_id}",
        principal_kind="owner_session",
        scopes=_READ_SCOPES,
    )
    api_token, raw_api_token = services.auth.create_api_token(
        "synthetic target credential",
        scopes=_READ_SCOPES,
        user_id=target_id,
        auth_revision=1,
    )
    assert raw_api_token

    body = {"expected_auth_revision": 1, "display_name": "Renamed target"}
    key = "update-account-0001"
    updated = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, key),
        json=body,
    )
    assert updated.status_code == 200
    assert updated.get_json()["display_name"] == "Renamed target"
    replay = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, key),
        json=body,
    )
    assert replay.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.get_json() == updated.get_json()
    changed = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, key),
        json={**body, "display_name": "Different target"},
    )
    assert changed.status_code == 409
    assert _error(changed)["code"] == "idempotency_conflict"

    stale = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, "stale-account-0001"),
        json={"expected_auth_revision": 9, "display_name": "Ignored"},
    )
    assert stale.status_code == 409
    assert _error(stale)["code"] == "stale_account"

    null_update = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, "null-update-0001"),
        json={"expected_auth_revision": 1, "display_name": None},
    )
    assert null_update.status_code == 422

    disabled = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=_headers(token, "disable-account-0001"),
        json={"expected_auth_revision": 1, "is_active": False},
    )
    assert disabled.status_code == 200
    assert disabled.get_json()["auth_revision"] == 2
    with services.database.session() as session:
        browser_row = session.get(BrowserAuthSession, browser.record_id)
        token_row = session.get(ApiToken, api_token.id)
        credential = session.get(UserCredential, target_id)
        assert browser_row is not None and browser_row.revoked_at is not None
        assert token_row is not None and token_row.revoked_at is not None
        assert credential is not None and credential.auth_revision == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiIdempotency)
                .where(ApiIdempotency.idempotency_key == "stale-account-0001")
            )
            == 0
        )


def test_revoked_administrator_after_guard_preflight_cannot_reserve_or_create(
    account_application: tuple[Flask, ApplicationServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    token = _api_token(services)
    owner_id = _owner_id(services)
    database = services.database
    original_immediate_session = database.immediate_session
    immediate_calls = 0

    @contextmanager
    def revoke_after_guard_preflight(*args, **kwargs):
        nonlocal immediate_calls
        immediate_calls += 1
        with original_immediate_session(*args, **kwargs) as session:
            yield session
        if immediate_calls == 2:
            with original_immediate_session() as session:
                credential = session.get(UserCredential, owner_id)
                assert credential is not None
                credential.auth_revision += 1

    monkeypatch.setattr(database, "immediate_session", revoke_after_guard_preflight)
    response = client.post(
        "/api/v1/admin/users",
        headers=_headers(token, "race-account-0001"),
        json={"login_name": "must-not-exist", "display_name": "Must not exist"},
    )
    assert response.status_code == 403
    assert _error(response)["code"] == "administrator_required"
    with original_immediate_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(UserCredential)
                .where(UserCredential.login_name == "must-not-exist")
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0


def test_public_adapter_protects_initial_administrator_even_with_successor(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    token = _api_token(services)
    initial_id = _owner_id(services)
    _add_user(
        services,
        display_name="Ready successor",
        administrator=True,
        password_hash=hash_password("synthetic successor password"),
    )

    demote = client.patch(
        f"/api/v1/admin/users/{initial_id}",
        headers=_headers(token, "initial-demotion-0001"),
        json={"expected_auth_revision": 1, "role": "user"},
    )
    assert demote.status_code == 409
    assert _error(demote)["code"] == "initial_administrator_required"
    assert _error(demote)["message"] == (
        "Enable team access before retiring the initial Administrator."
    )

    disable = client.patch(
        f"/api/v1/admin/users/{initial_id}",
        headers=_headers(token, "initial-disable-0001"),
        json={"expected_auth_revision": 1, "is_active": False},
    )
    assert disable.status_code == 409
    assert _error(disable)["code"] == "initial_administrator_required"

    profile = client.patch(
        f"/api/v1/admin/users/{initial_id}",
        headers=_headers(token, "initial-profile-0001"),
        json={"expected_auth_revision": 1, "display_name": "Initial profile edit"},
    )
    assert profile.status_code == 200
    assert profile.get_json()["display_name"] == "Initial profile edit"

    with services.database.session() as session:
        user = session.get(User, initial_id)
        assert user is not None and user.is_instance_admin and user.is_active
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiIdempotency)
                .where(ApiIdempotency.idempotency_key == "initial-demotion-0001")
            )
            == 0
        )


def test_reset_issue_reissues_expires_and_keeps_bearer_secret_out_of_storage(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    token = _api_token(services)
    disabled_id = _add_user(services, active=False)
    denied = client.post(
        f"/api/v1/admin/users/{disabled_id}/password-reset",
        headers=_headers(token),
        json={"expected_auth_revision": 1},
    )
    assert denied.status_code == 409
    assert _error(denied)["code"] == "account_disabled"

    target_id = _add_user(services, display_name="Reset target")
    first = client.post(
        f"/api/v1/admin/users/{target_id}/password-reset",
        headers=_headers(token, "reset-key-ignored-01"),
        json={"expected_auth_revision": 1},
    )
    assert first.status_code == 200
    first_payload = first.get_json()
    assert first_payload is not None
    first_reset_path = first_payload["reset_path"]
    has_reset_path_prefix = first_reset_path.startswith("/password-reset#token=")
    assert has_reset_path_prefix
    first_encoded = first_reset_path.split("#token=", 1)[1]
    first_raw = unquote(first_encoded)
    assert bool(first_raw)
    first_grant_id = first_payload["grant_id"]
    first_expiry = datetime.fromisoformat(first_payload["expires_at"])
    expiry_seconds = (first_expiry - datetime.now(UTC)).total_seconds()
    assert 29 * 60 <= expiry_seconds <= 31 * 60

    second = client.post(
        f"/api/v1/admin/users/{target_id}/password-reset",
        headers=_headers(token, "reset-key-ignored-01"),
        json={"expected_auth_revision": 1},
    )
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload is not None
    second_raw = unquote(second_payload["reset_path"].split("#token=", 1)[1])
    tokens_are_distinct = first_raw != second_raw
    assert tokens_are_distinct

    with services.database.session() as session:
        first_grant = session.get(PasswordResetGrant, first_grant_id)
        second_grant = session.get(PasswordResetGrant, second_payload["grant_id"])
        assert first_grant is not None and first_grant.revoked_at is not None
        assert second_grant is not None
        first_digest = hashlib.sha256(first_raw.encode("ascii")).hexdigest()
        second_digest = hashlib.sha256(second_raw.encode("ascii")).hexdigest()
        stored_hashes = [first_grant.token_hash, second_grant.token_hash]
        assert first_digest in stored_hashes and second_digest in stored_hashes
        raw_token_persisted = any(raw in stored_hashes for raw in (first_raw, second_raw))
        assert raw_token_persisted is False
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0
        events = session.scalars(select(AuditEvent)).all()
        audit_text = repr(
            [
                (
                    event.path,
                    event.idempotency_key,
                    event.resource_kind,
                    event.resource_id,
                    event.metadata_json,
                )
                for event in events
            ]
        )
        reset_secret_in_audit = any(raw in audit_text for raw in (first_raw, second_raw))
        assert reset_secret_in_audit is False


def test_reset_redemption_is_one_use_redacted_and_does_not_authenticate(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    client = flask_app.test_client()
    admin_token = _api_token(services)
    target_id = _add_user(services, display_name="Password reset target")

    invalid_secret = "synthetic-invalid-reset-secret-987654"
    invalid = client.post(
        "/api/v1/auth/password-reset",
        json={"token": invalid_secret, "new_password": "long enough synthetic password"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert invalid.status_code == 400
    assert _error(invalid)["code"] == "invalid_reset"

    issue = client.post(
        f"/api/v1/admin/users/{target_id}/password-reset",
        headers=_headers(admin_token),
        json={"expected_auth_revision": 1},
    )
    assert issue.status_code == 200
    issue_payload = issue.get_json()
    assert issue_payload is not None
    raw_reset_token = unquote(issue_payload["reset_path"].split("#token=", 1)[1])

    redeemed = client.post(
        "/api/v1/auth/password-reset",
        json={"token": raw_reset_token, "new_password": _NEW_PASSWORD},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert redeemed.status_code == 204
    assert redeemed.headers.get("Set-Cookie") is None
    throttle_cleared_after_success = (
        "password-reset:127.0.0.1" not in services.login_throttle._states
    )
    assert throttle_cleared_after_success
    replay = client.post(
        "/api/v1/auth/password-reset",
        json={"token": raw_reset_token, "new_password": _NEW_PASSWORD},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert replay.status_code == 400
    assert _error(replay)["code"] == "invalid_reset"

    secret_marker = "synthetic-validation-secret-246810"
    invalid_payload = client.post(
        "/api/v1/auth/password-reset",
        json={
            "token": secret_marker,
            "new_password": "short",
            "extra_secret": secret_marker,
        },
    )
    assert invalid_payload.status_code == 422
    invalid_text = invalid_payload.get_data(as_text=True)
    assert secret_marker not in invalid_text
    assert '"short"' not in invalid_text

    with services.database.session() as session:
        credential = session.get(UserCredential, target_id)
        grant = session.get(PasswordResetGrant, issue_payload["grant_id"])
        assert credential is not None and credential.auth_revision == 2
        assert credential.password_hash is not None
        assert grant is not None and grant.consumed_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(BrowserAuthSession)
                .where(BrowserAuthSession.user_id == target_id)
            )
            == 0
        )


def test_reset_throttle_applies_to_loopback_and_never_uses_bearer_token_as_key(
    account_application: tuple[Flask, ApplicationServices],
) -> None:
    flask_app, services = account_application
    services.login_throttle = LoginThrottle(max_attempts=2, initial_block_seconds=30)
    client = flask_app.test_client()
    raw_reset_token = "synthetic-invalid-token-for-throttle"
    body = {"token": raw_reset_token, "new_password": _NEW_PASSWORD}

    first = client.post(
        "/api/v1/auth/password-reset",
        json=body,
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    second = client.post(
        "/api/v1/auth/password-reset",
        json=body,
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert first.status_code == 400 and second.status_code == 400

    blocked = client.post(
        "/api/v1/auth/password-reset",
        json=body,
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert blocked.status_code == 429
    error = _error(blocked)
    assert error["code"] == "password_reset_throttled"
    assert error["message"] == "Too many invalid reset attempts. Try again later."
    retry_after = int(blocked.headers["Retry-After"])
    assert retry_after > 0
    assert error["details"]["retry_after_seconds"] == retry_after
    assert services.login_throttle.retry_after("password-reset:127.0.0.1") > 0
    assert services.login_throttle.retry_after(raw_reset_token) == 0
