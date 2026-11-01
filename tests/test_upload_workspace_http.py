"""Named multipart, chunked team ingress and current replay authority."""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest
from flask.wrappers import Request
from sqlalchemy import select

from pandrator.web import models as m
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_purge_workspace_http import project


@pytest.fixture
def uploads(case, monkeypatch):
    return SimpleNamespace(
        case=case, u=team(case, monkeypatch), services=case.app.extensions["pandrator"]["services"]
    )


def test_unbound_multipart_uses_actual_personal_or_selected_team(uploads):
    f, u = uploads, uploads.u
    personal = body(
        u.alice.post(
            "/api/v1/uploads", data={"file": (io.BytesIO(b"personal"), "private.txt")}, headers=u.ah
        ),
        201,
    )
    shared = body(
        u.bob.post(
            "/api/v1/uploads",
            query_string={"workspace_id": u.workspace_id},
            data={"workspace_id": u.workspace_id, "file": (io.BytesIO(b"team"), "shared.txt")},
            headers=u.bh,
        ),
        201,
    )
    with f.case.database.snapshot_session() as unit:
        home = unit.scalar(
            select(m.Workspace.id).where(m.Workspace.personal_owner_user_id == u.alice_id)
        )
        assert unit.get_one(m.Artifact, personal["artifact_id"]).workspace_id == home
        assert unit.get_one(m.SourceAsset, shared["source_asset_id"]).workspace_id == u.workspace_id
    assert [x["id"] for x in body(u.alice.get("/api/v1/sources"))["items"]] == [
        personal["source_asset_id"]
    ]
    assert [
        x["id"]
        for x in body(
            u.alice.get("/api/v1/sources", query_string={"workspace_id": u.workspace_id})
        )["items"]
    ] == [shared["source_asset_id"]]


def test_foreign_multipart_denied_before_parsing_or_staging(uploads, monkeypatch):
    f, u = uploads, uploads.u
    private = project(u.bob, u.bh)
    before = set(f.services.paths.temporary.iterdir())

    def forbidden(*_args, **_kwargs):
        pytest.fail("Foreign multipart request reached form parsing")

    monkeypatch.setattr(Request, "_load_form_data", forbidden)
    for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
        assert (
            client.post(
                "/api/v1/uploads",
                query_string={"session_id": private["id"]},
                data={"file": (io.BytesIO(b"denied"), "denied.txt")},
                headers=csrf,
            ).status_code
            == 404
        )
    assert set(f.services.paths.temporary.iterdir()) == before


def test_real_chunked_team_project_roundtrip_above_browser_threshold(uploads):
    f, u = uploads, uploads.u
    record = project(u.alice, u.ah, u.workspace_id)
    payload = b"a" * (33 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    initialized = body(
        u.alice.post(
            "/api/v1/uploads/init",
            json={
                "filename": "team-transcript.txt",
                "size_bytes": len(payload),
                "sha256": digest,
                "session_id": record["id"],
                "workspace_id": u.workspace_id,
            },
            headers=headers(u.ah, "team-large-source-upload"),
        ),
        201,
    )
    root = f"/api/v1/uploads/{initialized['id']}"
    for index, start in enumerate(range(0, len(payload), initialized["chunk_size"])):
        chunk = payload[start : start + initialized["chunk_size"]]
        body(
            u.bob.put(
                root + f"/chunks/{index}",
                data=chunk,
                headers={
                    **u.bh,
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                },
            )
        )
    completed = body(u.bob.post(root + "/complete", headers=u.bh), 201)
    assert completed["sha256"] == digest
    assert body(u.alice.post(root + "/complete", headers=u.ah), 201) == completed
    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, completed["artifact_id"])
        assert (
            artifact.workspace_id == u.workspace_id and artifact.project_id == record["project_id"]
        )
        assert (
            hashlib.sha256(
                f.services.paths.managed_path(artifact.relative_path).read_bytes()
            ).hexdigest()
            == digest
        )
    assert completed["source_asset_id"] in {
        x["id"]
        for x in body(u.bob.get("/api/v1/sources", query_string={"workspace_id": u.workspace_id}))[
            "items"
        ]
    }
    assert not (f.services.paths.temporary / "uploads" / initialized["id"]).exists()


def test_initialization_receipt_rechecks_scope_in_writer_after_json(uploads, monkeypatch):
    f, u = uploads, uploads.u
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write"}, workspace=u.workspace_id
    )
    api = f.case.app.test_client()
    values = {"filename": "receipt.txt", "size_bytes": 3, "workspace_id": u.workspace_id}
    request_headers = headers(bearer, "current-upload-initialization")
    accepted = body(api.post("/api/v1/uploads/init", json=values, headers=request_headers), 201)
    original = Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == "/api/v1/uploads/init":
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    assert api.post("/api/v1/uploads/init", json=values, headers=request_headers).status_code == 403
    with f.case.database.snapshot_session() as unit:
        assert list(unit.scalars(select(m.UploadSessionRecord.id))) == [accepted["id"]]
