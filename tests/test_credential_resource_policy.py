from __future__ import annotations

import socket
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest
import requests.sessions
from flask import Flask, g, has_request_context, request
from flask.testing import FlaskClient
from sqlalchemy import Connection, delete, event, select, update
from sqlalchemy.orm import Session
from werkzeug.test import TestResponse

from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAction,
    ProjectNotFound,
    WorkspaceNotFound,
)
from pandrator.web.access.project_policy import require_project_access, require_project_creation
from pandrator.web.access.project_visibility import project_ids_for_user
from pandrator.web.access.session_policy import SessionNotFound, require_session_access
from pandrator.web.access.session_queries import get_session_record, list_session_records
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import (
    CredentialOwner,
    CredentialResources,
    resources_for_owner,
)
from pandrator.web.accounts.execution_actor import UserActor, actor_from_principal
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
)
from pandrator.web.api import create_app
from pandrator.web.auth import AuthService, BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    AutomationClient,
    SessionRecord,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import ProjectMembership
from tests.web_test_support import prepare_web_test_data_root

READ_SCOPES = frozenset({"app.read"})
WRITE_SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def block_network_attempts() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked_request(*_args: object, **_kwargs: object) -> None:
        attempts.append("requests.sessions.Session.request")
        raise AssertionError("Credential resource policy tests must not use the network.")

    def blocked_connect(*_args: object, **_kwargs: object) -> None:
        attempts.append("socket.socket.connect")
        raise AssertionError("Credential resource policy tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked_request)
    patcher.setattr(socket.socket, "connect", blocked_connect)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts, f"Unexpected network attempts: {attempts!r}"


@dataclass(frozen=True, slots=True)
class PolicyCase:
    app: Flask
    bootstrap: BootstrapTokenStore
    auth: AuthService
    database: Database
    services: Any
    users: dict[str, str]
    personal_workspaces: dict[str, str]
    team_workspace_id: str
    projects: dict[str, str]
    sessions: dict[str, str]


@dataclass(frozen=True, slots=True)
class ApiCredential:
    user_id: str
    token_id: str
    subject: str
    raw_token: str = field(repr=False)
    scopes: frozenset[str]
    client_id: str | None
    actor: UserActor | None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.raw_token}"}

    @property
    def context(self) -> AccessContext:
        if self.actor is None:
            raise AssertionError("This credential was not resolved to a live actor.")
        return AccessContext(self.actor)


def _insert_user(
    session: Session,
    *,
    name: str,
    is_instance_admin: bool = False,
) -> tuple[str, str]:
    user_id = new_id()
    session.add(
        User(
            id=user_id,
            display_name=name,
            is_active=True,
            is_instance_admin=is_instance_admin,
        )
    )
    session.flush()
    session.add(
        UserCredential(
            user_id=user_id,
            login_name=f"credential-policy-{user_id}",
            password_hash=None,
            auth_revision=1,
        )
    )
    workspace = Workspace(
        id=new_id(),
        kind="personal",
        name=f"{name}'s workspace",
        personal_owner_user_id=user_id,
        state="active",
        revision=1,
    )
    session.add(workspace)
    session.flush()
    session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user_id,
            role="owner",
        )
    )
    session.flush()
    return user_id, workspace.id


def _record(
    name: str,
    *,
    updated_at: datetime = NOW,
    revision: int = 1,
) -> SessionRecord:
    return SessionRecord(
        id=new_id(),
        name=name,
        workflow_kind="subtitles",
        interface_mode="advanced",
        source_language="en",
        target_language="pl",
        included_stages_json=[],
        status="idle",
        revision=revision,
        created_at=updated_at,
        updated_at=updated_at,
    )


def _seed_project(
    session: Session,
    *,
    workspace_id: str,
    name: str,
    source_updated_at: datetime,
    language_updated_at: datetime,
) -> tuple[str, str, str]:
    source = _record(f"{name} source", updated_at=source_updated_at)
    project = create_session_project(session, source, workspace_id=workspace_id)
    language = _record(f"{name} language", updated_at=language_updated_at)
    create_session_project(session, language, project_id=project.id)
    return project.id, source.id, language.id


