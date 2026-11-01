"""Opt-in named login, safe Manager launch and independent instance roles."""

from __future__ import annotations

import pytest
from requests import Response
from sqlalchemy import func, select

from pandrator.web.accounts import installation_mode
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.commands import AccountActor, AccountCommandError, AccountCommands
from pandrator.web.auth import ALL_SCOPES, MCP_BOOTSTRAP_SCOPES
from pandrator.web.models import AppSetting, AuditEvent, User
from pandrator_manager.api.app import _browser_handoff_url
from tests.test_account_authentication_integration import case as case  # noqa: F401

PASSWORD = "disposable test password for named login"


def new_account(case, login="alice", role="user"):
    commands = AccountCommands(case.database)
    account = commands.create_user(case.actor, login_name=login, display_name=login.title(), role=role)
    grant = commands.issue_password_reset(case.actor, account.user_id, expected_auth_revision=account.auth_revision)
    commands.redeem_password_reset(grant.raw_token, PASSWORD)
    return account


def enable(case, monkeypatch):
    monkeypatch.setattr(installation_mode, "COLLABORATION_AVAILABLE", True)
    case.auth.initialize_owner(PASSWORD)
    response = case.client.post(
        "/api/v1/admin/installation/collaboration", json={"expected_revision": 0},
        headers={"X-CSRF-Token": case.csrf},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_solo_defaults_keep_named_login_and_workspace_routes_closed(case):
    account = new_account(case)
    status = case.client.get("/api/v1/auth/status").get_json()
    assert status["installation"] == {
        "mode": "solo",
        "revision": 0,
        "collaboration_available": True,
        "collaboration_enabled": False,
    }
    before = case.client.get("/api/v1/admin/users").get_json()
    assert before["named_user_access"] is False
    client = case.app.test_client()
    assert (
        client.post(
            "/api/v1/auth/login", json={"login_name": "alice", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert not case.auth.solo_access_allowed(account.user_id, account.auth_revision)
    assert case.client.get("/api/v1/workspaces").status_code == 409
    rejected = case.client.post(
        "/api/v1/admin/installation/collaboration",
        json={"expected_revision": 0},
        headers={"X-CSRF-Token": case.csrf},
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "administrator_password_required"
    with case.database.snapshot_session() as session:
        assert session.get(AppSetting, installation_mode.INSTALLATION_POLICY_KEY) is None


def test_closed_build_rejects_opt_in_even_with_administrator_password(case, monkeypatch):
    monkeypatch.setattr(installation_mode, "COLLABORATION_AVAILABLE", False)
    case.auth.initialize_owner(PASSWORD)
    response = case.client.post(
        "/api/v1/admin/installation/collaboration",
        json={"expected_revision": 0},
        headers={"X-CSRF-Token": case.csrf},
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "collaboration_not_available"
    with case.database.snapshot_session() as session:
        assert session.get(AppSetting, installation_mode.INSTALLATION_POLICY_KEY) is None


def test_enable_requires_password_and_preserves_private_data(case, monkeypatch):
    monkeypatch.setattr(installation_mode, "COLLABORATION_AVAILABLE", True)
    headers = {"X-CSRF-Token": case.csrf}
    assert (
        case.client.post(
            "/api/v1/admin/installation/collaboration", json={"expected_revision": 0}
        ).status_code
        == 403
    )
    response = case.client.post("/api/v1/admin/installation/collaboration", json={"expected_revision": 0}, headers=headers)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "administrator_password_required"
    state = enable(case, monkeypatch)
    assert state["collaboration_enabled"] is True and state["revision"] == 1
    assert case.client.post("/api/v1/admin/installation/collaboration", json={"expected_revision": 0}, headers=headers).status_code == 409
    assert case.client.post("/api/v1/admin/installation/collaboration", json={"expected_revision": 1}, headers=headers).status_code == 200
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "installation.collaboration.enable")) == 1


def test_installation_mode_uses_the_callers_snapshot_during_activation(case, monkeypatch):
    with case.database.snapshot_session() as snapshot:
        assert not case.auth.collaboration_enabled(db_session=snapshot)
        enable(case, monkeypatch)
        assert case.auth.collaboration_enabled()
        assert not case.auth.collaboration_enabled(db_session=snapshot)
        assert snapshot.is_active
    assert case.auth.collaboration_enabled()


def test_named_login_has_user_scopes_and_reset_revokes_cookie(case, monkeypatch):
    account = new_account(case)
    enable(case, monkeypatch)
    client = case.app.test_client()
    for login in ("unknown", ""):
        response = client.post("/api/v1/auth/login", json={"login_name": login or "missing", "password": PASSWORD})
        assert response.status_code == 401
        assert response.get_json()["error"]["code"] == "invalid_credentials"
    response = client.post("/api/v1/auth/login", json={"login_name": " ALIce ", "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    status = client.get("/api/v1/auth/status").get_json()
    principal = status["principal"]
    assert principal["user_id"] == account.user_id
    assert principal["role"] == "user"
    assert "app.workspaces.enroll" in principal["scopes"]
    assert not set(principal["scopes"]) & {"app.admin", "manager.read", "manager.runtime", "manager.mutate"}
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.get("/api/v1/workspaces").status_code == 200
    commands = AccountCommands(case.database)
    with case.database.snapshot_session() as session:
        revision = session.get(UserCredential, account.user_id).auth_revision
    reset = commands.issue_password_reset(case.actor, account.user_id, expected_auth_revision=revision)
    commands.redeem_password_reset(reset.raw_token, PASSWORD + " changed")
    assert client.get("/api/v1/sessions").status_code == 401
    assert client.get("/api/v1/auth/status").get_json()["principal"] is None


def test_stored_opt_in_cannot_bypass_closed_build_gate(case, monkeypatch):
    monkeypatch.setattr(installation_mode, "COLLABORATION_AVAILABLE", False)
    account = new_account(case)
    with case.database.immediate_session() as session:
        session.add(AppSetting(key=installation_mode.INSTALLATION_POLICY_KEY, revision=1, value_json={"schema_version": 1, "mode": "collaboration"}))
    assert not case.auth.collaboration_enabled()
    assert not case.auth.solo_access_allowed(account.user_id, account.auth_revision)
    assert case.app.test_client().post("/api/v1/auth/login", json={"login_name": "alice", "password": PASSWORD}).status_code == 401
    assert "app.workspaces.enroll" not in MCP_BOOTSTRAP_SCOPES


def test_initial_administrator_can_retire_after_collaboration_and_replacement(case, monkeypatch):
    replacement = new_account(case, "other-admin", "administrator")
    initial = case.auth.initial_account()
    enable(case, monkeypatch)
    response = case.client.patch(
        f"/api/v1/admin/users/{initial.user_id}",
        json={"expected_auth_revision": initial.auth_revision, "is_active": False},
        headers={"X-CSRF-Token": case.csrf, "Idempotency-Key": "retire-initial-admin"},
    )
    assert response.status_code == 200, response.get_json()
    assert case.auth.initialized() is True
    client = case.app.test_client()
    assert client.post("/api/v1/auth/login", json={"login_name": "other-admin", "password": PASSWORD}).status_code == 200
    status = client.get("/api/v1/auth/status").get_json()
    assert status["principal"]["user_id"] == replacement.user_id
    assert set(status["principal"]["scopes"]) == ALL_SCOPES
    assert client.get("/api/v1/admin/users").get_json()["named_user_access"] is True
    commands = AccountCommands(case.database)
    revision = status["principal"]["auth_revision"]
    with pytest.raises(AccountCommandError, match="Another active Administrator"):
        commands.update_user(AccountActor(replacement.user_id, revision, ALL_SCOPES), replacement.user_id, expected_auth_revision=revision, is_active=False)


def test_manager_plain_handoff_preserves_existing_browser_identity():
    response = Response()
    response.status_code = 200
    response._content = b'{"login_required": true}'
    assert _browser_handoff_url(response, "http://127.0.0.1:8010") == "http://127.0.0.1:8010/"


def test_public_login_payload_is_bounded_and_password_errors_omit_input(case):
    client = case.app.test_client()
    sentinel = "private-password-sentinel-" * 50
    invalid = client.post("/api/v1/auth/login", json={"password": sentinel})
    assert invalid.status_code == 422
    assert sentinel not in invalid.get_data(as_text=True)
    oversized = client.post("/api/v1/auth/login", json={"password": "x" * 9000})
    assert oversized.status_code == 413
