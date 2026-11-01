"""Library transport scope and writer-time authority, without native generation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.identity import initial_personal_workspace
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_lifecycle_idempotency import silent_wav


def seed(services, workspace, name):
    path = services.paths.uploads / f"{name}.wav"
    path.write_bytes(silent_wav())
    with execution_actor_scope(HostActor("pandrator-cli")):
        candidate = services.sounds.import_audio(path, name, workspace_id=workspace)["candidate"]
        with services.database.immediate_session() as session:
            item = services.sounds.save_in_session(
                session, {"candidate_id": candidate["id"], "name": name}, workspace_id=workspace
            )["item"]
    return candidate, item


@pytest.fixture
def library(case, monkeypatch):
    users = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as session:
        original = initial_personal_workspace(session).id
        homes = dict(session.execute(select(m.Workspace.personal_owner_user_id, m.Workspace.id)
            .where(m.Workspace.kind == "personal")).all())
    private = seed(services, homes[users.bob_id], "Private forest")
    shared = seed(services, users.workspace_id, "Shared birds")
    owner = seed(services, original, "Original bells")
    return SimpleNamespace(case=case, users=users, services=services, original=original,
        homes=homes, private=private, shared=shared, owner=owner)


def counts(f):
    with f.case.database.snapshot_session() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (m.SoundItem, m.SoundVariant, m.Artifact, m.Job, m.ApiIdempotency)}


def test_http_default_home_and_explicit_team_discovery_are_separate(library):
    f, u = library, library.users
    assert body(u.alice.get("/api/v1/sounds"))["items"] == []
    assert [x["name"] for x in body(u.owner.get("/api/v1/sounds"))["items"]] == ["Original bells"]
    assert [x["name"] for x in body(u.bob.get("/api/v1/sounds"))["items"]] == ["Private forest"]
    query = {"workspace_id": u.workspace_id, "limit": 1}
    assert [x["id"] for x in body(u.alice.get("/api/v1/sounds", query_string=query))["items"]] == [f.shared[1]["id"]]
    assert body(u.bob.get(f"/api/v1/sounds/{f.shared[1]['id']}", query_string=query))["name"] == "Shared birds"
    assert [x["id"] for x in body(u.alice.get("/api/v1/sounds/candidates", query_string=query))["items"]] == [f.shared[0]["id"]]
    assert body(u.bob.get(f"/api/v1/sounds/candidates/{f.shared[0]['id']}", query_string=query))["id"] == f.shared[0]["id"]
    assert u.alice.get(f"/api/v1/sounds/{f.shared[1]['id']}").status_code == 404


def test_http_foreign_user_and_admin_denials_precede_rows_body_and_replay(library, monkeypatch):
    f, u = library, library.users
    query = {"workspace_id": f.homes[u.bob_id]}
    statements = []
    def observed(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)
    def forbidden_json(*_a, **_kw):
        pytest.fail("Foreign library authority was checked after request hydration")
    original_json = Request.get_json
    monkeypatch.setattr(Request, "get_json", forbidden_json)
    before = counts(f)
    event.listen(f.case.database.engine, "before_cursor_execute", observed)
    try:
        for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
            for path in ("/api/v1/sounds", "/api/v1/sounds/candidates",
                f"/api/v1/sounds/{f.private[1]['id']}", f"/api/v1/sounds/candidates/{f.private[0]['id']}"):
                assert client.get(path, query_string=query).status_code == 404
            for method, path in (("post", "/api/v1/sounds/save"),
                ("patch", f"/api/v1/sounds/{f.private[1]['id']}"),
                ("delete", f"/api/v1/sounds/candidates/{f.private[0]['id']}")):
                result = getattr(client, method)(path, query_string=query, data=b"{invalid",
                    content_type="application/json", headers=headers(csrf, "foreign-library-refusal"))
                assert result.status_code == 404
        monkeypatch.setattr(Request, "get_json", original_json)
        allowed = {"workspace_id": u.workspace_id}
        assert u.alice.post("/api/v1/sounds/save", query_string=allowed,
            json={"candidate_id": f.private[0]["id"], "name": "Cannot attach private bytes"},
            headers=headers(u.ah, "foreign-candidate-allowed-scope")).status_code == 404
        assert u.owner.patch(f"/api/v1/sounds/{f.shared[1]['id']}", query_string=allowed,
            json={"expected_revision": 1, "preferred_variant_id": f.private[0]["id"]},
            headers=headers(u.oh, "foreign-preferred-allowed-scope")).status_code == 404
        assert u.alice.delete(f"/api/v1/sounds/candidates/{f.private[0]['id']}", query_string=allowed,
            headers=headers(u.ah, "foreign-discard-allowed-scope")).status_code == 404
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert not any("sound_items.tags_json" in sql or "sound_variants.provenance_json" in sql for sql in statements)
    assert counts(f) == before


def test_http_member_save_attach_update_archive_and_discard_preserve_revisions(library):
    f, u = library, library.users
    with execution_actor_scope(HostActor("pandrator-cli")):
        path = f.services.paths.uploads / "new-shared.wav"
        path.write_bytes(silent_wav())
        candidate = f.services.sounds.import_audio(path, "New shared", workspace_id=u.workspace_id)["candidate"]
    query = {"workspace_id": u.workspace_id}
    payload = {"candidate_id": candidate["id"], "item_id": f.shared[1]["id"], "expected_revision": 1}
    saved = body(u.alice.post("/api/v1/sounds/save", query_string=query, json=payload,
        headers=headers(u.ah, "member-library-attach")))
    assert saved["item"]["revision"] == 2 and saved["item_id"] == f.shared[1]["id"]
    replay = u.alice.post("/api/v1/sounds/save", query_string=query, json=payload,
        headers=headers(u.ah, "member-library-attach"))
    assert body(replay) == saved and replay.headers["Idempotency-Replayed"] == "true"
    updated = body(u.bob.patch(f"/api/v1/sounds/{saved['item_id']}", query_string=query,
        json={"expected_revision": 2, "name": "Team birds", "tags": ["woods"],
            "preferred_variant_id": candidate["id"], "archived": True},
        headers=headers(u.bh, "member-library-update")))
    assert updated["revision"] == 3 and updated["name"] == "Team birds" and updated["archived"]
    assert updated["preferred_variant"]["id"] == candidate["id"]
    assert u.alice.patch(f"/api/v1/sounds/{saved['item_id']}", query_string=query,
        json={"expected_revision": 2, "name": "Stale overwrite"},
        headers=headers(u.ah, "member-library-stale")).status_code == 409
    assert u.bob.delete(f"/api/v1/sounds/candidates/{candidate['id']}", query_string=query,
        headers=headers(u.bh, "member-discard-attached")).status_code == 422
    with execution_actor_scope(HostActor("pandrator-cli")):
        unsaved = f.services.sounds.import_audio(path, "Discard me", workspace_id=u.workspace_id)["candidate"]
    discarded = body(u.alice.delete(f"/api/v1/sounds/candidates/{unsaved['id']}", query_string=query,
        headers=headers(u.ah, "member-discard-unsaved")))
    assert discarded["status"] == "discarded"


@pytest.mark.parametrize("loss", ["membership", "token"])
def test_http_writer_rechecks_after_body_and_before_reservation(library, monkeypatch, loss):
    f, u = library, library.users
    token, bearer = scoped_token(f.case, u.alice_id, {"app.read", "app.write"}, workspace=u.workspace_id)
    api = f.case.app.test_client()
    before = counts(f)
    original = Request.get_json
    calls = []
    def parse_then_revoke(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == "/api/v1/sounds/save":
            calls.append(True)
            with f.case.database.immediate_session() as session:
                if loss == "membership":
                    session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
                else:
                    session.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()
        return value
    monkeypatch.setattr(Request, "get_json", parse_then_revoke)
    result = api.post("/api/v1/sounds/save", query_string={"workspace_id": u.workspace_id},
        json={"candidate_id": f.shared[0]["id"]}, headers=headers(bearer, "library-writer-revoke"))
    assert result.status_code == (404 if loss == "membership" else 403)
    assert calls == [True] and counts(f) == before


def test_http_replay_refuses_departed_member_and_narrow_credentials(library):
    f, u = library, library.users
    query = {"workspace_id": u.workspace_id}
    value = {"expected_revision": 1, "name": "Shared revised"}
    path = f"/api/v1/sounds/{f.shared[1]['id']}"
    first = u.alice.patch(path, query_string=query, json=value, headers=headers(u.ah, "library-replay-authority"))
    body(first)
    replay = u.alice.patch(path, query_string=query, json=value, headers=headers(u.ah, "library-replay-authority"))
    assert body(replay) == first.get_json() and replay.headers["Idempotency-Replayed"] == "true"
    _, read = scoped_token(f.case, u.bob_id, {"app.read"}, workspace=u.workspace_id)
    api = f.case.app.test_client()
    assert api.get(path, query_string=query, headers=read).status_code == 200
    assert api.patch(path, query_string=query, json=value, headers=headers(read, "library-no-write")).status_code == 403
    _, personal = scoped_token(f.case, u.bob_id, {"app.read", "app.write"}, workspace=f.homes[u.bob_id])
    assert api.get(path, query_string=query, headers=personal).status_code == 404
    before = counts(f)
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    assert u.alice.patch(path, query_string=query, json=value,
        headers=headers(u.ah, "library-replay-authority")).status_code == 404
    assert counts(f) == before


def test_http_library_rejects_session_and_invalid_workspace_scope(library):
    f, u = library, library.users
    for query in ({"session_id": "not-a-library"}, {"workspace_id": ""},
        {"workspace_id": " "}, {"workspace_id": "w" * 81}):
        for path in ("/api/v1/sounds", "/api/v1/sounds/candidates"):
            assert u.alice.get(path, query_string=query).status_code == 422
        assert u.alice.post("/api/v1/sounds/save", query_string=query,
            json={"candidate_id": f.shared[0]["id"]}, headers=headers(u.ah, "invalid-library-scope")).status_code == 422


def test_solo_receipt_payload_remains_canonical_without_workspace_query(case, monkeypatch):
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as session:
        workspace = initial_personal_workspace(session).id
    path = services.paths.uploads / "solo-sound.wav"
    path.write_bytes(silent_wav())
    with execution_actor_scope(HostActor("pandrator-cli")):
        candidate = services.sounds.import_audio(path, "Solo sound", workspace_id=workspace)["candidate"]
    captured = []
    original = services.idempotency.begin
    def observe(*args, **kwargs):
        if kwargs.get("operation_id") == "saveSound":
            captured.append(kwargs["payload"])
        return original(*args, **kwargs)
    monkeypatch.setattr(services.idempotency, "begin", observe)
    value = {"candidate_id": candidate["id"], "name": "Solo saved"}
    csrf = {"X-CSRF-Token": case.csrf}
    first = case.client.post("/api/v1/sounds/save", json=value, headers=headers(csrf, "solo-library-canonical"))
    body(first)
    replay = case.client.post("/api/v1/sounds/save", json=value, headers=headers(csrf, "solo-library-canonical"))
    assert body(replay) == first.get_json() and replay.headers["Idempotency-Replayed"] == "true"
    assert captured == [value, value]
