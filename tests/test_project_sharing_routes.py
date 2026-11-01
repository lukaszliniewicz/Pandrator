"""Named viewing works through HTTP without broadening workspace authority."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import func, select

from pandrator.web.models import AuditEvent, Project, SessionRecord
from pandrator.web.project_sharing_schemas import (
    ProjectViewersResponse,
    ProjectViewingCandidatesResponse,
    SharedProjectsResponse,
)
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import PASSWORD, enable, new_account


def login(case, name):
    client = case.app.test_client()
    response = client.post(
        "/api/v1/auth/login", json={"login_name": name, "password": PASSWORD},
    )
    assert response.status_code == 200, response.get_json()
    return client, {"X-CSRF-Token": response.get_json()["csrf_token"]}


def project(case):
    services = case.app.extensions["pandrator"]["services"]
    record = services.sessions.create(
        "Private viewing lesson", workflow_kind="subtitles", source_language="en",
    )
    with case.database.snapshot_session() as session:
        root = session.get_one(Project, record.project_id)
        project_id = root.id
    path = services.paths.sessions / record.storage_key / "saved.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n00:00:01,000 --> 00:00:03,500\nA saved lesson.\n", encoding="utf-8")
    artifact = services.artifacts.register(path, kind="srt", role="export", session_id=record.id)
    return project_id, record, artifact


def test_named_viewing_discovery_download_revocation_and_current_revision(case, monkeypatch):
    alice = new_account(case)
    unrelated_admin = new_account(case, "other-admin", "administrator")
    enable(case, monkeypatch)
    owner, owner_headers = login(case, "owner")
    reader, reader_headers = login(case, "alice")
    outsider, _ = login(case, "other-admin")
    root_id, source, artifact = project(case)
    path = f"/api/v1/projects/{root_id}/viewers"
    recipient = f"{path}/{alice.user_id}"
    assert unrelated_admin.user_id != case.auth.initial_account().user_id
    for client in (reader, outsider):
        assert client.get(path).status_code == 404
        assert client.get(f"/api/v1/sessions/{source.id}").status_code == 404
    assert SharedProjectsResponse.model_validate(reader.get("/api/v1/projects/shared-with-me").get_json()).items == []
    initial = owner.get(path)
    assert initial.status_code == 200, initial.get_json()
    access = ProjectViewersResponse.model_validate(initial.get_json())
    assert not access.items
    candidates = owner.get(f"{path}/users?query=ali")
    assert candidates.status_code == 200
    assert ProjectViewingCandidatesResponse.model_validate(candidates.get_json()).items[0].user_id == alice.user_id
    assert owner.put(recipient, json={"expected_revision": access.revision}).status_code == 403
    granted = owner.put(recipient, json={"expected_revision": access.revision}, headers=owner_headers)
    assert granted.status_code == 200, granted.get_json()
    access = ProjectViewersResponse.model_validate(granted.get_json())
    assert [(item.user_id, item.access) for item in access.items] == [(alice.user_id, "view")]
    assert owner.put(recipient, json={"expected_revision": access.revision - 1}, headers=owner_headers).status_code == 409
    repeated = owner.put(recipient, json={"expected_revision": access.revision}, headers=owner_headers)
    assert repeated.status_code == 200 and repeated.get_json()["revision"] == access.revision

    shared_response = reader.get("/api/v1/projects/shared-with-me?query=lesson&limit=1")
    assert shared_response.status_code == 200
    shared = SharedProjectsResponse.model_validate(shared_response.get_json())
    assert [(item.project_id, item.source_session_id) for item in shared.items] == [(root_id, source.id)]
    assert "workspace_id" not in shared_response.get_json()["items"][0]
    assert "workspace_name" not in shared_response.get_json()["items"][0]
    detail = reader.get(f"/api/v1/sessions/{source.id}")
    assert detail.status_code == 200, detail.get_json()
    assert detail.get_json()["allowed_actions"] == ["read"]
    assert detail.get_json()["workspace_name"] is None
    download = reader.get(f"/api/v1/artifacts/{artifact.id}/content")
    assert download.status_code == 200 and b"A saved lesson." in download.data
    for endpoint in (path, f"{path}/users?query=ow"):
        assert reader.get(endpoint).status_code == 403
    assert reader.put(recipient, json={"expected_revision": access.revision}, headers=reader_headers).status_code == 403
    edit = reader.patch(
        f"/api/v1/sessions/{source.id}", json={"name": "Reader cannot edit"},
        headers={**reader_headers, "If-Match": f'"{source.revision}"'},
    )
    assert edit.status_code == 403, edit.get_json()
    export = reader.post(
        f"/api/v1/sessions/{source.id}/stages/export/run",
        json={"output": {"export_mode": "subtitles", "subtitle_format": "srt"}},
        headers={**reader_headers, "Idempotency-Key": "reader-cannot-export"},
    )
    assert export.status_code == 403, export.get_json()
    assert outsider.get(path).status_code == 404

    # Source trash hides the project from a guest while retaining the grant.
    with case.database.immediate_session() as session:
        from pandrator.web.model_base import utcnow

        session.get_one(SessionRecord, source.id).trashed_at = utcnow()
    assert reader.get(f"/api/v1/sessions/{source.id}").status_code == 404
    assert reader.get(f"/api/v1/artifacts/{artifact.id}/content").status_code == 404
    assert not reader.get("/api/v1/projects/shared-with-me").get_json()["items"]
    assert owner.get(path).status_code == 200
    with case.database.immediate_session() as session:
        session.get_one(SessionRecord, source.id).trashed_at = None
    assert reader.get(f"/api/v1/artifacts/{artifact.id}/content").status_code == 200
    revoked = owner.post(f"{recipient}/revoke", json={"expected_revision": access.revision}, headers=owner_headers)
    assert revoked.status_code == 200, revoked.get_json()
    assert not ProjectViewersResponse.model_validate(revoked.get_json()).items
    assert reader.get(f"/api/v1/sessions/{source.id}").status_code == 404
    assert reader.get(f"/api/v1/artifacts/{artifact.id}/content").status_code == 404
    assert not reader.get("/api/v1/projects/shared-with-me").get_json()["items"]
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "project.view.grant")) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "project.view.revoke")) == 1
        assert session.get_one(SessionRecord, source.id).name == source.name


def test_viewing_routes_remain_closed_in_solo_and_validate_bounded_inputs(case, monkeypatch):
    root_id, _source, _artifact = project(case)
    path = f"/api/v1/projects/{root_id}/viewers"
    assert case.client.get("/api/v1/projects/shared-with-me").status_code == 409
    assert case.client.get(path).status_code == 409
    new_account(case)
    enable(case, monkeypatch)
    client, headers = login(case, "owner")
    for suffix in ("?limit=0", "?limit=101", "?offset=-1", "?query=" + "x" * 256):
        assert client.get("/api/v1/projects/shared-with-me" + suffix).status_code == 422
    user_id = client.get(f"{path}/users?query=ali").get_json()["items"][0]["user_id"]
    for body in ({"expected_revision": True}, {"expected_revision": 0}, {"expected_revision": 1, "role": "editor"}):
        response = client.put(f"{path}/{user_id}", json=body, headers=headers)
        assert response.status_code == 422, response.get_json()
    assert client.put(f"{path}/{user_id}", data=" " * 9000, content_type="application/json", headers=headers).status_code == 413


def test_named_consent_selects_project_only_and_revocation_clears_old_approval(case, monkeypatch):
    alice = new_account(case)
    enable(case, monkeypatch)
    owner, owner_headers = login(case, "owner")
    reader, _reader_headers = login(case, "alice")
    root_id, source, artifact = project(case)
    path = f"/api/v1/projects/{root_id}/viewers/{alice.user_id}"
    granted = owner.put(path, json={"expected_revision": 1}, headers=owner_headers)
    assert granted.status_code == 200, granted.get_json()
    revision = granted.get_json()["revision"]
    verifier, client_id = "s" * 64, str(uuid4())
    redirect_uri = "http://127.0.0.1:43123/callback"
    consent = reader.get("/api/v1/auth/automation/authorize", query_string={
        "response_type": "code", "client_id": client_id,
        "client_name": "Disposable project reader", "redirect_uri": redirect_uri,
        "scope": "app.read", "state": "project-consent-test",
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
    })
    assert consent.status_code == 200, consent.get_data(as_text=True)
    html = consent.get_data(as_text=True)
    assert "Approve access to individual shared projects" in html
    project_checkbox = re.search(r'<input type="checkbox" name="project_id"[^>]*>', html)
    assert project_checkbox is not None and "checked" not in project_checkbox.group(0)
    nonce = re.search(r'name="authorization_nonce" value="([^"]+)"', html)
    assert nonce is not None
    approval = reader.post("/api/v1/auth/automation/authorize", data={
        "authorization_nonce": nonce.group(1), "decision": "approve", "project_id": root_id,
    })
    assert approval.status_code == 302, approval.get_data(as_text=True)
    code = parse_qs(urlsplit(approval.headers["Location"]).query)["code"][0]
    api = case.app.test_client()
    token = api.post("/api/v1/auth/automation/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    })
    assert token.status_code == 200, token.get_json()
    bearer = {"Authorization": f'Bearer {token.get_json()["access_token"]}'}
    assert api.get("/api/v1/workspaces", headers=bearer).get_json()["items"] == []
    shared = api.get("/api/v1/projects/shared-with-me", headers=bearer)
    assert [item["project_id"] for item in shared.get_json()["items"]] == [root_id]
    assert api.get(f"/api/v1/artifacts/{artifact.id}/content", headers=bearer).status_code == 200
    revoked = owner.post(f"{path}/revoke", json={"expected_revision": revision}, headers=owner_headers)
    assert revoked.status_code == 200, revoked.get_json()
    assert api.get(f"/api/v1/sessions/{source.id}", headers=bearer).status_code == 404
    regranted = owner.put(path, json={"expected_revision": revoked.get_json()["revision"]}, headers=owner_headers)
    assert regranted.status_code == 200, regranted.get_json()
    assert reader.get(f"/api/v1/sessions/{source.id}").status_code == 200
    assert api.get(f"/api/v1/sessions/{source.id}", headers=bearer).status_code == 404