@pytest.fixture
def policy_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PolicyCase]:
    data_root = tmp_path / "data"
    prepare_web_test_data_root(data_root)
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=data_root,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]["services"]
    database: Database = services.database
    try:
        with database.immediate_session() as session:
            users: dict[str, str] = {}
            personal_workspaces: dict[str, str] = {}
            for name, is_instance_admin in (
                ("owner", False),
                ("member", False),
                ("guest", False),
                ("late-owner", False),
                ("private-owner", False),
                ("instance-admin", True),
            ):
                user_id, workspace_id = _insert_user(
                    session,
                    name=f"Credential policy {name}",
                    is_instance_admin=is_instance_admin,
                )
                users[name] = user_id
                personal_workspaces[name] = workspace_id

            team = Workspace(
                id=new_id(),
                kind="team",
                name="Credential policy team",
                personal_owner_user_id=None,
                state="active",
                revision=1,
            )
            session.add(team)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=team.id,
                        user_id=users["owner"],
                        role="owner",
                    ),
                    WorkspaceMembership(
                        workspace_id=team.id,
                        user_id=users["member"],
                        role="member",
                    ),
                ]
            )

            projects: dict[str, str] = {}
            sessions: dict[str, str] = {}
            for key, source_offset, language_offset in (
                ("team-a", 2, 3),
                ("team-b", 4, 5),
            ):
                project_id, source_id, language_id = _seed_project(
                    session,
                    workspace_id=team.id,
                    name=f"Credential policy {key}",
                    source_updated_at=NOW - timedelta(days=source_offset),
                    language_updated_at=NOW - timedelta(days=language_offset),
                )
                projects[key] = project_id
                sessions[f"{key}-source"] = source_id
                sessions[f"{key}-language"] = language_id

            private_project, private_source, private_language = _seed_project(
                session,
                workspace_id=personal_workspaces["private-owner"],
                name="Credential policy private personal project",
                source_updated_at=NOW - timedelta(days=7),
                language_updated_at=NOW - timedelta(days=8),
            )
            projects["private"] = private_project
            sessions["private-source"] = private_source
            sessions["private-language"] = private_language

            for user_key in ("owner", "member", "guest", "late-owner", "instance-admin"):
                project_id, source_id, language_id = _seed_project(
                    session,
                    workspace_id=personal_workspaces[user_key],
                    name=f"Credential policy {user_key} personal project",
                    source_updated_at=NOW - timedelta(days=9),
                    language_updated_at=NOW - timedelta(days=10),
                )
                projects[f"{user_key}-personal"] = project_id
                sessions[f"{user_key}-personal-source"] = source_id
                sessions[f"{user_key}-personal-language"] = language_id

            session.add_all(
                [
                    ProjectMembership(
                        project_id=projects["team-a"],
                        user_id=users["member"],
                        role="editor",
                    ),
                    ProjectMembership(
                        project_id=projects["team-b"],
                        user_id=users["guest"],
                        role="viewer",
                    ),
                ]
            )

        synthetic_users = frozenset(users.values())
        real_solo_access_allowed = services.auth.solo_access_allowed

        def allow_only_synthetic_users(
            user_id: str | None,
            auth_revision: int | None,
        ) -> bool:
            if user_id in synthetic_users and auth_revision == 1:
                return True
            return real_solo_access_allowed(user_id, auth_revision)

        monkeypatch.setattr(services.auth, "solo_access_allowed", allow_only_synthetic_users)
        yield PolicyCase(
            app=app,
            bootstrap=bootstrap,
            auth=services.auth,
            database=database,
            services=services,
            users=users,
            personal_workspaces=personal_workspaces,
            team_workspace_id=team.id,
            projects=projects,
            sessions=sessions,
        )
    finally:
        services.close()


def _api_credential(
    case: PolicyCase,
    user_key: str,
    *,
    resource_grants: CredentialResources | None = None,
    scopes: frozenset[str] = READ_SCOPES,
    client_id: str | None = None,
    expires_at: datetime | None = None,
    resolve_actor: bool = True,
) -> ApiCredential:
    user_id = case.users[user_key]
    token, raw_token = case.auth.create_api_token(
        "Synthetic credential resource policy token",
        scopes=scopes,
        expires_at=expires_at,
        user_id=user_id,
        auth_revision=1,
        client_id=client_id,
        resource_grants=resource_grants,
    )
    actor = None
    if resolve_actor:
        principal = case.auth.resolve_api_token(
            raw_token,
            network_zone="loopback",
            target_instance_id=None,
        )
        assert principal is not None
        actor = actor_from_principal(principal)
        assert actor is not None
    return ApiCredential(
        user_id=user_id,
        token_id=token.id,
        subject=token.subject or f"api-token:{token.id}",
        raw_token=raw_token,
        scopes=scopes,
        client_id=client_id,
        actor=actor,
    )


def _interactive_actor(
    case: PolicyCase,
    client: FlaskClient,
    user_key: str,
    *,
    scopes: frozenset[str] = READ_SCOPES,
) -> UserActor:
    user_id = case.users[user_key]
    subject = f"credential-resource-policy-interactive:{user_id}"
    grant = case.bootstrap.issue(
        user_id=user_id,
        auth_revision=1,
        subject=subject,
        scopes=scopes,
    )
    response = client.post("/api/v1/auth/bootstrap", json={"token": grant})
    assert response.status_code == 200, response.get_json()
    with client.session_transaction() as browser_state:
        raw_token = browser_state["auth_session"]
    principal = case.auth.browser_sessions.resolve(raw_token)
    assert principal is not None
    assert principal.user_id == user_id
    assert principal.kind == "owner_session"
    assert principal.client_id is None
    return UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        browser_session_id=principal.session_id,
    )


