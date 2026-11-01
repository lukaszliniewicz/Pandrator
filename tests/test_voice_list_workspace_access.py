from __future__ import annotations

import socket

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web import voice_routes
from pandrator.web.access.contracts import ProjectAction
from pandrator.web.access.workspace_policy import WorkspaceAction
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.commands import AccountCommands
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.models import Workspace
from tests.test_account_authentication_integration import AuthCase
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import PASSWORD, enable, new_account
from tests.test_evidence_access_routes import body, headers, team
from tests.test_project_sharing_routes import login
from tests.test_provider_workspace_access import promote, scoped_token


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args, **_kwargs):
        raise AssertionError("Voice-list workspace qualification must remain local")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def personal_workspace_id(case: AuthCase, user_id: str) -> str:
    with case.database.snapshot_session() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(
                Workspace.kind == "personal",
                Workspace.personal_owner_user_id == user_id,
            )
        )
    assert workspace_id is not None
    return workspace_id


def seed_voice(case: AuthCase, workspace_id: str, name: str) -> str:
    with case.database.immediate_session() as session:
        voice = m.Voice(
            workspace_id=workspace_id,
            name=name,
            metadata_json={"voice_category": "unspecified"},
        )
        session.add(voice)
        session.flush()
        return voice.id


def voice_names(response, *, status: int = 200) -> list[str]:
    payload = response.get_json()
    assert response.status_code == status, payload
    if status != 200:
        return []
    assert isinstance(payload, dict)
    return [item["name"] for item in payload["items"]]


def domain_counts(case: AuthCase) -> tuple[int, int, int]:
    with case.database.snapshot_session() as session:
        return (
            session.scalar(select(func.count()).select_from(m.Voice)) or 0,
            session.scalar(select(func.count()).select_from(m.VoiceSample)) or 0,
            session.scalar(select(func.count()).select_from(m.Artifact)) or 0,
        )


def create_team_session(members, *, key: str = "voice-list-team-session") -> dict:
    response = members.alice.post(
        "/api/v1/sessions",
        json={"name": "Voice list team session", "workspace_id": members.workspace_id},
        headers=headers(members.ah, key),
    )
    return body(response, 201)


