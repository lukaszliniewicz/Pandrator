"""Named source-library scope, finite project reads and replay authority."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_purge_workspace_http import project


def seed(f, workspace, name):
    path = f.services.paths.uploads / f"{uuid4()}.txt"
    path.write_text(name)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path, kind="text", role="upload", workspace_id=workspace
        )
        return f.services.source_library.ensure_for_artifact(
            artifact.id, display_name=name, kind="text", workspace_id=workspace
        )


@pytest.fixture
def library(case, monkeypatch):
    users = team(case, monkeypatch)
    f = SimpleNamespace(case=case, u=users, services=case.app.extensions["pandrator"]["services"])
    with case.database.snapshot_session() as unit:
        f.homes = dict(
            unit.execute(
                select(m.Workspace.personal_owner_user_id, m.Workspace.id).where(
                    m.Workspace.kind == "personal"
                )
            ).all()
        )
    f.private = seed(f, f.homes[users.bob_id], "Bob private source")
    f.shared = seed(f, users.workspace_id, "Team source")
    f.original = seed(f, f.homes[case.actor.user_id], "Original source")
    return f


def test_default_personal_and_explicit_team_library_are_separate(library):
    f, u = library, library.u
    assert body(u.alice.get("/api/v1/sources"))["items"] == []
    assert [x["id"] for x in body(u.bob.get("/api/v1/sources"))["items"]] == [f.private.id]
    assert [x["id"] for x in body(u.owner.get("/api/v1/sources"))["items"]] == [f.original.id]
    query = {"workspace_id": u.workspace_id, "view": "compact"}
    for client in (u.alice, u.bob):
        items = body(client.get("/api/v1/sources", query_string=query))["items"]
        assert [x["id"] for x in items] == [f.shared.id]
        assert items[0]["workspace_id"] == u.workspace_id
        assert not {"path", "external_path", "metadata"}.intersection(items[0])


def test_foreign_user_and_installation_admin_denials_precede_body(library, monkeypatch):
    f, u = library, library.u

    def forbidden(*_args, **_kwargs):
        pytest.fail("Private source mutation reached JSON before authority")

    monkeypatch.setattr(Request, "get_json", forbidden)
    root = f"/api/v1/sources/{f.private.id}"
    for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
        assert (
            client.get(
                "/api/v1/sources", query_string={"workspace_id": f.homes[u.bob_id]}
            ).status_code
            == 404
        )
        assert client.get(root + "/references").status_code == 404
        assert client.patch(root, json={}, headers=csrf).status_code == 404
        assert client.delete(root, headers=csrf).status_code == 404
    with f.case.database.snapshot_session() as unit:
        asset = unit.get_one(m.SourceAsset, f.private.id)
        assert asset.revision == 1 and asset.state == "current"


def test_read_only_member_discovers_but_cannot_attach_or_rename_before_json(library, monkeypatch):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    _, bearer = scoped_token(f.case, u.alice_id, {"app.read"}, workspace=u.workspace_id)
    api = f.case.app.test_client()
    assert (
        len(
            body(
                api.get(
                    "/api/v1/sources", query_string={"workspace_id": u.workspace_id}, headers=bearer
                )
            )["items"]
        )
        == 1
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Read-only mutation parsed JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    assert api.patch(f"/api/v1/sources/{f.shared.id}", json={}, headers=bearer).status_code == 403
    assert (
        api.post(f"/api/v1/sessions/{record['id']}/sources", json={}, headers=bearer).status_code
        == 403
    )


def test_member_attachment_replay_rechecks_current_write_scope(library, monkeypatch):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write"}, workspace=u.workspace_id
    )
    api = f.case.app.test_client()
    root = f"/api/v1/sessions/{record['id']}/sources"
    values = {"source_asset_id": f.shared.id, "role": "reference"}
    request_headers = {
        **headers(bearer, "source-attachment-current-scope"),
        "If-Match": str(record["revision"]),
    }
    accepted = body(api.post(root, json=values, headers=request_headers), 201)
    original = Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == root:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    assert api.post(root, json=values, headers=request_headers).status_code == 403
    with f.case.database.snapshot_session() as unit:
        assert unit.get_one(m.SessionSource, accepted["id"]).revision == 1


def test_private_project_viewer_reads_attachments_without_library_discovery(library):
    f, u = library, library.u
    record = project(u.bob, u.bh)
    attached = body(
        u.bob.post(
            f"/api/v1/sessions/{record['id']}/sources",
            json={"source_asset_id": f.private.id, "role": "reference"},
            headers={
                **headers(u.bh, "private-source-attachment"),
                "If-Match": str(record["revision"]),
            },
        ),
        201,
    )
    with f.case.database.immediate_session() as unit:
        unit.add(
            m.ProjectMembership(project_id=record["project_id"], user_id=u.alice_id, role="viewer")
        )
    _, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write"}, project=record["project_id"]
    )
    api = f.case.app.test_client()
    items = body(api.get(f"/api/v1/sessions/{record['id']}/sources", headers=bearer))["items"]
    assert [x["attachment"]["id"] for x in items] == [attached["id"]]
    assert api.get("/api/v1/sources", headers=bearer).status_code == 404
    assert api.get(f"/api/v1/sources/{f.private.id}/references", headers=bearer).status_code == 404
    assert (
        api.post(f"/api/v1/sessions/{record['id']}/sources", json={}, headers=bearer).status_code
        == 403
    )


def test_mcp_source_selector_is_structured_and_forwarded_without_paths():
    from pandrator_mcp.schemas import ListSourcesInput
    from pandrator_mcp.tools.sessions import list_sources

    calls = []
    application = SimpleNamespace(
        list_sources=lambda **kwargs: (
            calls.append(kwargs)
            or {
                "items": [
                    {
                        "id": "asset",
                        "workspace_id": "team",
                        "display_name": "Shared input",
                        "state": "current",
                        "path": "/private-path",
                        "metadata": {"secret": "omit"},
                    }
                ]
            }
        )
    )
    runtime: Any = SimpleNamespace(require_application=lambda: application)
    result = list_sources(runtime, ListSourcesInput(workspace_id="team"))
    assert calls == [{"include_trashed": False, "workspace_id": "team"}]
    assert result["items"][0]["workspace_id"] == "team"
    assert "private-path" not in str(result) and "secret" not in str(result)
