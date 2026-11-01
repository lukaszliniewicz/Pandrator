"""Finite current-workspace provider routes and approved Member catalogues."""

from __future__ import annotations

import json
import socket
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from pandrator.logic.llm_handler import ModelDiscoveryResult
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext
from pandrator.web.access.workspace_policy import WorkspaceAction
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.provider_access import (
    member_model_catalogue,
    member_provider_catalogue,
    require_provider_workspace,
)
from pandrator.web.provider_settings import build_llm_settings
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_evidence_access_routes import body, headers, team
from tests.test_project_sharing_routes import login

SECRET = "synthetic-provider-workspace-secret"
PRIVATE_URL = "https://private-endpoint.invalid/v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Only synthetic provider boundaries are permitted")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def provider(client, csrf, *, workspace=None, label="Synthetic provider", enabled=True):
    query = {} if workspace is None else {"workspace_id": workspace}
    return body(
        client.post(
            "/api/v1/providers",
            query_string=query,
            json={
                "provider_key": "openai",
                "label": label,
                "base_url": PRIVATE_URL,
                "api_key": SECRET,
                "credential_backend": "database",
                "enabled": enabled,
                "options": {"is_custom": True, "private_configuration": "DO NOT LOAD"},
            },
            headers=csrf,
        ),
        201,
    )


def model(client, csrf, p, *, workspace=None, name="synthetic-model", default=True):
    return body(
        client.post(
            f"/api/v1/providers/{p['id']}/models",
            query_string={} if workspace is None else {"workspace_id": workspace},
            json={
                "model_id": name,
                "is_default": default,
                "input_modalities": ["text", "audio"],
                "options": {"private_configuration": "DO NOT LOAD"},
            },
            headers=csrf,
        ),
        201,
    )


def promote(case, members):
    current = body(members.owner.get(f"/api/v1/workspaces/{members.workspace_id}"))
    body(
        members.owner.patch(
            f"/api/v1/workspaces/{members.workspace_id}/members/{members.alice_id}",
            json={"role": "manager", "expected_revision": current["revision"]},
            headers=members.oh,
        )
    )


def scoped_token(case, user_id, scopes, *, workspace=None, project=None):
    record, raw = case.auth.create_api_token(
        "Provider scoped credential",
        user_id=user_id,
        scopes=frozenset(scopes),
        resource_grants=CredentialResources(
            workspace_ids=frozenset([workspace]) if workspace else frozenset(),
            project_ids=frozenset([project]) if project else frozenset(),
        ),
    )
    return record, {"Authorization": f"Bearer {raw}"}


@pytest.mark.parametrize("route", ["provider", "auxiliary"])
def test_credential_scope_without_write_stays_a_permission_error(case, monkeypatch, route):
    home = body(case.client.get("/api/v1/providers"))["workspace_id"]
    _, bearer = scoped_token(
        case,
        case.auth.initial_account().user_id,
        {"app.read", "app.credentials.write"},
        workspace=home,
    )

    def unexpected_preparation(*_args, **_kwargs):
        raise AssertionError("Denied writes cannot prepare credentials")

    monkeypatch.setattr(
        "pandrator.web.provider_routes.configure_credential_reference", unexpected_preparation
    )
    api = case.app.test_client()
    if route == "provider":
        response = api.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "Denied", "api_key": SECRET},
            headers=bearer,
        )
    else:
        response = api.put(
            "/api/v1/credentials/deepl", json={"api_key": SECRET}, headers=bearer
        )
    assert response.status_code == 403
    assert body(case.client.get("/api/v1/providers"))["items"] == []


@pytest.mark.parametrize("route", ["provider", "auxiliary"])
def test_storage_permission_error_retains_credential_unavailable_response(case, monkeypatch, route):
    def unavailable_storage(*_args, **_kwargs):
        raise PermissionError("Synthetic credential storage is unavailable")

    monkeypatch.setattr(
        "pandrator.web.provider_routes.configure_credential_reference", unavailable_storage
    )
    csrf = {"X-CSRF-Token": case.csrf}
    if route == "provider":
        response = case.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "Not saved", "api_key": SECRET},
            headers=csrf,
        )
    else:
        response = case.client.put(
            "/api/v1/credentials/deepl", json={"api_key": SECRET}, headers=csrf
        )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "credential_unavailable"
    assert body(case.client.get("/api/v1/providers"))["items"] == []


