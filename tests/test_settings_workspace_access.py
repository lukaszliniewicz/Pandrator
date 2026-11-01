"""Actual settings/default HTTP workspace and installation authority."""

from __future__ import annotations

import json
import socket

import pytest
from sqlalchemy import func, select

from pandrator.web import global_settings
from pandrator.web import models as m
from pandrator.web.settings_access import SettingsScopeInvalid
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_evidence_access_routes import body, headers, team
from tests.test_project_sharing_routes import login
from tests.test_provider_workspace_access import promote, scoped_token

SECRET = "synthetic-settings-workspace-secret"
SERVICE_VALUE = {
    "provider_configs": [{"id": "openai", "api_key": SECRET, "credential_backend": "database"}]
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Workspace settings qualification is local only")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def put(client, csrf, key, value, *, revision=0, workspace=None, query=None):
    values = {} if workspace is None else {"workspace_id": workspace}
    if query:
        values.update(query)
    return client.put(
        "/api/v1/settings/" + key,
        json={"value": value},
        query_string=values,
        headers={**csrf, "If-Match": f'"{revision}"'},
    )


def counts(case):
    with case.database.snapshot_session() as session:
        return [
            session.scalar(select(func.count()).select_from(table))
            for table in (
                m.AppSetting,
                m.AppSettingHistory,
                m.WorkspaceSetting,
                m.WorkspaceSettingHistory,
                m.StoredCredential,
            )
        ]


def test_solo_and_noninitial_own_defaults_services_leave_initial_and_foreign_unchanged(
    case, monkeypatch
):
    initial = body(case.client.get("/api/v1/defaults/tts"))
    services = case.app.extensions["pandrator"]["services"]
    trusted = services.global_settings.defaults("tts")
    assert "setting_scope" not in trusted and "workspace_id" not in trusted
    new_account(case)
    enable(case, monkeypatch)
    client, csrf = login(case, "alice")
    own = body(client.get("/api/v1/defaults/tts"))
    assert (
        own["workspace_id"] != initial["workspace_id"]
        and own["setting_scope"] == "workspace"
        and own["can_manage"]
    )
    changed = put(client, csrf, "defaults.tts", {"max_attempts": 2})
    assert body(changed)["value"]["max_attempts"] == 2 and changed.headers["ETag"] == '"1"'
    saved = body(put(client, csrf, "services.tts", SERVICE_VALUE))
    assert saved["workspace_id"] == own["workspace_id"] and saved["can_manage"]
    assert SECRET not in json.dumps(saved)
    assert body(client.get("/api/v1/settings/services.tts"))["value"] == saved["value"]
    assert services.global_settings.defaults("tts")["value"] == trusted["value"]
    assert (
        services.global_settings.defaults("tts", workspace_id=own["workspace_id"])["value"][
            "max_attempts"
        ]
        == 2
    )
    assert "setting_scope" not in services.global_settings.get(
        "services.tts", workspace_id=own["workspace_id"]
    )
    assert body(case.client.get("/api/v1/defaults/tts"))["value"] == initial["value"]
    assert (
        case.client.get(
            "/api/v1/defaults/tts", query_string={"workspace_id": own["workspace_id"]}
        ).status_code
        == 404
    )
    denied = put(
        client,
        csrf,
        "services.tts",
        {
            "provider_configs": [
                {
                    "id": "openai",
                    "credential_backend": "environment",
                    "credential_reference": "OPENAI_API_KEY",
                }
            ]
        },
        revision=1,
    )
    assert denied.status_code == 422
    assert body(client.get("/api/v1/settings/services.tts"))["revision"] == 1
    with pytest.raises(SettingsScopeInvalid):
        services.global_settings.defaults("tts", session_id="unproved")

    def file_denied(*_args, **_kwargs):
        raise PermissionError("Synthetic credential file permission failure")

    monkeypatch.setattr(global_settings, "prepare_tts_settings_for_storage", file_denied)
    failure = put(client, csrf, "services.tts", SERVICE_VALUE, revision=1)
    assert body(failure, 422)["error"]["code"] == "credential_unavailable"


def test_team_member_approved_defaults_reads_manager_only_raw_and_all_writes_denied(
    case, monkeypatch
):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    saved = body(put(members.owner, members.oh, "defaults.tts", {"max_attempts": 3}, workspace=wid))
    body(put(members.owner, members.oh, "wizard", {"private": "manager config"}, workspace=wid))
    src = body(
        members.alice.post(
            "/api/v1/sessions",
            json={"name": "Team settings source", "workspace_id": wid},
            headers=headers(members.ah, "settings-team-source"),
        ),
        201,
    )
    direct = body(members.alice.get("/api/v1/defaults/tts", query_string={"workspace_id": wid}))
    rooted = body(members.alice.get("/api/v1/defaults/tts", query_string={"session_id": src["id"]}))
    assert direct["value"] == rooted["value"] == saved["value"] and not direct["can_manage"]
    assert (
        body(
            members.alice.get("/api/v1/settings/defaults.tts", query_string={"workspace_id": wid})
        )["value"]
        == saved["value"]
    )
    assert (
        members.alice.get("/api/v1/settings/wizard", query_string={"workspace_id": wid}).status_code
        == 404
    )
    before = counts(case)
    for key, value in [
        ("defaults.tts", {"max_attempts": 4}),
        ("wizard", {"x": 1}),
        ("services.tts", SERVICE_VALUE),
    ]:
        assert (
            put(members.alice, members.ah, key, value, revision=1, workspace=wid).status_code == 404
        )
    assert counts(case) == before
    new_account(case, "foreign-admin", "administrator")
    admin, _ = login(case, "foreign-admin")
    assert admin.get("/api/v1/defaults/tts", query_string={"workspace_id": wid}).status_code == 404
    conflict = put(
        members.owner, members.oh, "defaults.tts", {"max_attempts": 4}, revision=0, workspace=wid
    )
    assert body(conflict, 409)["error"]["code"] == "revision_conflict"
    assert (
        body(
            put(
                members.owner,
                members.oh,
                "defaults.tts",
                {"max_attempts": 4},
                revision=1,
                workspace=wid,
            )
        )["revision"]
        == 2
    )


def test_readonly_service_flags_project_ceilings_and_scope_validation(case, monkeypatch):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    body(put(members.owner, members.oh, "defaults.tts", {"max_attempts": 3}, workspace=wid))
    body(put(members.owner, members.oh, "services.tts", {"provider_configs": []}, workspace=wid))
    owner_id = case.auth.initial_account().user_id
    _, read = scoped_token(case, owner_id, {"app.read"}, workspace=wid)
    api = case.app.test_client()
    assert (
        body(api.get("/api/v1/defaults/tts", query_string={"workspace_id": wid}, headers=read))[
            "can_manage"
        ]
        is False
    )
    assert (
        body(
            api.get(
                "/api/v1/settings/services.tts", query_string={"workspace_id": wid}, headers=read
            )
        )["can_manage"]
        is False
    )
    assert put(api, read, "defaults.tts", {}, revision=1, workspace=wid).status_code == 403
    _, write = scoped_token(case, owner_id, {"app.read", "app.write"}, workspace=wid)
    assert (
        body(
            api.get(
                "/api/v1/settings/services.tts", query_string={"workspace_id": wid}, headers=write
            )
        )["can_manage"]
        is False
    )
    src = body(
        members.alice.post(
            "/api/v1/sessions",
            json={"name": "Scoped settings source", "workspace_id": wid},
            headers=headers(members.ah, "settings-scope-source"),
        ),
        201,
    )
    _, project = scoped_token(case, members.alice_id, {"app.read"}, project=src["project_id"])
    assert (
        api.get(
            "/api/v1/defaults/tts", query_string={"session_id": src["id"]}, headers=project
        ).status_code
        == 404
    )
    assert (
        api.get(
            "/api/v1/defaults/tts", query_string={"workspace_id": wid}, headers=project
        ).status_code
        == 404
    )
    personal = body(members.alice.get("/api/v1/defaults/tts"))["workspace_id"]
    assert (
        members.alice.get(
            "/api/v1/defaults/tts", query_string={"session_id": src["id"], "workspace_id": personal}
        ).status_code
        == 404
    )
    for invalid in ("", " ", "x" * 81):
        assert (
            members.owner.get(
                "/api/v1/defaults/tts", query_string={"workspace_id": invalid}
            ).status_code
            == 422
        )
        assert (
            members.owner.get(
                "/api/v1/defaults/tts", query_string={"session_id": invalid}
            ).status_code
            == 422
        )
    assert (
        members.owner.get(
            "/api/v1/settings/defaults.tts", query_string={"session_id": src["id"]}
        ).status_code
        == 422
    )
    assert (
        put(
            members.owner, members.oh, "defaults.tts", {}, workspace=wid, query={"session_id": ""}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("loss", ["membership", "reset", "scope"])
def test_current_loss_denies_identical_later_mutation(case, monkeypatch, loss):
    members = team(case, monkeypatch)
    promote(case, members)
    wid = members.workspace_id
    first = body(put(members.alice, members.ah, "defaults.tts", {"max_attempts": 3}, workspace=wid))
    token, bearer = scoped_token(case, members.alice_id, {"app.read", "app.write"}, workspace=wid)
    api = case.app.test_client()
    with case.database.immediate_session() as session:
        if loss == "membership":
            session.delete(session.get_one(m.WorkspaceMembership, (wid, members.alice_id)))
        elif loss == "reset":
            session.get_one(m.UserCredential, members.alice_id).auth_revision += 1
        else:
            session.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
    denied = put(
        api, bearer, "defaults.tts", {"max_attempts": 4}, revision=first["revision"], workspace=wid
    )
    assert denied.status_code == ({"membership": 404, "reset": 401, "scope": 403}[loss]), (
        denied.get_json()
    )
    services = case.app.extensions["pandrator"]["services"]
    assert services.global_settings.defaults("tts", workspace_id=wid)["value"]["max_attempts"] == 3


def test_noninitial_admin_host_authority_is_separate_from_initial_home_content(case, monkeypatch):
    ordinary = new_account(case)
    administrator = new_account(case, "other-admin", "administrator")
    enable(case, monkeypatch)
    owner, oh = login(case, "owner")
    admin, ah = login(case, "other-admin")
    user, uh = login(case, "alice")
    initial = body(owner.get("/api/v1/defaults/tts"))["workspace_id"]
    current = owner.get("/api/v1/settings/web.preferences")
    revision = current.get_json()["revision"] if current.status_code == 200 else 0
    seeded = body(put(owner, oh, "web.preferences", {"theme": "synthetic"}, revision=revision))
    assert seeded["setting_scope"] == "installation" and "workspace_id" not in seeded
    read = body(admin.get("/api/v1/settings/web.preferences"))
    assert read["value"] == seeded["value"] and read["can_manage"]
    assert (
        body(
            put(
                admin,
                ah,
                "web.preferences",
                {"theme": "synthetic-other"},
                revision=read["revision"],
            )
        )["revision"]
        == read["revision"] + 1
    )
    denied = user.get("/api/v1/settings/web.preferences")
    assert body(denied, 403)["error"]["code"] == "administrator_required"
    assert (
        body(put(user, uh, "web.preferences", {}, revision=read["revision"] + 1), 403)["error"][
            "code"
        ]
        == "administrator_required"
    )
    assert (
        admin.get("/api/v1/defaults/tts", query_string={"workspace_id": initial}).status_code == 404
    )
    assert (
        admin.get(
            "/api/v1/settings/web.preferences", query_string={"workspace_id": initial}
        ).status_code
        == 422
    )
    assert put(admin, ah, "web.preferences", {}, query={"session_id": "source"}).status_code == 422
    _, without_admin = scoped_token(
        case, administrator.user_id, {"app.read", "app.write"}, workspace=None
    )
    api = case.app.test_client()
    assert (
        body(api.get("/api/v1/settings/web.preferences", headers=without_admin), 403)["error"][
            "code"
        ]
        == "administrator_required"
    )
    _, readonly = scoped_token(
        case, administrator.user_id, {"app.admin", "app.read"}, workspace=None
    )
    host = body(api.get("/api/v1/settings/web.preferences", headers=readonly))
    assert host["can_manage"] is False and host["setting_scope"] == "installation"
    assert put(api, readonly, "web.preferences", {}, revision=host["revision"]).status_code == 403
    assert ordinary.user_id != administrator.user_id


@pytest.mark.parametrize("key", ["services.tts", "services.stt"])
def test_service_writer_fresh_scope_denies_before_protected_row_and_credential_preparation(
    case, monkeypatch, key
):
    members = team(case, monkeypatch)
    promote(case, members)
    wid = members.workspace_id
    body(put(members.owner, members.oh, key, {"provider_configs": []}, workspace=wid))
    token, bearer = scoped_token(
        case, members.alice_id, {"app.read", "app.write", "app.credentials.write"}, workspace=wid
    )
    services = case.app.extensions["pandrator"]["services"]
    original = services.global_settings.replace

    def revoke_before_writer(*args, **kwargs):
        assert kwargs["access_context"].actor.token_id == token.id
        with case.database.immediate_session() as session:
            session.get_one(m.ApiToken, token.id).scopes_json = ["app.read", "app.write"]
        return original(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Revoked service write reached protected settings/credentials")

    monkeypatch.setattr(services.global_settings, "replace", revoke_before_writer)
    monkeypatch.setattr(global_settings, "workspace_setting_in_workspace", forbidden)
    monkeypatch.setattr(global_settings, "prepare_tts_settings_for_storage", forbidden)
    monkeypatch.setattr(global_settings, "prepare_stt_settings_for_storage", forbidden)
    denied = put(case.app.test_client(), bearer, key, SERVICE_VALUE, revision=1, workspace=wid)
    assert body(denied, 403)["error"]["code"] == "scope_denied"
    with case.database.snapshot_session() as session:
        assert session.get_one(m.WorkspaceSetting, (wid, key)).revision == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.WorkspaceSettingHistory)
                .where(
                    m.WorkspaceSettingHistory.workspace_id == wid,
                    m.WorkspaceSettingHistory.key == key,
                )
            )
            == 0
        )


def test_reads_do_not_create_domain_rows_or_resolve_credentials(case, monkeypatch):
    members = team(case, monkeypatch)
    wid = members.workspace_id
    body(put(members.owner, members.oh, "defaults.tts", {"max_attempts": 3}, workspace=wid))
    body(put(members.owner, members.oh, "services.tts", SERVICE_VALUE, workspace=wid))
    before = counts(case)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Settings read resolved credentials")

    monkeypatch.setattr("pandrator.web.credentials.resolve_secret_reference", forbidden)
    monkeypatch.setattr("pandrator.web.credentials.resolve_provider_credential", forbidden)
    assert (
        members.alice.get("/api/v1/defaults/tts", query_string={"workspace_id": wid}).status_code
        == 200
    )
    assert (
        members.alice.get(
            "/api/v1/settings/defaults.tts", query_string={"workspace_id": wid}
        ).status_code
        == 200
    )
    assert (
        members.owner.get(
            "/api/v1/settings/services.tts", query_string={"workspace_id": wid}
        ).status_code
        == 200
    )
    assert counts(case) == before
