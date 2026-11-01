"""Administrator automation enrollment revalidates browser authority in-writer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from flask import Flask
from sqlalchemy import func, select

from pandrator.web.accounts.auth_contracts import ALL_SCOPES, MCP_BOOTSTRAP_SCOPES, PrincipalKind
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.accounts.execution_actor import UserActor, actor_from_principal
from pandrator.web.accounts.models import User
from pandrator.web.api import create_app
from pandrator.web.auth import AuthService, BootstrapTokenStore, Principal
from pandrator.web.automation_enrollment import (
    AUTOMATION_SCOPES,
    AutomationEnrollmentService,
    EnrollmentError,
)
from pandrator.web.database import Database
from pandrator.web.model_base import utcnow
from pandrator.web.models import AutomationClient, AutomationEnrollmentGrant
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.targets import (
    APPLICATION_SCOPES,
    MANAGER_RECOVERY_SCOPES,
    TargetProfile,
)
from tests.web_test_support import prepare_web_test_data_root

_TARGET_INSTANCE_ID = "synthetic-enrollment-target"
_CANONICAL_ORIGIN = "https://synthetic.example"
_REDIRECT_URI = "http://127.0.0.1:43123/callback"
_VERIFIER = "synthetic-pkce-verifier-0123456789-abcdefghijklmnopqrstuvwxyz"
_AUTHORITY_ERROR = "The approving account credential is no longer valid."


@dataclass
class EnrollmentCase:
    app: Flask
    database: Database
    auth: AuthService
    enrollment: AutomationEnrollmentService
    user_id: str
    auth_revision: int

    def browser_principal(self, scopes: object = ALL_SCOPES) -> Principal:
        issued = self.auth.browser_sessions.issue(
            self.user_id,
            self.auth_revision,
            subject=f"synthetic-enrollment-admin-{uuid4()}",
            principal_kind="owner_session",
            scopes=scopes,
        )
        snapshot = self.auth.browser_sessions.resolve(issued.raw_token)
        assert snapshot is not None
        return Principal(
            subject=snapshot.subject,
            kind=snapshot.kind,
            scopes=snapshot.scopes,
            token_id=None,
            network_zone="loopback",
            target_instance_id=_TARGET_INSTANCE_ID,
            client_id=snapshot.client_id,
            user_id=snapshot.user_id,
            auth_revision=snapshot.auth_revision,
            browser_session_id=snapshot.session_id,
        )


@pytest.fixture
def enrollment_case(tmp_path: Path) -> Iterator[EnrollmentCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    launch_token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    response = client.post("/api/v1/auth/bootstrap", json={"token": launch_token})
    assert response.status_code == 200, response.get_json()

    services = app.extensions["pandrator"]
    database = services["database"]
    auth = services["auth"]
    account = auth.initial_account()
    try:
        yield EnrollmentCase(
            app=app,
            database=database,
            auth=auth,
            enrollment=services["automation_enrollment"],
            user_id=account.user_id,
            auth_revision=account.auth_revision,
        )
    finally:
        database.dispose()


def _issue_code(
    case: EnrollmentCase,
    approved_by: Principal,
    *,
    client_id: str,
    scopes: tuple[str, ...] = ("app.read", "app.write"),
) -> str:
    return case.enrollment.issue_code(
        client_id=client_id,
        client_name="Synthetic enrollment client",
        redirect_uri=_REDIRECT_URI,
        scopes=scopes,
        code_challenge=create_s256_code_challenge(_VERIFIER),
        code_challenge_method="S256",
        expires_in_days=1,
        target_instance_id=_TARGET_INSTANCE_ID,
        canonical_origin=_CANONICAL_ORIGIN,
        approved_by=approved_by,
    )


def _assert_no_client_or_grant(case: EnrollmentCase, client_id: str) -> None:
    with case.database.session() as session:
        assert session.get(AutomationClient, client_id) is None
        grants = session.scalar(
            select(func.count())
            .select_from(AutomationEnrollmentGrant)
            .where(AutomationEnrollmentGrant.client_id == client_id)
        )
    assert grants == 0


def test_explicit_administrator_enrollment_exchanges_only_requested_scopes(
    enrollment_case: EnrollmentCase,
) -> None:
    case = enrollment_case
    approved_by = case.browser_principal()
    client_id = str(uuid4())
    selected_scopes = ("app.admin", "app.read", "app.write")
    actor = actor_from_principal(approved_by)
    assert isinstance(actor, UserActor)
    assert actor.browser_session_id == approved_by.browser_session_id

    code = _issue_code(case, approved_by, client_id=client_id, scopes=selected_scopes)
    with case.database.session() as session:
        grant = session.scalar(
            select(AutomationEnrollmentGrant).where(
                AutomationEnrollmentGrant.client_id == client_id
            )
        )
        client = session.get(AutomationClient, client_id)
        assert grant is not None and client is not None
        assert grant.code_hash == hashlib.sha256(code.encode("utf-8")).hexdigest()
        assert grant.code_hash != code
        assert client.allowed_scopes_json == sorted(selected_scopes)
        assert client.user_id == case.user_id
        assert client.user_auth_revision == case.auth_revision

    token = case.enrollment.exchange_code(
        code=code,
        client_id=client_id,
        redirect_uri=_REDIRECT_URI,
        code_verifier=_VERIFIER,
        target_instance_id=_TARGET_INSTANCE_ID,
        canonical_origin=_CANONICAL_ORIGIN,
    )
    assert token.record.scopes_json == sorted(selected_scopes)
    assert token.record.user_id == case.user_id
    assert token.record.user_auth_revision == case.auth_revision
    assert token.record.token_hash != token.raw_token
    assert case.auth.verify_api_token(token.raw_token)


def test_admin_scope_is_explicit_and_bootstrap_defaults_remain_read_only() -> None:
    assert "app.admin" in AUTOMATION_SCOPES
    assert "app.admin" in APPLICATION_SCOPES
    assert "app.admin" not in MCP_BOOTSTRAP_SCOPES
    assert "app.admin" not in MANAGER_RECOVERY_SCOPES

    default_profile = TargetProfile(
        name="synthetic-default",
        mode=TargetMode.EXTERNAL_HTTPS,
        application_origin="https://synthetic.example",
    )
    assert default_profile.requested_application_scopes == ("app.read",)
    admin_profile = TargetProfile(
        name="synthetic-admin",
        mode=TargetMode.EXTERNAL_HTTPS,
        application_origin="https://synthetic.example",
        requested_application_scopes=("app.admin", "app.read"),
    )
    assert set(admin_profile.requested_application_scopes) == {"app.admin", "app.read"}

    assert AutomationEnrollmentService.validate_scopes(["app.admin", "app.read"]) == frozenset(
        {"app.admin", "app.read"}
    )
    with pytest.raises(EnrollmentError) as empty:
        AutomationEnrollmentService.validate_scopes([])
    assert empty.value.code == "invalid_scope"
    with pytest.raises(EnrollmentError) as unknown:
        AutomationEnrollmentService.validate_scopes(["app.unknown"])
    assert unknown.value.code == "invalid_scope"


@pytest.mark.parametrize("change", ["revoked", "expired", "demoted", "epoch", "scopes"])
def test_stale_browser_authority_denies_before_client_or_grant_write(
    enrollment_case: EnrollmentCase, change: str
) -> None:
    case = enrollment_case
    approved_by = case.browser_principal()
    assert approved_by.browser_session_id is not None
    client_id = str(uuid4())

    with case.database.immediate_session() as session:
        if change == "demoted":
            user = session.get(User, case.user_id)
            assert user is not None
            user.is_instance_admin = False
        elif change == "epoch":
            credential = session.get(UserCredential, case.user_id)
            assert credential is not None
            credential.auth_revision += 1
        else:
            browser = session.get(BrowserAuthSession, approved_by.browser_session_id)
            assert browser is not None
            if change == "revoked":
                browser.revoked_at = utcnow()
            elif change == "expired":
                browser.expires_at = utcnow() - timedelta(seconds=1)
                browser.idle_expires_at = browser.expires_at
            else:
                browser.scopes_json = ["app.admin", "app.read"]

    with pytest.raises(EnrollmentError) as denied:
        _issue_code(case, approved_by, client_id=client_id)
    assert denied.value.code == "access_denied"
    assert str(denied.value) == _AUTHORITY_ERROR
    _assert_no_client_or_grant(case, client_id)


def test_missing_browser_transport_and_api_or_manager_principals_cannot_approve(
    enrollment_case: EnrollmentCase,
) -> None:
    case = enrollment_case
    browser = case.browser_principal()
    client_id = str(uuid4())
    # This malformed snapshot retains the real account ID/epoch and removes only its transport.
    missing_transport = replace(browser, browser_session_id=None)
    with pytest.raises(EnrollmentError) as missing:
        _issue_code(case, missing_transport, client_id=client_id)
    assert missing.value.code == "access_denied"
    assert str(missing.value) == _AUTHORITY_ERROR
    _assert_no_client_or_grant(case, client_id)

    non_browser_principals: tuple[tuple[PrincipalKind, frozenset[str]], ...] = (
        ("api_token", ALL_SCOPES),
        ("manager_bootstrap", MCP_BOOTSTRAP_SCOPES),
    )
    for principal_kind, scopes in non_browser_principals:
        _record, raw_token = case.auth.create_api_token(
            "synthetic non-browser enrollment credential",
            principal_kind=principal_kind,
            scopes=scopes,
            user_id=case.user_id,
            auth_revision=case.auth_revision,
        )
        principal = case.auth.resolve_api_token(
            raw_token,
            network_zone="loopback",
            target_instance_id=_TARGET_INSTANCE_ID,
        )
        assert principal is not None
        client_id = str(uuid4())
        with pytest.raises(EnrollmentError) as denied:
            _issue_code(case, principal, client_id=client_id)
        assert denied.value.code == "access_denied"
        _assert_no_client_or_grant(case, client_id)


def test_admin_cannot_delegate_a_scope_missing_from_its_browser_grant(
    enrollment_case: EnrollmentCase,
) -> None:
    case = enrollment_case
    approved_by = case.browser_principal(scopes={"app.admin", "app.read"})
    client_id = str(uuid4())
    with pytest.raises(EnrollmentError) as denied:
        _issue_code(
            case,
            approved_by,
            client_id=client_id,
            scopes=("app.admin", "app.read", "app.write"),
        )
    assert denied.value.code == "access_denied"
    assert str(denied.value) == _AUTHORITY_ERROR
    _assert_no_client_or_grant(case, client_id)


def test_private_register_remains_bound_to_an_administrator(
    enrollment_case: EnrollmentCase,
) -> None:
    case = enrollment_case
    account = case.auth.initial_account()
    commands = AccountCommands(case.database)
    member = commands.create_user(
        AccountActor(account.user_id, account.auth_revision, ALL_SCOPES),
        login_name="synthetic-member",
        display_name="Synthetic member",
    )
    client_id = str(uuid4())

    with pytest.raises(EnrollmentError) as denied:
        case.enrollment.register(
            client_id=client_id,
            name="Synthetic private client",
            redirect_uris=[_REDIRECT_URI],
            scopes=["app.read"],
            target_instance_id=_TARGET_INSTANCE_ID,
            canonical_origin=_CANONICAL_ORIGIN,
            created_by="synthetic-member",
            user_id=member.user_id,
            auth_revision=member.auth_revision,
        )
    assert denied.value.code == "access_denied"
    _assert_no_client_or_grant(case, client_id)