def test_solo_and_noninitial_home_create_genuine_provider_model_and_database_credential(
    case, monkeypatch
):
    initial = provider(case.client, {"X-CSRF-Token": case.csrf})
    assert body(case.client.get("/api/v1/providers"))["can_manage"] is True
    alice = new_account(case)
    new_account(case, "foreign-admin", "administrator")
    enable(case, monkeypatch)
    client, csrf = login(case, "alice")
    outsider, _ = login(case, "foreign-admin")
    p = provider(client, csrf)
    default = model(client, csrf, p)
    assert SECRET not in json.dumps(p)
    home = body(client.get("/api/v1/providers"))
    assert home["can_manage"] is True and home["items"][0]["id"] == p["id"]
    assert initial["id"] not in {item["id"] for item in home["items"]}
    src = body(
        client.post(
            "/api/v1/sessions",
            json={"name": "Own correction source", "workflow_kind": "subtitles"},
            headers=headers(csrf, "provider-own-source"),
        ),
        201,
    )
    with case.database.snapshot_session() as session:
        stored = session.get_one(m.Provider, p["id"])
        assert stored.workspace_id == home["workspace_id"]
        assert stored.workspace_id != session.get_one(m.Provider, initial["id"]).workspace_id
        assert session.get_one(m.ProviderModel, default["id"]).provider_id == p["id"]
        credentials = list(
            session.scalars(
                select(m.StoredCredential).where(
                    m.StoredCredential.workspace_id == stored.workspace_id
                )
            )
        )
        assert credentials and any(c.secret_value == SECRET for c in credentials)
    services = case.app.extensions["pandrator"]["services"]
    settings, selected = build_llm_settings(case.database, services.paths, session_id=src["id"])
    assert selected.endswith("/synthetic-model") and settings.workspace_id == home["workspace_id"]
    assert settings.provider_binding_id == p["id"]
    for foreign in (case.client, outsider):
        assert (
            foreign.get(
                "/api/v1/providers", query_string={"workspace_id": home["workspace_id"]}
            ).status_code
            == 404
        )
        assert foreign.get(f"/api/v1/providers/{p['id']}/models").status_code == 404
    updated = client.patch(
        f"/api/v1/providers/{p['id']}",
        json={"label": "Updated private"},
        headers={**csrf, "If-Match": '"1"'},
    )
    assert body(updated)["revision"] == 2
    assert (
        client.patch(
            f"/api/v1/providers/{p['id']}",
            json={"label": "Stale"},
            headers={**csrf, "If-Match": '"1"'},
        ).status_code
        == 409
    )
    assert client.get("/api/v1/providers", query_string={"workspace_id": ""}).status_code == 422
    assert client.get("/api/v1/credential-backends").status_code == 200
    assert client.get("/api/v1/providers/profiles").status_code == 200
    assert alice.user_id != case.auth.initial_account().user_id


