"""Transport and credential regression tests for private account foundations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator
from unittest.mock import patch
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.commands import AccountActor, AccountCommandError, AccountCommands
from pandrator.web.accounts.passwords import mirror_initial_owner_password, verify_password
from pandrator.web.api import create_app
from pandrator.web.auth import (
    ALL_SCOPES,
    MCP_BOOTSTRAP_SCOPES,
    AuthService,
    BootstrapTokenStore,
    Principal,
)
from pandrator.web.automation_enrollment import EnrollmentError
from pandrator.web.database import Database
from pandrator.web.models import ApiToken, AutomationEnrollmentGrant, OwnerAccount, utcnow
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class AuthCase:
    app: Flask
    client: FlaskClient
    auth: AuthService
    database: Database
    bootstrap: BootstrapTokenStore
    csrf: str

    @property
    def actor(self) -> AccountActor:
        account = self.auth.initial_account()
        return AccountActor(account.user_id, account.auth_revision, ALL_SCOPES)


@pytest.fixture
def case(tmp_path: Path) -> Iterator[AuthCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    launch_token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    response = client.post("/api/v1/auth/bootstrap", json={"token": launch_token})
    assert response.status_code == 200, response.get_json()
    services = app.extensions["pandrator"]
    try:
        yield AuthCase(
            app,
            client,
            services["auth"],
            services["database"],
            bootstrap,
            response.get_json()["csrf_token"],
        )
    finally:
        services["database"].dispose()


def copied_cookie_client(case: AuthCase, cookie: str) -> FlaskClient:
    client = case.app.test_client()
    client.set_cookie("pandrator_session", cookie)
    return client


def current_cookie(case: AuthCase) -> str:
    cookie = case.client.get_cookie("pandrator_session")
    assert cookie is not None
    return cookie.value


def current_browser_principal(case: AuthCase) -> Principal:
    with case.client.session_transaction() as cookie:
        raw = cookie["auth_session"]
    captured = case.auth.browser_sessions.resolve(raw)
    assert captured is not None
    return Principal(
        subject=captured.subject,
        kind=captured.kind,
        scopes=captured.scopes,
        token_id=None,
        browser_session_id=captured.session_id,
        network_zone="loopback",
        target_instance_id="synthetic-instance",
        client_id=captured.client_id,
        user_id=captured.user_id,
        auth_revision=captured.auth_revision,
    )


def test_bootstrap_uses_an_opaque_revocable_user_credential(case: AuthCase) -> None:
    account = case.auth.initial_account()
    with case.client.session_transaction() as cookie:
        assert set(cookie) == {"authenticated", "csrf_token", "auth_session"}
        raw = cookie["auth_session"]
    with case.database.session() as session:
        record = session.scalar(select(BrowserAuthSession))
        assert record is not None
        assert record.user_id == account.user_id
        assert record.auth_revision == account.auth_revision
        assert record.token_hash != raw
    assert case.client.get("/api/v1/auth/tokens").status_code == 200


def test_logout_revokes_a_copied_cookie(case: AuthCase) -> None:
    copied = copied_cookie_client(case, current_cookie(case))
    assert copied.get("/api/v1/auth/tokens").status_code == 200
    assert (
        case.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": case.csrf}).status_code
        == 200
    )
    assert copied.get("/api/v1/sessions").status_code == 401
    status = copied.get("/api/v1/auth/status").get_json()
    assert status["authenticated"] is False
    assert status["csrf_token"] is None


def test_password_login_rotates_and_revokes_the_previous_cookie(case: AuthCase) -> None:
    password = "synthetic owner account password"
    case.auth.initialize_owner(password)
    previous = copied_cookie_client(case, current_cookie(case))
    response = case.client.post("/api/v1/auth/login", json={"password": password})
    assert response.status_code == 200
    assert previous.get("/api/v1/auth/tokens").status_code == 401
    assert case.client.get("/api/v1/auth/tokens").status_code == 200


def test_cli_password_replacement_invalidates_browser_api_and_reset_credentials(
    case: AuthCase,
) -> None:
    old_password = "synthetic first owner password"
    new_password = "synthetic replacement owner password"
    case.auth.initialize_owner(old_password)
    _token, raw = case.auth.create_api_token("synthetic reset credential")
    actor = case.actor
    commands = AccountCommands(case.database)
    grant = commands.issue_password_reset(
        actor, actor.user_id, expected_auth_revision=actor.auth_revision
    )
    copied = copied_cookie_client(case, current_cookie(case))
    case.auth.initialize_owner(new_password, replace=True)
    assert copied.get("/api/v1/auth/tokens").status_code == 401
    assert not case.auth.verify_api_token(raw)
    with pytest.raises(AccountCommandError, match="invalid or expired"):
        commands.redeem_password_reset(grant.raw_token, "synthetic reset replacement password")
    assert not case.auth.verify_password(old_password)
    assert case.auth.verify_password(new_password)


def test_legacy_signed_cookie_has_no_implicit_owner_or_scope_fallback(case: AuthCase) -> None:
    client = case.app.test_client()
    with client.session_transaction() as cookie:
        cookie["authenticated"] = True
        cookie["csrf_token"] = "synthetic-old-csrf"
        cookie["principal_subject"] = "owner"
        cookie["principal_scopes"] = list(ALL_SCOPES)
        cookie["principal_kind"] = "unknown"
    assert client.get("/api/v1/auth/tokens").status_code == 401
    assert client.get("/api/v1/auth/status").get_json()["authenticated"] is False


def test_token_subject_and_all_scopes_do_not_make_a_user_an_administrator(case: AuthCase) -> None:
    member = AccountCommands(case.database).create_user(
        case.actor, login_name="translator", display_name="Translator"
    )
    _record, raw = case.auth.create_api_token(
        "synthetic member token",
        scopes=["app.read"],
        subject="owner",
        user_id=member.user_id,
        auth_revision=member.auth_revision,
    )
    principal = case.auth.resolve_api_token(raw, network_zone="loopback", target_instance_id=None)
    assert principal is not None and principal.user_id == member.user_id
    assert principal.subject == "owner"
    with pytest.raises(PermissionError):
        case.auth.require_administrator(replace(principal, scopes=ALL_SCOPES))
    with case.database.session() as session:
        count = session.scalar(select(func.count()).select_from(ApiToken))
    with pytest.raises(PermissionError):
        case.auth.create_api_token(
            "synthetic invalid administrator",
            user_id=member.user_id,
            auth_revision=member.auth_revision,
        )
    with case.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ApiToken)) == count


def test_named_account_is_not_admitted_to_unfiltered_solo_routes(case: AuthCase) -> None:
    commands = AccountCommands(case.database)
    member = commands.create_user(case.actor, login_name="member", display_name="Member")
    grant = commands.issue_password_reset(
        case.actor, member.user_id, expected_auth_revision=member.auth_revision
    )
    password = "synthetic named member password"
    commands.redeem_password_reset(grant.raw_token, password)
    response = case.app.test_client().post("/api/v1/auth/login", json={"password": password})
    assert response.status_code == 401
    account = commands.list_users(case.actor)
    current_member = next(item for item in account if item.user_id == member.user_id)
    _record, raw = case.auth.create_api_token(
        "synthetic closed named-account transport",
        scopes=["app.read"],
        user_id=member.user_id,
        auth_revision=current_member.auth_revision,
    )
    assert (
        case.app.test_client()
        .get("/api/v1/sessions", headers={"Authorization": f"Bearer {raw}"})
        .status_code
        == 401
    )


def test_malformed_password_hash_is_a_generic_authentication_failure(case: AuthCase) -> None:
    with case.database.immediate_session() as session:
        credential = session.get(UserCredential, case.actor.user_id)
        assert credential is not None
        credential.password_hash = "synthetic malformed encoded hash"
    response = case.app.test_client().post(
        "/api/v1/auth/login", json={"password": "synthetic supplied password"}
    )
    assert response.status_code == 401
    assert "synthetic" not in str(response.get_json())


def test_successful_rehash_preserves_epoch_and_legacy_projection(case: AuthCase) -> None:
    password = "synthetic legacy owner password"
    old_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    actor = case.actor
    with case.database.immediate_session() as session:
        credential = session.get(UserCredential, actor.user_id)
        assert credential is not None
        credential.password_hash = old_hash
        mirror_initial_owner_password(
            session, user_id=actor.user_id, encoded=old_hash, updated_at=utcnow()
        )
    account = case.auth.authenticate_initial_password(password)
    assert account is not None and account.auth_revision == actor.auth_revision
    with case.database.session() as session:
        credential = session.get(UserCredential, actor.user_id)
        projection = session.get(OwnerAccount, 1)
        assert credential is not None and projection is not None
        assert credential.password_hash != old_hash
        assert credential.password_hash == projection.password_hash
        encoded = credential.password_hash
        assert encoded is not None
        assert verify_password(encoded, password)


def test_concurrent_password_change_cannot_be_overwritten_by_successful_verification(
    case: AuthCase,
) -> None:
    password = "synthetic captured owner password"
    case.auth.initialize_owner(password)
    actor = case.actor

    def change_after_verification(encoded: str, candidate: str) -> bool:
        verified = verify_password(encoded, candidate)
        with case.database.immediate_session() as session:
            credential = session.get(UserCredential, actor.user_id)
            assert credential is not None
            credential.auth_revision += 1
            credential.password_hash = "synthetic concurrent replacement"
        return verified

    with patch("pandrator.web.auth.verify_password", side_effect=change_after_verification):
        assert case.auth.authenticate_initial_password(password) is None
    with case.database.session() as session:
        credential = session.get(UserCredential, actor.user_id)
        assert credential is not None
        assert credential.password_hash == "synthetic concurrent replacement"


def test_manager_bootstrap_cannot_receive_administrator_scope(
    case: AuthCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_path = tmp_path / "manager-test-secret"
    credential_path.write_text("synthetic-local-manager-secret", encoding="utf-8")
    monkeypatch.setenv("PANDRATOR_MANAGER_CREDENTIAL", str(credential_path))
    monkeypatch.setenv("PANDRATOR_MCP_BOOTSTRAP_EXTRA_SCOPES", "app.admin")
    manager = case.app.test_client()
    response = manager.post(
        "/api/v1/auth/manager-bootstrap",
        json={"scopes": sorted(MCP_BOOTSTRAP_SCOPES | {"app.admin"})},
        headers={"Authorization": "Bearer synthetic-local-manager-secret"},
    )
    assert response.status_code == 200, response.get_json()
    grant = response.get_json()
    assert "app.admin" not in grant["scopes"]
    assert manager.post("/api/v1/auth/bootstrap", json={"token": grant["token"]}).status_code == 200
    assert manager.get("/api/v1/auth/tokens").status_code == 403


def test_a_bound_launch_grant_is_fenced_by_password_replacement(case: AuthCase) -> None:
    case.auth.initialize_owner("synthetic initial owner password")
    actor = case.actor
    token = case.bootstrap.issue(user_id=actor.user_id, auth_revision=actor.auth_revision)
    case.auth.initialize_owner("synthetic changed owner password", replace=True)
    response = case.app.test_client().post("/api/v1/auth/bootstrap", json={"token": token})
    assert response.status_code == 401


def test_narrowing_client_approval_fences_old_tokens_and_unexchanged_codes(case: AuthCase) -> None:
    enrollment = case.app.extensions["pandrator"]["automation_enrollment"]
    actor = case.actor
    client_id = str(uuid4())
    binding = {
        "client_id": client_id,
        "target_instance_id": "synthetic-instance",
        "canonical_origin": "https://synthetic.example",
    }
    registration = {
        **binding,
        "name": "Synthetic client",
        "redirect_uris": ["http://127.0.0.1:43123/callback"],
        "created_by": "owner",
        "user_id": actor.user_id,
        "auth_revision": actor.auth_revision,
    }
    enrollment.register(**registration, scopes=["app.read", "app.write"])
    verifier = "a" * 64
    code = enrollment.issue_code(
        **binding,
        client_name="Synthetic client",
        redirect_uri=registration["redirect_uris"][0],
        scopes=["app.read", "app.write"],
        code_challenge=create_s256_code_challenge(verifier),
        code_challenge_method="S256",
        expires_in_days=1,
        approved_by=current_browser_principal(case),
    )
    _token, raw = case.auth.create_api_token(
        "synthetic client credential",
        client_id=client_id,
        scopes=["app.read", "app.write"],
        user_id=actor.user_id,
        auth_revision=actor.auth_revision,
    )
    _other, unrelated = case.auth.create_api_token(
        "synthetic unrelated credential", scopes=["app.read"]
    )
    assert case.auth.verify_api_token(raw)
    enrollment.register(**registration, scopes=["app.read"])
    assert not case.auth.verify_api_token(raw)
    assert case.auth.verify_api_token(unrelated)
    with pytest.raises(EnrollmentError) as error:
        enrollment.exchange_code(
            **binding,
            code=code,
            redirect_uri=registration["redirect_uris"][0],
            code_verifier=verifier,
        )
    assert error.value.code == "invalid_grant"


def test_enrollment_code_expiry_is_checked_after_writer_admission(case: AuthCase) -> None:
    # Bearer tokens resolve in a read snapshot and take no writer; their expiry is
    # covered in test_routine_request_writes.py.
    start = datetime(2035, 1, 1, tzinfo=UTC)
    clock = [start]
    enrollment = case.app.extensions["pandrator"]["automation_enrollment"]
    client_id = str(uuid4())
    verifier = "a" * 64
    redirect_uri = "http://127.0.0.1:43123/callback"
    code = enrollment.issue_code(
        client_id=client_id,
        client_name="Synthetic expiring client",
        redirect_uri=redirect_uri,
        scopes=["app.read"],
        code_challenge=create_s256_code_challenge(verifier),
        code_challenge_method="S256",
        expires_in_days=1,
        target_instance_id="synthetic-instance",
        canonical_origin="https://synthetic.example",
        approved_by=current_browser_principal(case),
    )
    with case.database.immediate_session() as session:
        grant = session.scalar(
            select(AutomationEnrollmentGrant).where(
                AutomationEnrollmentGrant.client_id == client_id
            )
        )
        assert grant is not None
        grant.expires_at = start + timedelta(seconds=1)
    resolve = lambda: enrollment.exchange_code(  # noqa: E731
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        target_instance_id="synthetic-instance",
        canonical_origin="https://synthetic.example",
    )
    clock_target = "pandrator.web.automation_enrollment.utcnow"

    original_writer = case.database.immediate_session

    @contextmanager
    def delayed_admission():
        with original_writer() as session:
            clock[0] = start + timedelta(seconds=2)
            yield session

    with (
        patch(clock_target, side_effect=lambda: clock[0]),
        patch.object(case.database, "immediate_session", delayed_admission),
    ):
        with pytest.raises(EnrollmentError) as error:
            resolve()
        assert error.value.code == "invalid_grant"