def _issued_browser_actor(
    case: PolicyCase,
    user_key: str,
    *,
    client_id: str,
    scopes: frozenset[str] = READ_SCOPES,
) -> UserActor:
    user_id = case.users[user_key]
    issued = case.auth.browser_sessions.issue(
        user_id,
        1,
        subject=f"credential-resource-policy-browser:{user_id}:{client_id}",
        principal_kind=(
            "manager_bootstrap" if client_id == "pandrator-mcp-local" else "owner_session"
        ),
        scopes=scopes,
        client_id=client_id,
    )
    principal = case.auth.browser_sessions.resolve(issued.raw_token)
    assert principal is not None
    assert principal.user_id == user_id
    assert principal.session_id == issued.record_id
    return UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        browser_session_id=principal.session_id,
        client_id=principal.client_id,
    )


def _insert_client(
    case: PolicyCase,
    user_key: str,
    *,
    resources: CredentialResources,
    scopes: frozenset[str] = WRITE_SCOPES,
) -> str:
    user_id = case.users[user_key]
    client_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Credential policy synthetic client",
                subject=f"credential-resource-policy-client:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=sorted(scopes),
                target_instance_id=new_id(),
                canonical_origin="http://127.0.0.1:53000",
                created_by="credential-resource-policy-test",
                user_id=user_id,
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add_all(
            [
                AutomationClientWorkspaceGrant(
                    client_id=client_id,
                    workspace_id=workspace_id,
                )
                for workspace_id in sorted(resources.workspace_ids)
            ]
            + [
                AutomationClientProjectGrant(
                    client_id=client_id,
                    project_id=project_id,
                )
                for project_id in sorted(resources.project_ids)
            ]
        )
    return client_id


def _grant_client_project(case: PolicyCase, client_id: str, project_id: str) -> None:
    with case.database.immediate_session() as session:
        session.add(AutomationClientProjectGrant(client_id=client_id, project_id=project_id))


def _session_ids(records: list[SessionRecord]) -> set[str]:
    return {record.id for record in records}


def _http_error(response: TestResponse) -> tuple[int, dict[str, Any]]:
    payload = response.get_json()
    assert isinstance(payload, dict) and isinstance(payload.get("error"), dict)
    error = dict(payload["error"])
    request_id = error.pop("request_id")
    assert isinstance(request_id, str) and request_id
    return response.status_code, error


