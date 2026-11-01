"""Evidence route catalogues obey project and independent workspace rights."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.models import Provider, ProviderModel
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import PASSWORD, enable, new_account
from tests.test_provider_workspace_routes import provider_case as provider_case  # noqa: F401


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked_connect(self: socket.socket, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Network access is blocked in this acceptance test.")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)


def _stub_readiness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"local": 0, "catalogues": []}

    def local_probe() -> dict[str, Any]:
        calls["local"] += 1
        return {}

    def catalogue(
        _database: Any,
        _paths: Any,
        *,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        calls["catalogues"].append(
            {"session_id": session_id, "workspace_id": workspace_id}
        )
        return {"profiles": []}, 1

    monkeypatch.setattr(
        "pandrator.web.subtitle_evidence._cached_stt_backend_statuses", local_probe
    )
    monkeypatch.setattr(
        "pandrator.web.subtitle_evidence.stt_catalogue_snapshot", catalogue
    )
    return calls


@contextmanager
def _capture_provider_sql(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        lowered = statement.lower()
        if "providers" in lowered or "provider_models" in lowered:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _add_model(
    database: Any,
    workspace_id: str,
    *,
    label: str,
    model_id: str,
    modalities: list[str],
    kind: str = "llm",
    enabled: bool = True,
    active: bool = True,
    default: bool = False,
) -> tuple[str, str]:
    with database.immediate_session() as session:
        provider = Provider(
            workspace_id=workspace_id,
            kind=kind,
            provider_key="synthetic",
            label=label,
            enabled=enabled,
            base_url="https://private.invalid/v1",
            secret_ref="keyring:synthetic/private-provider-key",
            options_json={"is_custom": True, "private_marker": "never-return"},
        )
        session.add(provider)
        session.flush()
        model = ProviderModel(
            provider_id=provider.id,
            model_id=model_id,
            input_modalities_json=modalities,
            is_active=active,
            is_default=default,
        )
        session.add(model)
        session.flush()
        return provider.id, model.id


def _login(case: Any, login_name: str) -> tuple[Any, dict[str, str], str]:
    client = case.app.test_client()
    response = client.post(
        "/api/v1/auth/login",
        json={"login_name": login_name, "password": PASSWORD},
    )
    assert response.status_code == 200, response.get_json()
    principal = client.get("/api/v1/auth/status").get_json()["principal"]
    assert principal is not None
    return (
        client,
        {"X-CSRF-Token": response.get_json()["csrf_token"]},
        principal["user_id"],
    )


def _mutation_headers(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def _create_session(
    client: Any,
    headers: dict[str, str],
    *,
    name: str,
    workspace_id: str | None = None,
) -> tuple[str, str]:
    payload: dict[str, Any] = {
        "name": name,
        "workflow_kind": "subtitles",
        "source_language": "en",
    }
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    idempotency_key = f"create-{name.lower().replace(' ', '-')}"
    response = client.post(
        "/api/v1/sessions",
        json=payload,
        headers=_mutation_headers(headers, idempotency_key),
    )
    assert response.status_code == 201, response.get_json()
    session_id = response.get_json()["id"]
    return session_id, response.get_json()["project_id"]


def _personal_workspace_id(database: Any, user_id: str) -> str:
    with database.snapshot_session() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(
                Workspace.kind == "personal",
                Workspace.personal_owner_user_id == user_id,
            )
        )
    assert workspace_id is not None
    return workspace_id


def test_solo_catalogue_uses_personal_audio_models_and_one_safe_join(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = request.getfixturevalue("provider_case")
    _block_network(monkeypatch)
    readiness = _stub_readiness(monkeypatch)
    own_workspace = case.workspaces["personal"]

    _, own_model = _add_model(
        case.database,
        own_workspace,
        label="Personal audio",
        model_id="personal-audio",
        modalities=["text", "audio"],
        active=True,
    )
    _, default_model = _add_model(
        case.database,
        own_workspace,
        label="Default audio",
        model_id="default-audio",
        modalities=["audio"],
        active=False,
        default=True,
    )
    _add_model(
        case.database,
        own_workspace,
        label="Text only",
        model_id="text-only",
        modalities=["text"],
    )
    _add_model(
        case.database,
        own_workspace,
        label="Inactive audio",
        model_id="inactive-audio",
        modalities=["audio"],
        active=False,
    )
    _add_model(
        case.database,
        own_workspace,
        label="Disabled audio",
        model_id="disabled-audio",
        modalities=["audio"],
        enabled=False,
    )
    _add_model(
        case.database,
        own_workspace,
        label="Non LLM audio",
        model_id="non-llm-audio",
        modalities=["audio"],
        kind="stt",
    )
    _add_model(
        case.database,
        own_workspace,
        label="Audio derived text",
        model_id="audio-derived-text",
        modalities=["text", "audio_embedding"],
    )
    _add_model(
        case.database,
        case.workspaces["alpha"],
        label="Foreign audio",
        model_id="foreign-audio",
        modalities=["audio"],
    )

    with _capture_provider_sql(case.database.engine) as statements:
        response = case.client.get("/api/v1/subtitle-evidence/routes")

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert set(body) == {
        "schema_version",
        "language",
        "routes",
        "audio_models",
        "can_select_audio_models",
    }
    assert body["schema_version"] == "1"
    assert body["can_select_audio_models"] is True
    assert body["audio_models"] == [
        {
            "id": default_model,
            "model_id": "default-audio",
            "provider_label": "Default audio",
            "custom": True,
        },
        {
            "id": own_model,
            "model_id": "personal-audio",
            "provider_label": "Personal audio",
            "custom": True,
        }
    ]
    assert "private-provider-key" not in response.get_data(as_text=True)
    assert "private.invalid" not in response.get_data(as_text=True)
    assert readiness["local"] == 1
    assert readiness["catalogues"] == [
        {"session_id": None, "workspace_id": own_workspace}
    ]
    assert len(statements) == 1
    sql = statements[0].lower()
    assert "provider_models" in sql and "join providers" in sql
    assert "secret_ref" not in sql and "base_url" not in sql
    assert "providers.*" not in sql and "provider_models.*" not in sql


def test_collaboration_requires_project_read_and_separate_workspace_read(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_network(monkeypatch)
    readiness = _stub_readiness(monkeypatch)
    alice_account = new_account(case, login="alice")
    enable(case, monkeypatch)
    owner, owner_headers, owner_id = _login(case, "owner")
    alice, alice_headers, alice_id = _login(case, "alice")
    assert owner_id != alice_id

    source_id, project_id = _create_session(
        alice, alice_headers, name="Alice private evidence"
    )
    alice_workspace = _personal_workspace_id(case.database, alice_id)
    _add_model(
        case.database,
        alice_workspace,
        label="Alice audio",
        model_id="alice-audio",
        modalities=["audio"],
    )
    owner_workspace = _personal_workspace_id(case.database, owner_id)
    _add_model(
        case.database,
        owner_workspace,
        label="Owner private audio",
        model_id="owner-audio",
        modalities=["audio"],
    )

    assert owner.get(
        f"/api/v1/subtitle-evidence/routes?session_id={source_id}"
    ).status_code == 404
    assert owner.get(
        "/api/v1/subtitle-evidence/routes?session_id=missing-session"
    ).status_code == 404
    assert owner.get("/api/v1/subtitle-evidence/routes?session_id=").status_code == 422
    assert owner.get(
        f"/api/v1/subtitle-evidence/routes?session_id={'x' * 81}"
    ).status_code == 422

    with _capture_provider_sql(case.database.engine) as statements:
        response = owner.get("/api/v1/subtitle-evidence/routes")
    assert response.status_code == 200, response.get_json()
    no_session = response.get_json()
    assert no_session["audio_models"] == []
    assert no_session["can_select_audio_models"] is False
    assert all(route["ready"] is None for route in no_session["routes"])
    assert statements == []
    assert readiness == {"local": 0, "catalogues": []}

    alice_response = alice.get(
        f"/api/v1/subtitle-evidence/routes?session_id={source_id}"
    )
    assert alice_response.status_code == 200, alice_response.get_json()
    assert [model["model_id"] for model in alice_response.get_json()["audio_models"]] == [
        "alice-audio"
    ]
    assert readiness["catalogues"] == [
        {"session_id": None, "workspace_id": alice_workspace}
    ]

    grant = alice.put(
        f"/api/v1/projects/{project_id}/viewers/{owner_id}",
        json={"expected_revision": 1},
        headers=alice_headers,
    )
    assert grant.status_code == 200, grant.get_json()
    readiness_before_shared_read = {
        "local": readiness["local"],
        "catalogues": list(readiness["catalogues"]),
    }
    with _capture_provider_sql(case.database.engine) as statements:
        shared = owner.get(
            f"/api/v1/subtitle-evidence/routes?session_id={source_id}"
        )
    assert shared.status_code == 200, shared.get_json()
    assert shared.get_json()["audio_models"] == []
    assert shared.get_json()["can_select_audio_models"] is False
    assert all(route["ready"] is None for route in shared.get_json()["routes"])
    assert statements == []
    assert readiness == readiness_before_shared_read

    revoked = alice.post(
        f"/api/v1/projects/{project_id}/viewers/{owner_id}/revoke",
        json={"expected_revision": grant.get_json()["revision"]},
        headers=alice_headers,
    )
    assert revoked.status_code == 200, revoked.get_json()
    assert owner.get(
        f"/api/v1/subtitle-evidence/routes?session_id={source_id}"
    ).status_code == 404
    assert alice_account.user_id == alice_id


def test_team_catalogue_requires_workspace_grant_even_with_project_token_grant(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_network(monkeypatch)
    readiness = _stub_readiness(monkeypatch)
    new_account(case, login="alice")
    enable(case, monkeypatch)
    owner, owner_headers, owner_id = _login(case, "owner")
    alice, alice_headers, alice_id = _login(case, "alice")

    workspace_response = owner.post(
        "/api/v1/workspaces",
        json={"name": "Evidence team"},
        headers=_mutation_headers(owner_headers, "evidence-team-create"),
    )
    assert workspace_response.status_code == 201, workspace_response.get_json()
    assert workspace_response.get_json()["role"] == "manager"
    team_id = workspace_response.get_json()["id"]
    invitation = owner.post(
        f"/api/v1/workspaces/{team_id}/invitations",
        json={"user_id": alice_id, "role": "member", "expected_revision": 1},
        headers=_mutation_headers(owner_headers, "evidence-team-invite"),
    )
    assert invitation.status_code == 201, invitation.get_json()
    accepted = alice.post(
        f"/api/v1/workspace-invitations/{invitation.get_json()['id']}/accept",
        json={},
        headers=_mutation_headers(alice_headers, "evidence-team-accept"),
    )
    assert accepted.status_code == 200, accepted.get_json()

    with case.database.snapshot_session() as session:
        assert session.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.workspace_id == team_id,
                WorkspaceMembership.user_id == owner_id,
            )
        ) == "admin"
        assert session.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.workspace_id == team_id,
                WorkspaceMembership.user_id == alice_id,
            )
        ) == "member"

    source_id, project_id = _create_session(
        owner,
        owner_headers,
        name="Team evidence source",
        workspace_id=team_id,
    )
    _, team_model = _add_model(
        case.database,
        team_id,
        label="Team audio",
        model_id="team-audio",
        modalities=["audio"],
    )
    _add_model(
        case.database,
        _personal_workspace_id(case.database, alice_id),
        label="Alice personal audio",
        model_id="alice-personal-audio",
        modalities=["audio"],
    )
    _add_model(
        case.database,
        _personal_workspace_id(case.database, owner_id),
        label="Owner personal audio",
        model_id="owner-personal-audio",
        modalities=["audio"],
    )

    for client in (owner, alice):
        response = client.get(
            f"/api/v1/subtitle-evidence/routes?session_id={source_id}"
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["can_select_audio_models"] is True
        assert [model["id"] for model in body["audio_models"]] == [team_model]

    with case.database.snapshot_session() as session:
        current_auth_revision = session.get(UserCredential, alice_id).auth_revision
    token, raw_token = case.auth.create_api_token(
        "Evidence project-only member token",
        scopes=frozenset({"app.read"}),
        user_id=alice_id,
        auth_revision=current_auth_revision,
        resource_grants=CredentialResources(project_ids=frozenset({project_id})),
    )
    readiness_before_project_only = {
        "local": readiness["local"],
        "catalogues": list(readiness["catalogues"]),
    }
    with _capture_provider_sql(case.database.engine) as project_only_sql:
        project_only = case.app.test_client().get(
            f"/api/v1/subtitle-evidence/routes?session_id={source_id}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert project_only.status_code == 200, project_only.get_json()
    assert project_only.get_json()["audio_models"] == []
    assert project_only.get_json()["can_select_audio_models"] is False
    assert all(route["ready"] is None for route in project_only.get_json()["routes"])
    assert project_only_sql == []
    assert readiness == readiness_before_project_only

    with case.database.immediate_session() as session:
        session.add(
            ApiTokenWorkspaceGrant(token_id=token.id, workspace_id=team_id)
        )
    with _capture_provider_sql(case.database.engine) as statements:
        workspace_granted = case.app.test_client().get(
            f"/api/v1/subtitle-evidence/routes?session_id={source_id}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert workspace_granted.status_code == 200, workspace_granted.get_json()
    assert workspace_granted.get_json()["can_select_audio_models"] is True
    assert [model["id"] for model in workspace_granted.get_json()["audio_models"]] == [
        team_model
    ]
    assert len(statements) == 1
    assert readiness["catalogues"][-1] == {
        "session_id": None,
        "workspace_id": team_id,
    }
