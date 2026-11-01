"""Credential resource capture and consent stay atomic and cannot expand silently."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import delete, func, select

from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.credential_resources import (
    CredentialOwner,
    CredentialResources,
    CredentialResourcesDenied,
    replace_client_resources,
    resources_for_owner,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import AutomationCodeProjectGrant
from pandrator.web.auth import AuthService, Principal
from pandrator.web.automation_enrollment import AutomationEnrollmentService, EnrollmentError
from pandrator.web.database import Database
from pandrator.web.model_base import new_id
from pandrator.web.models import ApiToken, AutomationClient, AutomationEnrollmentGrant
from pandrator.web.projects.models import Project
from tests.web_test_support import prepare_web_test_data_root

TARGET = "synthetic-resource-target"
ORIGIN = "https://synthetic.example"
REDIRECT = "http://127.0.0.1:43123/callback"
VERIFIER = "synthetic-resource-verifier-0123456789-abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class IssuanceCase:
    database: Database
    auth: AuthService
    enrollment: AutomationEnrollmentService
    user_id: str
    personal_id: str
    team_id: str
    project_ids: tuple[str, str]

    def resources(self, owner: CredentialOwner, owner_id: str) -> CredentialResources:
        with self.database.snapshot_session() as session:
            return resources_for_owner(session, owner, owner_id)

    def principal(self, *, client_id: str | None = None) -> Principal:
        issued = self.auth.browser_sessions.issue(
            self.user_id,
            1,
            subject="synthetic-consenting-admin",
            principal_kind="owner_session",
            scopes=ALL_SCOPES,
            client_id=client_id,
        )
        snapshot = self.auth.browser_sessions.resolve(issued.raw_token)
        assert snapshot is not None
        return Principal(
            subject=snapshot.subject,
            kind=snapshot.kind,
            scopes=snapshot.scopes,
            token_id=None,
            network_zone="loopback",
            target_instance_id=TARGET,
            client_id=snapshot.client_id,
            user_id=snapshot.user_id,
            auth_revision=snapshot.auth_revision,
            browser_session_id=snapshot.session_id,
        )


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append("network")
        raise AssertionError("Credential issuance tests must remain offline.")

    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)
    yield
    assert attempts == []


@pytest.fixture
def case(tmp_path: Path) -> Iterator[IssuanceCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    auth = AuthService(database)
    account = auth.initial_account()
    with database.immediate_session() as session:
        personal_id = session.scalar(
            select(Workspace.id).where(Workspace.personal_owner_user_id == account.user_id)
        )
        assert personal_id is not None
        team = Workspace(kind="team", name="Synthetic consent team", state="active")
        session.add(team)
        session.flush()
        session.add(
            WorkspaceMembership(workspace_id=team.id, user_id=account.user_id, role="owner")
        )
        projects = (
            Project(workspace_id=team.id, name="First project"),
            Project(workspace_id=team.id, name="Second project"),
        )
        session.add_all(projects)
        session.flush()
        team_id = team.id
        project_ids = (projects[0].id, projects[1].id)
    try:
        yield IssuanceCase(
            database,
            auth,
            AutomationEnrollmentService(database, auth),
            account.user_id,
            personal_id,
            team_id,
            project_ids,
        )
    finally:
        database.dispose()


def _register(
    case: IssuanceCase,
    client_id: str,
    resources: CredentialResources,
    *,
    scopes: tuple[str, ...] = ("app.read",),
) -> AutomationClient:
    return case.enrollment.register(
        client_id=client_id,
        name="Synthetic approved client",
        redirect_uris=[REDIRECT],
        scopes=scopes,
        target_instance_id=TARGET,
        canonical_origin=ORIGIN,
        created_by="synthetic-consenting-admin",
        user_id=case.user_id,
        auth_revision=1,
        resource_grants=resources,
    )


def _code(
    case: IssuanceCase,
    client_id: str,
    resources: CredentialResources,
    *,
    principal: Principal | None = None,
    scopes: tuple[str, ...] = ("app.read",),
) -> tuple[str, str]:
    raw = case.enrollment.issue_code(
        client_id=client_id,
        client_name="Synthetic approved client",
        redirect_uri=REDIRECT,
        scopes=scopes,
        code_challenge=create_s256_code_challenge(VERIFIER),
        code_challenge_method="S256",
        expires_in_days=1,
        target_instance_id=TARGET,
        canonical_origin=ORIGIN,
        approved_by=principal if principal is not None else case.principal(),
        resource_grants=resources,
    )
    with case.database.snapshot_session() as session:
        grant_id = session.scalar(
            select(AutomationEnrollmentGrant.id).where(
                AutomationEnrollmentGrant.client_id == client_id,
                AutomationEnrollmentGrant.code_hash == hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        assert grant_id is not None
    return raw, grant_id


def _exchange(case: IssuanceCase, client_id: str, code: str, *, verifier: str = VERIFIER):
    return case.enrollment.exchange_code(
        code=code,
        client_id=client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        target_instance_id=TARGET,
        canonical_origin=ORIGIN,
    )


def _counts(case: IssuanceCase) -> tuple[int, ...]:
    with case.database.snapshot_session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in (ApiToken, AutomationClient, AutomationEnrollmentGrant, BrowserAuthSession)
        )


@pytest.mark.parametrize(
    "values",
    [
        {"workspace_ids": {"a"}},
        {"project_ids": ["a"]},
        {"workspace_ids": frozenset({True})},
        {"project_ids": frozenset({""})},
        {"project_ids": frozenset({" padded"})},
        {"workspace_ids": frozenset({"x" * 81})},
        {"project_ids": frozenset(str(index) for index in range(1001))},
    ],
)
def test_resource_input_rejects_untyped_or_malformed_selection(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CredentialResources(**values)


def test_in_session_token_and_grants_rollback_with_the_callers_unit(case: IssuanceCase) -> None:
    selection = CredentialResources(project_ids=frozenset({case.project_ids[0]}))
    before = _counts(case)
    token_id = ""
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with case.database.immediate_session() as session:
            token, _raw = case.auth.create_api_token_in_session(
                session,
                "Synthetic rollback token",
                scopes=("app.read",),
                user_id=case.user_id,
                auth_revision=1,
                resource_grants=selection,
            )
            token_id = token.id
            assert resources_for_owner(session, CredentialOwner.TOKEN, token_id) == selection
            raise RuntimeError("synthetic rollback")
    assert token_id
    assert _counts(case) == before
    assert case.resources(CredentialOwner.TOKEN, token_id) == CredentialResources()


@pytest.mark.parametrize("operation", ["token", "client", "code", "manager_browser"])
def test_invalid_resource_selection_creates_no_partial_credential(
    case: IssuanceCase, operation: str
) -> None:
    # Construct the real approving transport before taking the database snapshot.
    principal = case.principal()
    client_id = str(uuid4())
    invalid = CredentialResources(workspace_ids=frozenset({"unavailable-workspace"}))
    before = _counts(case)
    if operation == "token":
        with pytest.raises(CredentialResourcesDenied):
            case.auth.create_api_token(
                "Rejected token", user_id=case.user_id, auth_revision=1, resource_grants=invalid
            )
    elif operation == "client":
        with pytest.raises(EnrollmentError) as rejected:
            _register(case, client_id, invalid)
        assert (rejected.value.code, rejected.value.status_code) == ("access_denied", 403)
    elif operation == "code":
        with pytest.raises(EnrollmentError) as rejected:
            _code(case, client_id, invalid, principal=principal)
        assert (rejected.value.code, rejected.value.status_code) == ("access_denied", 403)
    else:
        with pytest.raises(ValueError, match="resource approval"):
            case.auth.browser_sessions.issue(
                case.user_id,
                1,
                subject="Rejected Manager browser",
                principal_kind="owner_session",
                scopes=ALL_SCOPES,
                client_id="pandrator-manager-browser",
                resource_grants=CredentialResources(workspace_ids=frozenset({case.team_id})),
            )
    assert _counts(case) == before
    assert case.resources(CredentialOwner.CLIENT, client_id) == CredentialResources()


def test_code_capture_survives_broader_client_approval_without_expanding_token(
    case: IssuanceCase,
) -> None:
    client_id = str(uuid4())
    captured = CredentialResources(project_ids=frozenset({case.project_ids[0]}))
    code, grant_id = _code(case, client_id, captured)
    assert case.resources(CredentialOwner.CODE, grant_id) == captured
    # Keep the pending code live to isolate capture; normal approval revocation
    # is exercised separately below rather than masking a missing snapshot.
    with case.database.immediate_session() as session:
        replace_client_resources(
            session,
            client_id,
            CredentialResources(workspace_ids=frozenset({case.team_id, case.personal_id})),
        )
        client = session.get(AutomationClient, client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read", "app.write"]

    with pytest.raises(EnrollmentError) as rejected:
        _exchange(case, client_id, code, verifier="wrong-verifier")
    assert rejected.value.code == "invalid_grant"
    with case.database.snapshot_session() as session:
        grant = session.get(AutomationEnrollmentGrant, grant_id)
        assert grant is not None and grant.consumed_at is None

    result = _exchange(case, client_id, code)
    assert result.record.scopes_json == ["app.read"]
    assert case.resources(CredentialOwner.TOKEN, result.record.id) == captured
    assert case.resources(CredentialOwner.CODE, grant_id) == captured
    resolved = case.auth.resolve_api_token(
        result.raw_token,
        network_zone="loopback",
        target_instance_id=TARGET,
        canonical_origin=ORIGIN,
    )
    assert resolved is not None and resolved.scopes == frozenset({"app.read"})
    before = _counts(case)
    with pytest.raises(EnrollmentError) as rejected:
        _exchange(case, client_id, code)
    assert rejected.value.code == "invalid_grant"
    assert _counts(case) == before


@pytest.mark.parametrize("cause", ["narrowed_client", "lost_user_role"])
def test_exchange_rejects_unavailable_captured_resources_without_consuming_code(
    case: IssuanceCase, cause: str
) -> None:
    client_id = str(uuid4())
    captured = CredentialResources(workspace_ids=frozenset({case.team_id}))
    code, grant_id = _code(case, client_id, captured)
    with case.database.immediate_session() as session:
        if cause == "narrowed_client":
            replace_client_resources(session, client_id, CredentialResources())
        else:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team_id,
                    WorkspaceMembership.user_id == case.user_id,
                )
            )
    before = _counts(case)
    with pytest.raises(EnrollmentError) as rejected:
        _exchange(case, client_id, code)
    assert (rejected.value.code, rejected.value.status_code) == ("invalid_grant", 400)
    assert _counts(case) == before
    with case.database.snapshot_session() as session:
        grant = session.get(AutomationEnrollmentGrant, grant_id)
        assert grant is not None and grant.consumed_at is None


@pytest.mark.parametrize("empty_at_issue", [True, False])
def test_empty_or_deleted_code_resources_never_fall_back_to_personal_scope(
    case: IssuanceCase, empty_at_issue: bool
) -> None:
    client_id = str(uuid4())
    selected = (
        CredentialResources()
        if empty_at_issue
        else CredentialResources(project_ids=frozenset({case.project_ids[0]}))
    )
    code, grant_id = _code(case, client_id, selected)
    if not empty_at_issue:
        with case.database.immediate_session() as session:
            session.execute(
                delete(AutomationCodeProjectGrant).where(
                    AutomationCodeProjectGrant.code_grant_id == grant_id
                )
            )
    result = _exchange(case, client_id, code)
    assert case.resources(CredentialOwner.TOKEN, result.record.id) == CredentialResources()


@pytest.mark.parametrize("change", ["narrow", "expand", "scopes"])
def test_approval_change_revokes_existing_tokens_and_pending_codes_atomically(
    case: IssuanceCase, change: str
) -> None:
    client_id = str(uuid4())
    workspace = CredentialResources(workspace_ids=frozenset({case.team_id}))
    project = CredentialResources(project_ids=frozenset({case.project_ids[0]}))
    old, new = (workspace, project) if change == "narrow" else (project, workspace)
    if change == "scopes":
        new = old
    code, grant_id = _code(case, client_id, old)
    token, raw = case.auth.create_api_token(
        "Previously approved token",
        user_id=case.user_id,
        auth_revision=1,
        scopes=("app.read",),
        client_id=client_id,
        target_instance_id=TARGET,
        canonical_origin=ORIGIN,
    )
    assert case.resources(CredentialOwner.TOKEN, token.id) == old
    _register(
        case,
        client_id,
        new,
        scopes=("app.read", "app.write") if change == "scopes" else ("app.read",),
    )
    with case.database.snapshot_session() as session:
        stored_token = session.get(ApiToken, token.id)
        grant = session.get(AutomationEnrollmentGrant, grant_id)
        assert stored_token is not None and stored_token.revoked_at is not None
        assert grant is not None and grant.consumed_at is not None
    assert case.resources(CredentialOwner.CLIENT, client_id) == new
    assert case.resources(CredentialOwner.TOKEN, token.id) == old
    assert case.resources(CredentialOwner.CODE, grant_id) == old
    assert (
        case.auth.resolve_api_token(
            raw, network_zone="loopback", target_instance_id=TARGET, canonical_origin=ORIGIN
        )
        is None
    )
    with pytest.raises(EnrollmentError) as rejected:
        _exchange(case, client_id, code)
    assert rejected.value.code == "invalid_grant"


def test_rotation_keeps_each_codes_captured_set_and_revokes_previous_token(
    case: IssuanceCase,
) -> None:
    client_id = str(uuid4())
    selected = CredentialResources(project_ids=frozenset({case.project_ids[0]}))
    first_code, _first_id = _code(case, client_id, selected)
    first = _exchange(case, client_id, first_code)
    second_code, _second_id = _code(case, client_id, selected)
    second = _exchange(case, client_id, second_code)
    assert first.record.id != second.record.id
    assert case.resources(CredentialOwner.TOKEN, second.record.id) == selected
    assert (
        case.auth.resolve_api_token(
            first.raw_token,
            network_zone="loopback",
            target_instance_id=TARGET,
            canonical_origin=ORIGIN,
        )
        is None
    )
    assert (
        case.auth.resolve_api_token(
            second.raw_token,
            network_zone="loopback",
            target_instance_id=TARGET,
            canonical_origin=ORIGIN,
        )
        is not None
    )


def test_manager_browser_cannot_approve_team_beyond_its_own_snapshot(case: IssuanceCase) -> None:
    manager = case.principal(client_id="pandrator-manager-browser")
    assert manager.has_scope("app.admin")
    team = CredentialResources(workspace_ids=frozenset({case.team_id}))
    client_id = str(uuid4())
    before = _counts(case)
    with pytest.raises(EnrollmentError) as rejected:
        _code(case, client_id, team, principal=manager)
    assert (rejected.value.code, rejected.value.status_code) == ("access_denied", 403)
    assert _counts(case) == before
    assert case.resources(CredentialOwner.CLIENT, client_id) == CredentialResources()

    # A normal interactive browser proves this user's real team-owner role.
    _raw, grant_id = _code(case, client_id, team)
    assert case.resources(CredentialOwner.CODE, grant_id) == team
    with case.database.snapshot_session() as session:
        assert session.get(AutomationClient, "pandrator-manager-browser") is None


def test_real_client_browser_captures_approval_and_cannot_expand_with_later_approval(
    case: IssuanceCase,
) -> None:
    client_id = str(uuid4())
    selected = CredentialResources(project_ids=frozenset({case.project_ids[0]}))
    _register(case, client_id, selected)
    first = case.auth.browser_sessions.issue(
        case.user_id,
        1,
        subject="Bound automation browser",
        principal_kind="automation_client",
        scopes=("app.read",),
        client_id=client_id,
    )
    with case.database.immediate_session() as session:
        replace_client_resources(
            session, client_id, CredentialResources(workspace_ids=frozenset({case.team_id}))
        )
    assert case.auth.browser_sessions.resolve(first.raw_token) is not None
    assert case.resources(CredentialOwner.BROWSER, first.record_id) == selected
    empty = case.auth.browser_sessions.issue(
        case.user_id,
        1,
        subject="Explicitly empty automation browser",
        principal_kind="automation_client",
        scopes=("app.read",),
        client_id=client_id,
        resource_grants=CredentialResources(),
    )
    assert case.resources(CredentialOwner.BROWSER, empty.record_id) == CredentialResources()
    with pytest.raises(ValueError, match="Interactive browser"):
        case.auth.browser_sessions.issue(
            case.user_id,
            1,
            subject="Interactive selection is invalid",
            principal_kind="owner_session",
            scopes=("app.read",),
            resource_grants=selected,
        )


def test_missing_personal_mapping_mints_empty_without_initial_owner_fallback(
    case: IssuanceCase,
) -> None:
    user_id = new_id()
    with case.database.immediate_session() as session:
        session.add(User(id=user_id, display_name="User without a personal mapping"))
        session.flush()
        session.add(UserCredential(user_id=user_id, login_name="unmapped-user", auth_revision=1))
    token, _raw = case.auth.create_api_token(
        "Unmapped user token", user_id=user_id, auth_revision=1, scopes=("app.read",)
    )
    browser = case.auth.browser_sessions.issue(
        user_id,
        1,
        subject="Unmapped delegated browser",
        principal_kind="api_token",
        scopes=("app.read",),
    )
    assert token.user_id == user_id
    assert case.resources(CredentialOwner.TOKEN, token.id) == CredentialResources()
    assert case.resources(CredentialOwner.BROWSER, browser.record_id) == CredentialResources()


def test_token_selection_rejects_untyped_internal_input_before_any_write(
    case: IssuanceCase,
) -> None:
    before = _counts(case)
    with pytest.raises(TypeError, match="typed credential"):
        case.auth.create_api_token(
            "Untyped resource input",
            user_id=case.user_id,
            auth_revision=1,
            resource_grants=cast(CredentialResources, {"workspace_ids": [case.team_id]}),
        )
    assert _counts(case) == before