def _session_snapshot(case: PolicyCase, session_id: str) -> dict[str, Any] | None:
    with case.database.snapshot_session() as session:
        row = (
            session.execute(select(SessionRecord.__table__).where(SessionRecord.id == session_id))
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


def _receipt_snapshot(
    case: PolicyCase,
    *,
    subject: str,
    key: str,
) -> dict[str, Any] | None:
    with case.database.snapshot_session() as session:
        row = (
            session.execute(
                select(ApiIdempotency.__table__).where(
                    ApiIdempotency.principal_subject == subject,
                    ApiIdempotency.operation_id == "updateSession",
                    ApiIdempotency.idempotency_key == key,
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


def test_default_token_and_reserved_manager_browsers_keep_their_personal_snapshot(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    token = _api_credential(case, "late-owner", scopes=READ_SCOPES)
    reserved_actors = [
        _issued_browser_actor(case, "late-owner", client_id=client_id)
        for client_id in ("pandrator-manager-browser", "pandrator-mcp-local")
    ]

    with case.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=case.team_workspace_id,
                user_id=case.users["late-owner"],
                role="owner",
            )
        )

    with case.database.snapshot_session() as session:
        role_visible = set(session.scalars(project_ids_for_user(token.user_id)).all())
        assert case.projects["team-a"] in role_visible
        assert case.projects["team-b"] in role_visible
        records = list_session_records(session, token.context)
        assert _session_ids(records) == {
            case.sessions["late-owner-personal-source"],
            case.sessions["late-owner-personal-language"],
        }
        assert (
            get_session_record(
                session,
                token.context,
                case.sessions["late-owner-personal-source"],
            ).id
            == case.sessions["late-owner-personal-source"]
        )
        with pytest.raises(SessionNotFound) as hidden:
            get_session_record(session, token.context, case.sessions["team-a-source"])
        with pytest.raises(SessionNotFound) as missing:
            get_session_record(session, token.context, "missing-session-id")
        assert hidden.value.args == missing.value.args == ("Session not found.",)

    with case.app.test_client() as client:
        listing = client.get("/api/v1/sessions", headers=token.headers)
        assert listing.status_code == 200
        assert {item["id"] for item in listing.get_json()["items"]} == {
            case.sessions["late-owner-personal-source"],
            case.sessions["late-owner-personal-language"],
        }
        hidden = client.get(
            f"/api/v1/sessions/{case.sessions['team-a-source']}",
            headers=token.headers,
        )
        missing = client.get("/api/v1/sessions/missing-session-id", headers=token.headers)
        assert _http_error(hidden) == _http_error(missing)
        assert _http_error(hidden) == (
            404,
            {"code": "not_found", "message": "Session not found.", "details": None},
        )

    with case.app.test_client() as client:
        interactive = _interactive_actor(case, client, "late-owner")
        assert interactive.principal_kind == "owner_session"
        assert interactive.browser_session_id is not None
        assert interactive.token_id is None
        assert interactive.client_id is None
        listing = client.get("/api/v1/sessions")
        assert listing.status_code == 200
        listed_ids = {item["id"] for item in listing.get_json()["items"]}
        assert case.sessions["team-a-source"] in listed_ids
        assert case.sessions["team-b-source"] in listed_ids
        detail = client.get(f"/api/v1/sessions/{case.sessions['team-a-source']}")
        assert detail.status_code == 200

    for actor in reserved_actors:
        reserved_client_id = actor.client_id
        assert actor.principal_kind == (
            "manager_bootstrap" if reserved_client_id == "pandrator-mcp-local" else "owner_session"
        )
        with case.database.snapshot_session() as session:
            reserved_rows = session.scalars(
                select(AutomationClient.id).where(AutomationClient.id == reserved_client_id)
            ).all()
            assert reserved_rows == []
            listed_ids = _session_ids(list_session_records(session, AccessContext(actor)))
            assert listed_ids == {
                case.sessions["late-owner-personal-source"],
                case.sessions["late-owner-personal-language"],
            }
            with pytest.raises(SessionNotFound):
                get_session_record(
                    session,
                    AccessContext(actor),
                    case.sessions["team-a-source"],
                )


def test_workspace_grants_still_intersect_live_project_roles_and_app_admin(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    owner = _api_credential(
        case,
        "owner",
        resource_grants=CredentialResources(workspace_ids=frozenset({case.team_workspace_id})),
        scopes=WRITE_SCOPES,
    )
    member = _api_credential(
        case,
        "member",
        resource_grants=CredentialResources(workspace_ids=frozenset({case.team_workspace_id})),
        scopes=WRITE_SCOPES,
    )
    with case.database.snapshot_session() as session:
        owner_ids = _session_ids(list_session_records(session, owner.context))
        assert owner_ids == {
            case.sessions["team-a-source"],
            case.sessions["team-a-language"],
            case.sessions["team-b-source"],
            case.sessions["team-b-language"],
        }
        member_ids = _session_ids(list_session_records(session, member.context))
        assert member_ids == owner_ids
        assert (
            require_project_access(
                session,
                member.context,
                case.projects["team-a"],
                ProjectAction.EDIT,
            ).project_id
            == case.projects["team-a"]
        )
        assert (
            require_project_access(
                session, member.context, case.projects["team-b"], ProjectAction.EDIT
            ).project_id
            == case.projects["team-b"]
        )
        with pytest.raises(AccessDenied):
            require_project_access(
                session, member.context, case.projects["team-b"], ProjectAction.MANAGE_MEMBERS
            )
        for context in (owner.context, member.context):
            for session_id in (
                case.sessions["owner-personal-source"],
                case.sessions["private-source"],
            ):
                with pytest.raises(SessionNotFound):
                    get_session_record(session, context, session_id)

    admin = _api_credential(
        case,
        "instance-admin",
        resource_grants=CredentialResources(),
        scopes=READ_SCOPES | {"app.admin"},
    )
    with case.database.immediate_session() as session:
        # An overbroad synthetic ceiling must not hide an Administrator role
        # bypass behind a missing-resource denial. Neither target has a User grant.
        session.add_all(
            [
                ApiTokenWorkspaceGrant(
                    token_id=admin.token_id, workspace_id=case.team_workspace_id
                ),
                ApiTokenProjectGrant(token_id=admin.token_id, project_id=case.projects["private"]),
            ]
        )
    with case.database.snapshot_session() as session:
        assert list_session_records(session, admin.context) == []
        for session_id in (
            case.sessions["team-a-source"],
            case.sessions["instance-admin-personal-source"],
            case.sessions["private-source"],
        ):
            with pytest.raises(SessionNotFound):
                get_session_record(session, admin.context, session_id)


def test_project_only_guest_reads_its_root_but_cannot_create_or_edit(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    guest = _api_credential(
        case,
        "guest",
        resource_grants=CredentialResources(project_ids=frozenset({case.projects["team-b"]})),
        scopes=WRITE_SCOPES,
    )
    with case.database.snapshot_session() as session:
        assert _session_ids(list_session_records(session, guest.context)) == {
            case.sessions["team-b-source"],
            case.sessions["team-b-language"],
        }
        for key in ("team-b-source", "team-b-language"):
            assert (
                get_session_record(session, guest.context, case.sessions[key]).id
                == (case.sessions[key])
            )
        for session_id in (
            case.sessions["team-a-source"],
            case.sessions["guest-personal-source"],
            case.sessions["private-source"],
        ):
            with pytest.raises(SessionNotFound):
                get_session_record(session, guest.context, session_id)
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, guest.context, case.team_workspace_id)
        with pytest.raises(AccessDenied):
            require_session_access(
                session,
                guest.context,
                case.sessions["team-b-source"],
                ProjectAction.EDIT,
            )

    with case.app.test_client() as client:
        source = client.get(
            f"/api/v1/sessions/{case.sessions['team-b-source']}",
            headers=guest.headers,
        )
        hidden = client.get(
            f"/api/v1/sessions/{case.sessions['team-a-source']}",
            headers=guest.headers,
        )
        missing = client.get("/api/v1/sessions/missing-session-id", headers=guest.headers)
        assert source.status_code == 200
        assert _http_error(hidden) == _http_error(missing)
        assert _http_error(hidden)[0] == 404
        edit = client.patch(
            f"/api/v1/sessions/{case.sessions['team-b-source']}",
            json={"interface_mode": "simple"},
            headers={**guest.headers, "If-Match": '"1"'},
        )
        assert _http_error(edit) == (
            403,
            {
                "code": "scope_denied",
                "message": "The requested action is not permitted.",
                "details": None,
            },
        )


def test_empty_and_missing_grants_never_fall_back_to_personal_access(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    empty = _api_credential(
        case,
        "owner",
        resource_grants=CredentialResources(),
        scopes=READ_SCOPES,
    )
    personal_session = case.sessions["owner-personal-source"]
    with case.database.snapshot_session() as session:
        assert list_session_records(session, empty.context) == []
        with pytest.raises(SessionNotFound) as empty_error:
            get_session_record(session, empty.context, personal_session)
        with pytest.raises(ProjectNotFound):
            require_project_access(
                session,
                empty.context,
                case.projects["owner-personal"],
                ProjectAction.READ,
            )

    missing_rows = _api_credential(case, "owner", scopes=READ_SCOPES)
    with case.database.immediate_session() as session:
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(
                ApiTokenWorkspaceGrant.token_id == missing_rows.token_id
            )
        )
        session.execute(
            delete(ApiTokenProjectGrant).where(
                ApiTokenProjectGrant.token_id == missing_rows.token_id
            )
        )
    with case.database.snapshot_session() as session:
        assert list_session_records(session, missing_rows.context) == []
        with pytest.raises(SessionNotFound) as missing_error:
            get_session_record(session, missing_rows.context, personal_session)
        assert empty_error.value.args == missing_error.value.args

    with case.app.test_client() as client:
        empty_listing = client.get("/api/v1/sessions", headers=empty.headers)
        missing_listing = client.get("/api/v1/sessions", headers=missing_rows.headers)
        assert empty_listing.status_code == missing_listing.status_code == 200
        assert empty_listing.get_json()["items"] == []
        assert missing_listing.get_json()["items"] == []
        empty_detail = client.get(f"/api/v1/sessions/{personal_session}", headers=empty.headers)
        missing_detail = client.get(
            f"/api/v1/sessions/{personal_session}", headers=missing_rows.headers
        )
        assert _http_error(empty_detail) == _http_error(missing_detail)
        assert _http_error(empty_detail) == (
            404,
            {"code": "not_found", "message": "Session not found.", "details": None},
        )


def test_token_client_grants_intersect_currently_and_are_not_cached(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    client_id = _insert_client(
        case,
        "owner",
        resources=CredentialResources(
            project_ids=frozenset({case.projects["team-a"], case.projects["team-b"]})
        ),
    )
    token = _api_credential(case, "owner", client_id=client_id, scopes=READ_SCOPES)
    with case.database.snapshot_session() as session:
        assert resources_for_owner(session, CredentialOwner.TOKEN, token.token_id) == (
            CredentialResources(
                project_ids=frozenset({case.projects["team-a"], case.projects["team-b"]})
            )
        )

    with case.database.immediate_session() as session:
        session.execute(
            delete(AutomationClientProjectGrant).where(
                AutomationClientProjectGrant.client_id == client_id,
                AutomationClientProjectGrant.project_id == case.projects["team-b"],
            )
        )
    with case.database.snapshot_session() as session:
        assert resources_for_owner(session, CredentialOwner.TOKEN, token.token_id).project_ids == (
            frozenset({case.projects["team-a"], case.projects["team-b"]})
        )
        assert resources_for_owner(session, CredentialOwner.CLIENT, client_id).project_ids == (
            frozenset({case.projects["team-a"]})
        )
        assert _session_ids(list_session_records(session, token.context)) == {
            case.sessions["team-a-source"],
            case.sessions["team-a-language"],
        }
        with pytest.raises(SessionNotFound):
            get_session_record(session, token.context, case.sessions["team-b-source"])


def test_workspace_project_client_ceiling_intersects_in_both_directions(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    team = CredentialResources(workspace_ids=frozenset({case.team_workspace_id}))
    project_a = CredentialResources(project_ids=frozenset({case.projects["team-a"]}))

    workspace_client = _insert_client(case, "owner", resources=team)
    project_token = _api_credential(
        case,
        "owner",
        client_id=workspace_client,
        resource_grants=project_a,
        scopes=WRITE_SCOPES,
    )
    with case.database.snapshot_session() as session:
        assert _session_ids(list_session_records(session, project_token.context)) == {
            case.sessions["team-a-source"],
            case.sessions["team-a-language"],
        }
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, project_token.context, case.team_workspace_id)

    project_client = _insert_client(case, "owner", resources=project_a)
    workspace_token = _api_credential(
        case,
        "owner",
        client_id=project_client,
        resource_grants=project_a,
        scopes=WRITE_SCOPES,
    )
    with case.database.immediate_session() as session:
        # This disposable fixture exceeds the client ceiling to exercise the
        # other half of the SQL AND predicate; normal minting rejects it.
        session.add(
            ApiTokenWorkspaceGrant(
                token_id=workspace_token.token_id,
                workspace_id=case.team_workspace_id,
            )
        )
    with case.database.snapshot_session() as session:
        token_resources = resources_for_owner(
            session,
            CredentialOwner.TOKEN,
            workspace_token.token_id,
        )
        assert token_resources == CredentialResources(
            workspace_ids=frozenset({case.team_workspace_id}),
            project_ids=frozenset({case.projects["team-a"]}),
        )
        assert (
            resources_for_owner(
                session,
                CredentialOwner.CLIENT,
                project_client,
            )
            == project_a
        )
        assert _session_ids(list_session_records(session, workspace_token.context)) == {
            case.sessions["team-a-source"],
            case.sessions["team-a-language"],
        }
        with pytest.raises(SessionNotFound):
            get_session_record(session, workspace_token.context, case.sessions["team-b-source"])
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, workspace_token.context, case.team_workspace_id)


def test_client_expansion_does_not_expand_issued_token_or_bound_browser_snapshot(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    project_a = case.projects["team-a"]
    project_b = case.projects["team-b"]
    client_id = _insert_client(
        case,
        "owner",
        resources=CredentialResources(project_ids=frozenset({project_a})),
    )
    token = _api_credential(case, "owner", client_id=client_id, scopes=READ_SCOPES)
    browser = _issued_browser_actor(case, "owner", client_id=client_id)
    assert browser.browser_session_id is not None
    with case.database.snapshot_session() as session:
        assert resources_for_owner(session, CredentialOwner.TOKEN, token.token_id).project_ids == (
            frozenset({project_a})
        )
        assert resources_for_owner(
            session,
            CredentialOwner.BROWSER,
            browser.browser_session_id,
        ).project_ids == frozenset({project_a})

    _grant_client_project(case, client_id, project_b)
    with case.database.snapshot_session() as session:
        for context in (token.context, AccessContext(browser)):
            assert _session_ids(list_session_records(session, context)) == {
                case.sessions["team-a-source"],
                case.sessions["team-a-language"],
            }
            with pytest.raises(SessionNotFound):
                get_session_record(session, context, case.sessions["team-b-source"])


@pytest.mark.parametrize("revocation", ["grant", "membership"])
def test_fresh_sql_rechecks_stale_grant_and_membership_rows_without_flushing(
    policy_case: PolicyCase,
    revocation: str,
) -> None:
    case = policy_case
    if revocation == "membership":
        # This subcase exercises a legacy project-only grant. An active workspace
        # member keeps content authority when only a redundant project row is removed.
        with case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team_workspace_id,
                    WorkspaceMembership.user_id == case.users["member"],
                )
            )
    credential = _api_credential(
        case,
        "member",
        resource_grants=CredentialResources(project_ids=frozenset({case.projects["team-a"]})),
        scopes=READ_SCOPES,
    )
    with case.database.immediate_session() as session:
        grant = session.get(ApiTokenProjectGrant, (credential.token_id, case.projects["team-a"]))
        membership = session.get(ProjectMembership, (case.projects["team-a"], credential.user_id))
        assert grant is not None
        assert membership is not None and membership.role == "editor"
        pending_user = User(
            id=new_id(),
            display_name="Unrelated pending credential policy user",
            is_active=True,
            is_instance_admin=False,
        )
        session.add(pending_user)
        if revocation == "grant":
            session.execute(
                delete(ApiTokenProjectGrant)
                .where(
                    ApiTokenProjectGrant.token_id == credential.token_id,
                    ApiTokenProjectGrant.project_id == case.projects["team-a"],
                )
                .execution_options(synchronize_session=False, autoflush=False)
            )
        else:
            session.execute(
                delete(ProjectMembership)
                .where(
                    ProjectMembership.project_id == case.projects["team-a"],
                    ProjectMembership.user_id == credential.user_id,
                )
                .execution_options(synchronize_session=False, autoflush=False)
            )

        assert _session_ids(list_session_records(session, credential.context)) == set()
        with pytest.raises(SessionNotFound):
            get_session_record(session, credential.context, case.sessions["team-a-source"])
        assert grant.project_id == case.projects["team-a"]
        assert membership.role == "editor"
        assert pending_user in session.new
        assert pending_user.id is not None
        assert session.in_transaction()


@pytest.mark.parametrize(
    "invalidator", ["token_revocation", "stale_epoch", "expiry", "scope_narrowing"]
)
def test_http_policy_keeps_actor_revocation_epoch_expiry_and_scope_checks(
    policy_case: PolicyCase,
    invalidator: str,
) -> None:
    case = policy_case
    credential = _api_credential(
        case,
        "owner",
        resource_grants=CredentialResources(workspace_ids=frozenset({case.team_workspace_id})),
        scopes=WRITE_SCOPES,
        expires_at=(utcnow() - timedelta(seconds=1)) if invalidator == "expiry" else None,
        resolve_actor=invalidator != "expiry",
    )
    with case.database.snapshot_session() as session:
        assert resources_for_owner(session, CredentialOwner.TOKEN, credential.token_id) == (
            CredentialResources(workspace_ids=frozenset({case.team_workspace_id}))
        )

    with case.database.immediate_session() as session:
        if invalidator == "token_revocation":
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == credential.token_id)
                .values(revoked_at=utcnow())
            )
        elif invalidator == "stale_epoch":
            session.execute(
                update(UserCredential)
                .where(UserCredential.user_id == credential.user_id)
                .values(auth_revision=2)
            )
        elif invalidator == "scope_narrowing":
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == credential.token_id)
                .values(scopes_json=["app.write"])
            )

    with case.app.test_client() as client:
        response = client.get(
            f"/api/v1/sessions/{case.sessions['team-a-source']}",
            headers=credential.headers,
        )
    status, error = _http_error(response)
    if invalidator == "scope_narrowing":
        assert (status, error) == (
            403,
            {
                "code": "scope_denied",
                "message": "The authenticated principal lacks the required scope.",
                "details": {"required_scopes": ["app.read"]},
            },
        )
    else:
        assert status == 401