def test_manager_connection_crud_and_member_minimal_catalogue_denies_all_management(
    case, monkeypatch
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    p = provider(members.owner, members.oh, workspace=wid)
    active = model(members.owner, members.oh, p, workspace=wid)
    inactive = model(members.owner, members.oh, p, workspace=wid, name="inactive", default=False)
    disabled = provider(members.owner, members.oh, workspace=wid, label="Disabled", enabled=False)
    before = body(members.owner.get("/api/v1/providers", query_string={"workspace_id": wid}))
    assert before["can_manage"] and any(
        item.get("base_url") == PRIVATE_URL for item in before["items"]
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Member path must not hydrate credentials/configuration")

    monkeypatch.setattr("pandrator.web.provider_routes._provider_payload", forbidden)
    monkeypatch.setattr("pandrator.web.provider_routes.resolve_provider_credential", forbidden)
    monkeypatch.setattr("pandrator.web.provider_routes.configure_credential_reference", forbidden)
    monkeypatch.setattr("pandrator.logic.llm_handler.discover_provider_models", forbidden)
    monkeypatch.setattr("pandrator.logic.llm_handler.chat_completion_with_metadata", forbidden)
    response = body(members.alice.get("/api/v1/providers", query_string={"workspace_id": wid}))
    assert response["can_manage"] is False and response["workspace_id"] == wid
    assert response["items"] == [
        {key: p[key] for key in ("id", "kind", "provider_key", "label", "enabled", "revision")}
        | {"options_json": {"is_custom": True}}
    ]
    catalogue = body(
        members.alice.get(f"/api/v1/providers/{p['id']}/models", query_string={"workspace_id": wid})
    )
    assert [item["id"] for item in catalogue["items"]] == [active["id"]]
    assert (
        catalogue["items"][0]["options_json"] == {}
        and catalogue["items"][0]["supports_audio_input"]
    )
    assert PRIVATE_URL not in json.dumps(catalogue) and "DO NOT LOAD" not in json.dumps(response)
    assert (
        members.alice.get(
            f"/api/v1/providers/{disabled['id']}/models", query_string={"workspace_id": wid}
        ).status_code
        == 404
    )
    operations = [
        ("post", "/api/v1/providers", {"provider_key": "openai", "label": "Denied"}),
        ("patch", f"/api/v1/providers/{p['id']}", {"label": "Denied"}),
        ("delete", f"/api/v1/providers/{p['id']}", {}),
        ("post", f"/api/v1/providers/{p['id']}/models", {"model_id": "denied"}),
        ("patch", f"/api/v1/providers/{p['id']}/models/{inactive['id']}", {"is_active": True}),
        ("delete", f"/api/v1/providers/{p['id']}/models/{inactive['id']}", {}),
        ("post", f"/api/v1/providers/{p['id']}/test", {}),
        ("post", f"/api/v1/providers/{p['id']}/models/refresh", {}),
        ("put", "/api/v1/credentials/jina", {"api_key": SECRET, "credential_backend": "database"}),
    ]
    for method, url, payload in operations:
        rejected = getattr(members.alice, method)(
            url,
            query_string={"workspace_id": wid},
            json=payload,
            headers={**members.ah, "If-Match": '"1"'},
        )
        assert rejected.status_code == 404, (url, rejected.get_json())
    assert (
        members.alice.get("/api/v1/credentials", query_string={"workspace_id": wid}).status_code
        == 404
    )

    with sql_capture(case.database) as get_sql:
        assert (
            members.alice.get("/api/v1/providers", query_string={"workspace_id": wid}).status_code
            == 200
        )
        assert (
            members.alice.get(
                f"/api/v1/providers/{p['id']}/models", query_string={"workspace_id": wid}
            ).status_code
            == 200
        )
    for statement, _parameters in get_sql:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            assert not any(
                name in statement.lower()
                for name in (
                    "providers",
                    "provider_models",
                    "stored_credentials",
                    "workspace_settings",
                )
            )


def test_readonly_manager_and_project_credential_ceilings_and_session_catalogue(case, monkeypatch):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    p = provider(members.owner, members.oh, workspace=wid)
    model(members.owner, members.oh, p, workspace=wid)
    owner_id = case.auth.initial_account().user_id
    _, read = scoped_token(case, owner_id, {"app.read"}, workspace=wid)
    api = case.app.test_client()
    config = body(api.get("/api/v1/providers", query_string={"workspace_id": wid}, headers=read))
    assert config["can_manage"] is False and config["items"][0]["base_url"] == PRIVATE_URL
    assert config["items"][0]["credential_backend"] == "database" and SECRET not in json.dumps(
        config
    )
    assert (
        api.post(
            "/api/v1/providers",
            query_string={"workspace_id": wid},
            json={"provider_key": "openai", "label": "Denied"},
            headers=read,
        ).status_code
        == 403
    )
    assert (
        api.post(
            f"/api/v1/providers/{p['id']}/models/refresh",
            query_string={"workspace_id": wid},
            json={},
            headers=read,
        ).status_code
        == 403
    )
    source = body(
        members.alice.post(
            "/api/v1/sessions",
            json={"name": "Team source", "workspace_id": wid},
            headers=headers(members.ah, "provider-team-source"),
        ),
        201,
    )
    for path in ("/api/v1/providers", f"/api/v1/providers/{p['id']}/models"):
        assert (
            body(members.alice.get(path, query_string={"session_id": source["id"]}))["workspace_id"]
            == wid
        )
        assert (
            members.alice.get(
                path,
                query_string={
                    "session_id": source["id"],
                    "workspace_id": body(members.alice.get("/api/v1/providers"))["workspace_id"],
                },
            ).status_code
            == 404
        )
    _, project = scoped_token(case, members.alice_id, {"app.read"}, project=source["project_id"])
    assert (
        api.get(
            "/api/v1/providers", query_string={"session_id": source["id"]}, headers=project
        ).status_code
        == 404
    )
    assert (
        api.get(
            "/api/v1/providers", query_string={"workspace_id": wid}, headers=project
        ).status_code
        == 404
    )
    other = provider(members.alice, members.ah, label="Other home")
    other_model = model(members.alice, members.ah, other)
    assert (
        members.owner.patch(
            f"/api/v1/providers/{p['id']}/models/{other_model['id']}",
            query_string={"workspace_id": wid},
            json={"is_active": True},
            headers={**members.oh, "If-Match": '"1"'},
        ).status_code
        == 404
    )
    assert (
        members.owner.delete(
            f"/api/v1/providers/{p['id']}/models/{other_model['id']}",
            query_string={"workspace_id": wid},
            json={},
            headers=members.oh,
        ).status_code
        == 404
    )


@pytest.mark.parametrize("loss", ["membership", "role", "reset"])
def test_identical_management_request_denies_after_current_authority_loss(case, monkeypatch, loss):
    members = team(case, monkeypatch)
    promote(case, members)
    wid = members.workspace_id
    p = provider(members.alice, members.ah, workspace=wid)
    url = f"/api/v1/providers/{p['id']}"
    request_headers = {**members.ah, "If-Match": '"1"'}
    with case.database.immediate_session() as session:
        if loss == "membership":
            session.delete(session.get_one(m.WorkspaceMembership, (wid, members.alice_id)))
        elif loss == "role":
            session.get_one(m.WorkspaceMembership, (wid, members.alice_id)).role = "member"
        else:
            session.get_one(m.UserCredential, members.alice_id).auth_revision += 1
    denied = members.alice.patch(
        url,
        query_string={"workspace_id": wid},
        json={"label": "Denied after loss"},
        headers=request_headers,
    )
    assert denied.status_code == (401 if loss == "reset" else 404), denied.get_json()
    with case.database.snapshot_session() as session:
        assert session.get_one(m.Provider, p["id"]).revision == 1


@pytest.mark.parametrize(
    ("operation", "lost_scope"),
    [("test", "app.run"), ("refresh", None), ("test", "app.credentials.write")],
)
def test_external_results_reprove_current_scope_and_manager_before_return_or_publish(
    case, monkeypatch, operation, lost_scope
):
    members = team(case, monkeypatch)
    promote(case, members)
    wid = members.workspace_id
    p = provider(members.alice, members.ah, workspace=wid)
    model(members.alice, members.ah, p, workspace=wid)
    token, bearer = scoped_token(
        case,
        members.alice_id,
        {"app.read", "app.write", "app.run", "app.credentials.write"},
        workspace=wid,
    )
    api = case.app.test_client()

    boundary_calls = []

    def boundary(*_args, **_kwargs):
        boundary_calls.append(operation)
        with case.database.immediate_session(busy_timeout_ms=50) as session:
            if operation == "test":
                session.get_one(m.ApiToken, token.id).scopes_json = sorted(
                    {"app.read", "app.write", "app.credentials.write", "app.run"} - {lost_scope}
                )
            else:
                session.delete(session.get_one(m.WorkspaceMembership, (wid, members.alice_id)))
        if operation == "test":
            return SimpleNamespace(
                content="PRIVATE TEST RESPONSE",
                model="synthetic-model",
                cost=None,
                cost_source=None,
            )
        return ModelDiscoveryResult(models=("newly-discovered",), source="api")

    target = "chat_completion_with_metadata" if operation == "test" else "discover_provider_models"
    monkeypatch.setattr("pandrator.logic.llm_handler." + target, boundary)
    suffix = "test" if operation == "test" else "models/refresh"
    response = api.post(
        f"/api/v1/providers/{p['id']}/{suffix}",
        query_string={"workspace_id": wid},
        json={},
        headers=bearer,
    )
    assert boundary_calls == [operation]
    assert response.status_code == (403 if operation == "test" else 404), response.get_json()
    assert "PRIVATE TEST RESPONSE" not in response.get_data(as_text=True)
    with case.database.snapshot_session() as session:
        assert list(
            session.scalars(
                select(m.ProviderModel.model_id).where(m.ProviderModel.provider_id == p["id"])
            )
        ) == ["synthetic-model"]


@contextmanager
def sql_capture(database):
    captured = []

    def collect(_connection, _cursor, statement, parameters, _context, _many):
        captured.append((statement, parameters))

    event.listen(database.engine, "before_cursor_execute", collect)
    try:
        yield captured
    finally:
        event.remove(database.engine, "before_cursor_execute", collect)


def test_member_sql_stays_fixed_and_selects_no_private_configuration(case, monkeypatch, tmp_path):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    p = provider(members.owner, members.oh, workspace=wid)
    model(members.owner, members.oh, p, workspace=wid)
    _token, bearer = scoped_token(case, members.alice_id, {"app.read"}, workspace=wid)
    principal = case.auth.resolve_api_token(
        bearer["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=case.app.extensions["pandrator"]["services"]
        .identity.snapshot(observed_origin="http://localhost")
        .instance_id,
    )
    assert principal is not None
    from pandrator.web.accounts.execution_actor import actor_from_principal

    actor = actor_from_principal(principal)
    assert actor is not None
    context = AccessContext(actor)

    def catalogue():
        with sql_capture(case.database) as sql, case.database.snapshot_session() as session:
            access = require_provider_workspace(session, context, wid)
            assert WorkspaceAction.MANAGE_READ not in access.allowed_actions
            providers = member_provider_catalogue(session, access.id)
            models = member_model_catalogue(session, p["id"], access.id)
        return sql, providers, models

    first, _, _ = catalogue()
    for index in range(8):
        provider(members.owner, members.oh, workspace=wid, label=f"Additional {index}")
    sql, providers, models = catalogue()
    assert len(sql) == len(first) and len(providers) == 9 and len(models) == 1
    for statement, _parameters in sql:
        assert not statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
        assert "providers.base_url" not in statement and "providers.secret_ref" not in statement
        assert "provider_models.options_json" not in statement
        if "providers.options_json" in statement:
            assert "JSON_EXTRACT" in statement
    provider_queries = [statement for statement, _ in sql if "FROM providers" in statement]
    assert len(provider_queries) == 1
    summary = {
        "provider_rows": 9,
        "single_provider_statements": len(first),
        "nine_provider_statements": len(sql),
        "provider_projection": provider_queries[0],
        "model_projection": next(
            statement for statement, _ in sql if "FROM provider_models" in statement
        ),
        "max_binds": max(len(parameters) for _, parameters in sql),
        "writes": 0,
        "measurement": "Reader/policy after genuine credential creation; excludes unchanged HTTP authentication last-used bookkeeping.",
    }
    (tmp_path / "bounded-sql.json").write_text(json.dumps(summary, indent=2))


@pytest.mark.parametrize("operation", ["test", "refresh"])
def test_authority_lost_during_real_hydration_stops_before_external_effect(
    case, monkeypatch, operation
):
    from pandrator.web import provider_routes, provider_settings

    members = team(case, monkeypatch)
    promote(case, members)
    wid = members.workspace_id
    p = provider(members.alice, members.ah, workspace=wid)
    model(members.alice, members.ah, p, workspace=wid)
    token, bearer = scoped_token(
        case,
        members.alice_id,
        {"app.read", "app.write", "app.run", "app.credentials.write"},
        workspace=wid,
    )
    api = case.app.test_client()
    hydrated = []
    external = []
    original = (
        provider_settings.build_llm_settings
        if operation == "test"
        else provider_routes.resolve_provider_credential
    )

    def hydrate_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        if operation == "test":
            assert isinstance(result, tuple)
            settings, _selected = result
            assert settings.workspace_id == wid and settings.provider_binding_id == p["id"]
            assert str(settings.provider_configs[0]["api_key"]) == SECRET
        else:
            assert not isinstance(result, tuple)
            assert result.source == "database" and result.value == SECRET
        hydrated.append(operation)
        with case.database.immediate_session(busy_timeout_ms=50) as session:
            current = session.get_one(m.ApiToken, token.id)
            if operation == "test":
                current.scopes_json = ["app.read", "app.write", "app.credentials.write"]
            else:
                current.revoked_at = m.utcnow()
        return result

    def forbidden_effect(*_args, **_kwargs):
        external.append(operation)
        raise AssertionError("Revoked hydrated authority must not start an external effect")

    if operation == "test":
        monkeypatch.setattr(provider_settings, "build_llm_settings", hydrate_then_revoke)
        monkeypatch.setattr(
            "pandrator.logic.llm_handler.chat_completion_with_metadata", forbidden_effect
        )
    else:
        monkeypatch.setattr(provider_routes, "resolve_provider_credential", hydrate_then_revoke)
        monkeypatch.setattr(
            "pandrator.logic.llm_handler.discover_provider_models", forbidden_effect
        )
    suffix = "test" if operation == "test" else "models/refresh"
    with sql_capture(case.database) as sql:
        response = api.post(
            f"/api/v1/providers/{p['id']}/{suffix}",
            query_string={"workspace_id": wid},
            json={},
            headers=bearer,
        )
    assert hydrated == [operation] and external == []
    assert response.status_code == 403, response.get_json()
    assert SECRET not in response.get_data(as_text=True)
    for statement, _parameters in sql:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            assert "provider_models" not in statement.lower()
    with case.database.snapshot_session() as session:
        assert list(
            session.scalars(
                select(m.ProviderModel.model_id).where(m.ProviderModel.provider_id == p["id"])
            )
        ) == ["synthetic-model"]


def test_management_metadata_respects_credential_write_and_workspace_backend_policy(
    case, monkeypatch
):
    solo = body(case.client.get("/api/v1/providers"))
    assert solo["credential_backends"] == ["database", "environment", "keyring", "file"]
    alice = new_account(case)
    enable(case, monkeypatch)
    client, csrf = login(case, "alice")
    p = provider(client, csrf)
    model(client, csrf, p)
    private = body(client.get("/api/v1/providers"))
    home = private["workspace_id"]
    assert private["credential_backends"] == ["database"] and private["can_manage"] is True
    assert body(client.get("/api/v1/credentials"))["credential_backends"] == ["database"]
    src = body(
        client.post(
            "/api/v1/sessions",
            json={"name": "Private provider source", "workflow_kind": "subtitles"},
            headers=headers(csrf, "provider-metadata-source"),
        ),
        201,
    )
    _, bearer = scoped_token(case, alice.user_id, {"app.read", "app.write"}, workspace=home)
    api = case.app.test_client()
    catalogue = body(
        api.get("/api/v1/providers", query_string={"session_id": src["id"]}, headers=bearer)
    )
    assert catalogue["workspace_id"] == home and catalogue["credential_backends"] == ["database"]
    assert catalogue["can_manage"] is False
    assert catalogue["items"][0]["base_url"] == PRIVATE_URL
    assert catalogue["items"][0]["credential_backend"] == "database"
    assert SECRET not in json.dumps(catalogue)
    models = body(
        api.get(
            f"/api/v1/providers/{p['id']}/models",
            query_string={"session_id": src["id"]},
            headers=bearer,
        )
    )
    assert models["can_manage"] is False and len(models["items"]) == 1