def test_solo_initial_owner_keeps_bundle_convenience_but_read_token_cannot_seed(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id = case.auth.initial_account().user_id
    workspace_id = personal_workspace_id(case, owner_id)
    seed_voice(case, workspace_id, "Initial owner voice")
    initializer_calls: list[str | None] = []

    def initialize(_database, _paths, _artifacts, *, workspace_id=None):
        initializer_calls.append(workspace_id)
        return None

    monkeypatch.setattr(voice_routes, "ensure_bundled_voice", initialize)

    owner_response = case.client.get("/api/v1/voices")
    assert voice_names(owner_response) == ["Initial owner voice"]
    assert owner_response.get_json()["workspace_id"] == workspace_id
    assert initializer_calls == [workspace_id]

    _token, raw_token = case.auth.create_api_token(
        "Read-only voice-list qualification",
        user_id=owner_id,
        scopes=frozenset({"app.read"}),
        resource_grants=CredentialResources(workspace_ids=frozenset({workspace_id})),
    )
    api = case.app.test_client()
    bearer = {"Authorization": f"Bearer {raw_token}"}
    before = domain_counts(case)
    read_only = api.get("/api/v1/voices", headers=bearer)
    assert voice_names(read_only) == ["Initial owner voice"]
    assert read_only.get_json()["workspace_id"] == workspace_id
    head = api.head("/api/v1/voices", headers=bearer)
    assert head.status_code == 200
    assert initializer_calls == [workspace_id]
    assert domain_counts(case) == before


def test_named_users_and_team_members_and_managers_read_only_the_proved_workspace(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = team(case, monkeypatch)
    foreign_admin = new_account(case, "foreign-admin", "administrator")
    foreign_client, _foreign_headers = login(case, "foreign-admin")
    owner_id = case.auth.initial_account().user_id
    initial_workspace = personal_workspace_id(case, owner_id)
    alice_home = personal_workspace_id(case, members.alice_id)
    foreign_home = personal_workspace_id(case, foreign_admin.user_id)
    seed_voice(case, initial_workspace, "Initial private voice")
    seed_voice(case, alice_home, "Alice private voice")
    seed_voice(case, foreign_home, "Foreign admin private voice")
    seed_voice(case, members.workspace_id, "Team voice")
    source = create_team_session(members)

    initializer_calls: list[str | None] = []

    def observe_initializer(_database, _paths, _artifacts, *, workspace_id=None):
        initializer_calls.append(workspace_id)
        return None

    monkeypatch.setattr(voice_routes, "ensure_bundled_voice", observe_initializer)

    assert voice_names(members.alice.get("/api/v1/voices")) == ["Alice private voice"]
    own = members.alice.get(
        "/api/v1/voices", query_string={"workspace_id": alice_home}
    )
    assert voice_names(own) == ["Alice private voice"]
    assert own.get_json()["workspace_id"] == alice_home

    team_list = members.alice.get(
        "/api/v1/voices", query_string={"workspace_id": members.workspace_id}
    )
    member_names = voice_names(team_list)
    assert member_names == ["Team voice"]
    assert team_list.get_json()["workspace_id"] == members.workspace_id
    assert voice_names(
        members.alice.get(
            "/api/v1/voices", query_string={"session_id": source["id"]}
        )
    ) == ["Team voice"]

    promote(case, members)
    manager_list = members.alice.get(
        "/api/v1/voices", query_string={"workspace_id": members.workspace_id}
    )
    assert voice_names(manager_list) == member_names

    assert voice_names(case.client.get("/api/v1/voices")) == ["Initial private voice"]
    assert voice_names(foreign_client.get("/api/v1/voices")) == [
        "Foreign admin private voice"
    ]
    for response in (
        foreign_client.get(
            "/api/v1/voices", query_string={"workspace_id": initial_workspace}
        ),
        foreign_client.get(
            "/api/v1/voices", query_string={"workspace_id": members.workspace_id}
        ),
        foreign_client.get(
            "/api/v1/voices", query_string={"session_id": source["id"]}
        ),
    ):
        voice_names(response, status=404)
    assert initializer_calls == []


def test_project_only_token_and_unavailable_or_mixed_roots_are_opaque(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = team(case, monkeypatch)
    source = create_team_session(members)
    seed_voice(case, members.workspace_id, "Team voice")
    alice_home = personal_workspace_id(case, members.alice_id)
    new_account(case, "foreign-admin", "administrator")
    foreign_client, foreign_csrf = login(case, "foreign-admin")
    foreign_source = body(
        foreign_client.post(
            "/api/v1/sessions",
            json={"name": "Foreign private session"},
            headers=headers(foreign_csrf, "voice-list-foreign-session"),
        ),
        201,
    )

    token, bearer = scoped_token(
        case,
        members.alice_id,
        {"app.read"},
        project=source["project_id"],
    )
    assert token.id
    api = case.app.test_client()
    session_reads = []
    workspace_checks = []
    actual_session_access = voice_routes.require_session_access
    actual_workspace_access = voice_routes.require_workspace_access

    def observe_session_access(db_session, access, session_id, action):
        result = actual_session_access(db_session, access, session_id, action)
        session_reads.append((session_id, result.workspace_id, action))
        return result

    def observe_workspace_access(db_session, access, workspace_id=None, *, action=WorkspaceAction.READ):
        workspace_checks.append((workspace_id, action))
        return actual_workspace_access(
            db_session, access, workspace_id, action=action
        )

    monkeypatch.setattr(voice_routes, "require_session_access", observe_session_access)
    monkeypatch.setattr(voice_routes, "require_workspace_access", observe_workspace_access)

    project_only = api.get(
        "/api/v1/voices", query_string={"session_id": source["id"]}, headers=bearer
    )
    voice_names(project_only, status=404)
    assert session_reads == [(source["id"], members.workspace_id, ProjectAction.READ)]
    assert workspace_checks == [(members.workspace_id, WorkspaceAction.READ)]

    mismatch = members.alice.get(
        "/api/v1/voices",
        query_string={"workspace_id": alice_home, "session_id": source["id"]},
    )
    missing_workspace = members.alice.get(
        "/api/v1/voices", query_string={"workspace_id": "voice-workspace-not-present"}
    )
    missing_session = members.alice.get(
        "/api/v1/voices", query_string={"session_id": "voice-session-not-present"}
    )
    foreign_project = members.alice.get(
        "/api/v1/voices", query_string={"session_id": foreign_source["id"]}
    )
    errors = []
    for response in (mismatch, missing_workspace, missing_session, foreign_project):
        voice_names(response, status=404)
        errors.append((response.get_json()["error"]["code"], response.get_json()["error"]["message"]))
    assert errors == [("not_found", "Voice workspace not found.")] * len(errors)

    validation_errors = []
    for parameter in ("workspace_id", "session_id"):
        for invalid in ("", " ", "x" * 81):
            response = members.alice.get(
                "/api/v1/voices", query_string={parameter: invalid}
            )
            voice_names(response, status=422)
            validation_errors.append(
                (response.get_json()["error"]["code"], response.get_json()["error"]["message"])
            )
    assert validation_errors == [
        ("validation_error", "Choose a valid voice workspace or session scope.")
    ] * len(validation_errors)


def test_membership_removal_password_reset_and_token_scope_loss_fence_retries(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = team(case, monkeypatch)
    alice_home = personal_workspace_id(case, members.alice_id)
    seed_voice(case, alice_home, "Alice home voice")
    seed_voice(case, members.workspace_id, "Team voice")

    team_request = {"workspace_id": members.workspace_id}
    assert voice_names(members.alice.get("/api/v1/voices", query_string=team_request)) == [
        "Team voice"
    ]
    state = body(members.owner.get(f"/api/v1/workspaces/{members.workspace_id}"))
    removed = members.owner.delete(
        f"/api/v1/workspaces/{members.workspace_id}/members/{members.alice_id}",
        json={"expected_revision": state["revision"]},
        headers=members.oh,
    )
    assert removed.status_code == 200, removed.get_json()
    voice_names(members.alice.get("/api/v1/voices", query_string=team_request), status=404)
    assert voice_names(members.alice.get("/api/v1/voices")) == ["Alice home voice"]

    token, raw_token = case.auth.create_api_token(
        "Scoped voice-list qualification",
        user_id=members.alice_id,
        scopes=frozenset({"app.read"}),
        resource_grants=CredentialResources(workspace_ids=frozenset({alice_home})),
    )
    api = case.app.test_client()
    bearer = {"Authorization": f"Bearer {raw_token}"}
    assert voice_names(api.get("/api/v1/voices", headers=bearer)) == ["Alice home voice"]
    with case.database.immediate_session() as session:
        stored_token = session.get_one(m.ApiToken, token.id)
        stored_token.scopes_json = ["app.write"]
    assert api.get("/api/v1/voices", headers=bearer).status_code == 403

    with case.database.snapshot_session() as session:
        credential = session.get_one(UserCredential, members.alice_id)
        revision = credential.auth_revision
    grant = AccountCommands(case.database).issue_password_reset(
        case.actor, members.alice_id, expected_auth_revision=revision
    )
    AccountCommands(case.database).redeem_password_reset(
        grant.raw_token, PASSWORD + " replaced"
    )
    assert members.alice.get("/api/v1/voices").status_code == 401


def test_named_get_head_do_not_hydrate_and_workspace_policy_queries_do_not_scale_with_voice_rows(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = new_account(case)
    enable(case, monkeypatch)
    alice_client, _alice_headers = login(case, "alice")
    workspace_id = personal_workspace_id(case, alice.user_id)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("A passive voice-list read reached a hydration path")

    monkeypatch.setattr(voice_routes, "ensure_bundled_voice", unexpected)
    monkeypatch.setattr(voice_routes, "package_metadata", unexpected)
    monkeypatch.setattr(voice_routes, "resolve_voice_sample_transcription_settings", unexpected)
    monkeypatch.setattr("pandrator.web.credentials.resolve_provider_credential", unexpected)

    def query_policy(client):
        statements: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lower())

        event.listen(case.database.engine, "before_cursor_execute", capture)
        try:
            response = client.get(
                "/api/v1/voices", query_string={"workspace_id": workspace_id}
            )
        finally:
            event.remove(case.database.engine, "before_cursor_execute", capture)
        assert response.status_code == 200, response.get_json()
        policy_queries = [
            index
            for index, statement in enumerate(statements)
            if "workspace_memberships" in statement and "workspaces" in statement
        ]
        voice_queries = [
            index
            for index, statement in enumerate(statements)
            if "voices" in statement and "workspace_memberships" not in statement
        ]
        assert policy_queries
        assert voice_queries and min(voice_queries) > min(policy_queries)
        return len(policy_queries), response

    before_empty = domain_counts(case)
    empty_policy_queries, empty = query_policy(alice_client)
    assert voice_names(empty) == []
    assert empty.get_json()["workspace_id"] == workspace_id
    assert domain_counts(case) == before_empty
    assert alice_client.head(
        "/api/v1/voices", query_string={"workspace_id": workspace_id}
    ).status_code == 200
    assert domain_counts(case) == before_empty

    seed_voice(case, workspace_id, "Only voice")
    before_one = domain_counts(case)
    one_voice_policy_queries, one_voice = query_policy(alice_client)
    assert voice_names(one_voice) == ["Only voice"]
    assert one_voice.get_json()["workspace_id"] == workspace_id
    assert domain_counts(case) == before_one
    assert alice_client.head(
        "/api/v1/voices", query_string={"workspace_id": workspace_id}
    ).status_code == 200
    assert domain_counts(case) == before_one
    with case.database.immediate_session() as session:
        for index in range(8):
            session.add(
                m.Voice(
                    workspace_id=workspace_id,
                    name=f"Additional voice {index}",
                    metadata_json={"voice_category": "unspecified"},
                )
            )
    nine_voice_policy_queries, nine_voice = query_policy(alice_client)
    after_nine_voice_read = domain_counts(case)
    assert after_nine_voice_read == (before_one[0] + 8, before_one[1], before_one[2])
    assert len(nine_voice.get_json()["items"]) == 9
    assert nine_voice.get_json()["workspace_id"] == workspace_id
    assert empty_policy_queries == one_voice_policy_queries == nine_voice_policy_queries
    assert alice_client.head(
        "/api/v1/voices", query_string={"workspace_id": workspace_id}
    ).status_code == 200
    assert domain_counts(case) == after_nine_voice_read