def test_session_query_filters_before_pagination_and_keeps_semantic_errors(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    credential = _api_credential(
        case,
        "member",
        resource_grants=CredentialResources(project_ids=frozenset({case.projects["team-a"]})),
        scopes=WRITE_SCOPES,
    )
    hidden = _record(
        "Unauthorized newer private sentinel",
        updated_at=NOW + timedelta(days=2),
        revision=919,
    )
    with case.database.immediate_session() as session:
        create_session_project(session, hidden, project_id=case.projects["team-b"])

    with case.database.snapshot_session() as session:
        page = list_session_records(session, credential.context, limit=1)
        assert [record.id for record in page] == [case.sessions["team-a-source"]]
        assert all(record.name != hidden.name for record in page)
        assert all(record.revision != hidden.revision for record in page)
        with pytest.raises(SessionNotFound) as hidden_error:
            get_session_record(session, credential.context, hidden.id)
        with pytest.raises(SessionNotFound) as missing_error:
            get_session_record(session, credential.context, "missing-session-id")
        assert hidden_error.value.args == missing_error.value.args

    with case.app.test_client() as client:
        listing = client.get("/api/v1/sessions", headers=credential.headers)
        serialized = str(listing.get_json())
        assert listing.status_code == 200
        assert hidden.id not in serialized
        assert hidden.name not in serialized
        assert all(item["revision"] != 919 for item in listing.get_json()["items"])
        hidden_response = client.get(f"/api/v1/sessions/{hidden.id}", headers=credential.headers)
        missing_response = client.get(
            "/api/v1/sessions/missing-session-id",
            headers=credential.headers,
        )
        assert _http_error(hidden_response) == _http_error(missing_response)
        assert _http_error(hidden_response)[0] == 404


def test_saved_idempotent_patch_cannot_replay_after_token_team_grant_deletion(
    policy_case: PolicyCase,
) -> None:
    case = policy_case
    credential = _api_credential(
        case,
        "member",
        resource_grants=CredentialResources(workspace_ids=frozenset({case.team_workspace_id})),
        scopes=WRITE_SCOPES,
    )
    session_id = case.sessions["team-a-source"]
    key = "credential-resource-revocation-replay-001"
    with case.app.test_client() as client:
        first = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"interface_mode": "simple"},
            headers={
                **credential.headers,
                "If-Match": '"1"',
                "Idempotency-Key": key,
            },
        )
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["interface_mode"] == "simple"
    receipt_before = _receipt_snapshot(case, subject=credential.subject, key=key)
    session_before = _session_snapshot(case, session_id)
    assert receipt_before is not None
    assert session_before is not None

    with case.database.immediate_session() as session:
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(
                ApiTokenWorkspaceGrant.token_id == credential.token_id,
                ApiTokenWorkspaceGrant.workspace_id == case.team_workspace_id,
            )
        )
        membership = session.get(ProjectMembership, (case.projects["team-a"], credential.user_id))
        token = session.get(ApiToken, credential.token_id)
        assert membership is not None and membership.role == "editor"
        assert token is not None and token.revoked_at is None
        assert token.scopes_json == sorted(WRITE_SCOPES)

    with case.app.test_client() as client:
        replay = client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"interface_mode": "simple"},
            headers={
                **credential.headers,
                "If-Match": '"1"',
                "Idempotency-Key": key,
            },
        )
    assert _http_error(replay) == (
        404,
        {"code": "not_found", "message": "Session not found.", "details": None},
    )
    assert _receipt_snapshot(case, subject=credential.subject, key=key) == receipt_before
    assert _session_snapshot(case, session_id) == session_before


def test_writer_wait_rechecks_team_grant_before_idempotent_patch(
    policy_case: PolicyCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = policy_case
    credential = _api_credential(
        case,
        "member",
        resource_grants=CredentialResources(workspace_ids=frozenset({case.team_workspace_id})),
        scopes=WRITE_SCOPES,
    )
    session_id = case.sessions["team-a-source"]
    request_thread_name = "credential-resource-writer-revocation-request"
    expected_path = f"/api/v1/sessions/{session_id}"
    writer_entry_reached = Event()
    release_request_writer = Event()
    route_begin_expected = Event()
    request_begin_attempted = Event()
    response_future: Future[TestResponse] = Future()
    key = "credential-resource-writer-revocation-001"
    session_before = _session_snapshot(case, session_id)
    original_immediate_session = case.database.immediate_session

    def before_cursor_execute(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            current_thread().name == request_thread_name
            and route_begin_expected.is_set()
            and statement.lstrip().upper() == "BEGIN IMMEDIATE"
        ):
            request_begin_attempted.set()

    def make_request() -> None:
        try:
            with case.app.test_client() as client:
                response = client.patch(
                    expected_path,
                    json={"interface_mode": "simple"},
                    headers={
                        **credential.headers,
                        "If-Match": '"1"',
                        "Idempotency-Key": key,
                    },
                )
            response_future.set_result(response)
        except BaseException as error:
            response_future.set_exception(error)

    thread = Thread(target=make_request, name=request_thread_name)
    response: TestResponse | None = None

    with monkeypatch.context() as scoped_patch:

        @contextmanager
        def controlled_immediate_session(
            *,
            busy_timeout_ms: int | None = None,
        ) -> Iterator[Session]:
            if (
                current_thread().name == request_thread_name
                and has_request_context()
                and request.method == "PATCH"
                and request.path == expected_path
                and getattr(g, "principal", None) is not None
                and not writer_entry_reached.is_set()
            ):
                assert request.endpoint is not None
                assert request.endpoint.rsplit(".", 1)[-1] == "session_update"
                writer_entry_reached.set()
                if not release_request_writer.wait(timeout=15):
                    raise TimeoutError("The bounded route-writer gate was not released.")
                route_begin_expected.set()
                if busy_timeout_ms is None:
                    busy_timeout_ms = 15_000
            with original_immediate_session(busy_timeout_ms=busy_timeout_ms) as session:
                yield session

        scoped_patch.setattr(
            case.database,
            "immediate_session",
            controlled_immediate_session,
        )
        listener_registered = False
        try:
            event.listen(
                case.database.engine,
                "before_cursor_execute",
                before_cursor_execute,
            )
            listener_registered = True
            thread.start()
            assert writer_entry_reached.wait(timeout=15), (
                "The request did not reach its post-authentication writer entry."
            )
            with original_immediate_session() as writer:
                writer.execute(
                    delete(ApiTokenWorkspaceGrant).where(
                        ApiTokenWorkspaceGrant.token_id == credential.token_id,
                        ApiTokenWorkspaceGrant.workspace_id == case.team_workspace_id,
                    )
                )
                release_request_writer.set()
                assert request_begin_attempted.wait(timeout=15), (
                    "The route did not attempt BEGIN IMMEDIATE while grant revocation held the writer lock."
                )
        finally:
            release_request_writer.set()
            if listener_registered:
                event.remove(
                    case.database.engine,
                    "before_cursor_execute",
                    before_cursor_execute,
                )
            if thread.is_alive():
                thread.join(timeout=15)
            if thread.ident is not None:
                response = response_future.result(timeout=15)
        assert not thread.is_alive(), "The bounded request thread did not finish."

    assert response is not None
    assert _http_error(response) == (
        404,
        {"code": "not_found", "message": "Session not found.", "details": None},
    )
    assert _receipt_snapshot(case, subject=credential.subject, key=key) is None
    assert _session_snapshot(case, session_id) == session_before
