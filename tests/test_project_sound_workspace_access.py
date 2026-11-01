"""Project sound edits retain fresh project and selected-library authority."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import scoped_token
from tests.test_sound_library_workspace_access import _candidate
from tests.test_sound_library_workspace_http import case as case  # noqa: F401
from tests.test_sound_library_workspace_http import library as library  # noqa: F401
from tests.test_sound_library_workspace_http import no_network as no_network  # noqa: F401


def source(client, csrf, *, workspace=None):
    values = {"name": "Sound authority fixture", "workflow_kind": "subtitles"}
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(client.post("/api/v1/sessions", json=values,
        headers=headers(csrf, f"sound-source-{uuid4()}")), 201)


def cue(variant_id, revision=0):
    return {"expected_revision": revision, "variant_id": variant_id,
        "anchor": {"kind": "source_time", "time_seconds": 0.0},
        "playback": {"mode": "overlay"}}


def add(client, csrf, record, value, key="sound-add-current-cue"):
    return client.post(f"/api/v1/sessions/{record['id']}/sound-plan/cues",
        json=value, headers=headers(csrf, key))


def counts(f):
    with f.case.database.snapshot_session() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (ProjectSoundPlan, ProjectSoundCue, m.ApiIdempotency)}


def test_foreign_user_and_admin_cannot_read_or_hydrate_sound_mutations(library, monkeypatch):
    f, u = library, library.users
    private = source(u.bob, u.bh)
    saved = body(add(u.bob, u.bh, private, cue(f.private[0]["id"])), 201)
    base = f"/api/v1/sessions/{private['id']}/sound-plan"
    before, statements = counts(f), []

    def observed(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    def forbidden_json(*_a, **_kw):
        pytest.fail("Foreign project authorization must precede request hydration")

    monkeypatch.setattr(Request, "get_json", forbidden_json)
    event.listen(f.case.database.engine, "before_cursor_execute", observed)
    try:
        for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
            assert client.get(base).status_code == 404
            for method, path in (("post", base + "/cues"),
                    ("patch", base + "/cues/" + saved["cue"]["logical_id"]),
                    ("delete", base + "/cues/" + saved["cue"]["logical_id"]),
                    ("post", base + "/restore")):
                result = getattr(client, method)(path, json={},
                    headers=headers(csrf, "sound-foreign-denied"))
                assert result.status_code == 404
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert counts(f) == before
    assert not any("project_sound_cues.anchor_json" in sql
        or "sound_variants.provenance_json" in sql for sql in statements)


def test_member_editing_survives_creator_departure(library):
    f, u = library, library.users
    record = source(u.alice, u.ah, workspace=u.workspace_id)
    saved = body(add(u.alice, u.ah, record, cue(f.shared[0]["id"])), 201)
    base = f"/api/v1/sessions/{record['id']}/sound-plan"
    logical = saved["cue"]["logical_id"]
    assert body(u.bob.get(base))["revision"] == 1
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    updated = body(u.bob.patch(base + "/cues/" + logical,
        json={"expected_revision": 1, "playback": {"gain_db": -3.0}},
        headers=headers(u.bh, "remaining-member-edits-sound")))
    assert updated["revision"] == 2
    assert body(u.bob.get(base))["cues"]["items"][0]["playback"]["gain_db"] == -3.0
    assert u.alice.get(base).status_code == 404
    assert add(u.alice, u.ah, record, cue(f.shared[0]["id"])).status_code == 404


def test_project_viewer_can_read_but_cannot_edit_or_browse_library(library, monkeypatch):
    f, u = library, library.users
    record = source(u.owner, u.oh)
    body(add(u.owner, u.oh, record, cue(f.owner[0]["id"])), 201)
    viewers = body(u.owner.get(f"/api/v1/projects/{record['project_id']}/viewers"))
    body(u.owner.put(f"/api/v1/projects/{record['project_id']}/viewers/{u.alice_id}",
        json={"expected_revision": viewers["revision"]}, headers=u.oh))
    base = f"/api/v1/sessions/{record['id']}/sound-plan"
    assert len(body(u.alice.get(base))["cues"]["items"]) == 1
    assert u.alice.get("/api/v1/sounds", query_string={"workspace_id": f.original}).status_code == 404

    def forbidden_json(*_a, **_kw):
        pytest.fail("Project viewing must be denied EDIT before JSON")

    monkeypatch.setattr(Request, "get_json", forbidden_json)
    assert add(u.alice, u.ah, record, cue(f.owner[0]["id"], 1),
        "viewer-cannot-add-sound").status_code == 403


def test_project_token_reuses_attached_sound_without_acquiring_library_access(library):
    f, u = library, library.users
    record = source(u.alice, u.ah, workspace=u.workspace_id)
    body(add(u.alice, u.ah, record, cue(f.shared[0]["id"])), 201)
    unrelated = _candidate(f.case, u.workspace_id)
    _token, bearer = scoped_token(f.case, u.alice_id, {"app.read", "app.write"},
        project=record["project_id"])
    api = f.case.app.test_client()
    base = f"/api/v1/sessions/{record['id']}/sound-plan"
    assert api.get(base, headers=bearer).status_code == 200
    assert api.get("/api/v1/sounds", query_string={"workspace_id": u.workspace_id},
        headers=bearer).status_code == 404
    assert body(add(api, bearer, record, cue(f.shared[0]["id"], 1),
        "project-token-reuses-sound"), 201)["revision"] == 2
    before, statements = counts(f), []

    def observed(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observed)
    try:
        assert add(api, bearer, record, cue(unrelated, 2),
            "project-token-cannot-publish-library").status_code == 404
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert counts(f) == before
    assert not any("sound_variants.provenance_json" in sql for sql in statements)
    _token, workspace = scoped_token(f.case, u.alice_id, {"app.read", "app.write"},
        workspace=u.workspace_id)
    assert body(add(api, workspace, record, cue(unrelated, 2),
        "workspace-token-selects-sound"), 201)["revision"] == 3


@pytest.mark.parametrize("loss", ["membership", "token"])
def test_writer_rechecks_current_authority_before_replaying_saved_cue(library, monkeypatch, loss):
    f, u = library, library.users
    record = source(u.alice, u.ah, workspace=u.workspace_id)
    token, bearer = scoped_token(f.case, u.alice_id, {"app.read", "app.write"},
        workspace=u.workspace_id)
    api = f.case.app.test_client()
    value = cue(f.shared[0]["id"])
    body(add(api, bearer, record, value, "sound-writer-replay"), 201)
    before, calls = counts(f), []
    original = Request.get_json

    def parse_then_revoke(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == f"/api/v1/sessions/{record['id']}/sound-plan/cues":
            calls.append(True)
            with f.case.database.immediate_session() as session:
                if loss == "membership":
                    session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
                else:
                    session.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_revoke)
    result = add(api, bearer, record, value, "sound-writer-replay")
    assert result.status_code == (404 if loss == "membership" else 403)
    assert calls == [True] and counts(f) == before


def test_direct_service_proves_current_actor_before_private_plan_bodies(library):
    f, u = library, library.users
    private = source(u.bob, u.bh)
    body(add(u.bob, u.bh, private, cue(f.private[0]["id"])), 201)
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=u.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        with pytest.raises(SessionNotFound):
            f.services.project_sounds.get_plan(private["id"])
        with f.case.database.snapshot_session() as session:
            with pytest.raises(SessionNotFound):
                f.services.project_sounds.snapshot_in_session(session, private["id"])
            with pytest.raises(SessionNotFound):
                f.services.project_sounds.add_in_session(session, private["id"], cue(f.private[0]["id"]))
    assert f.services.project_sounds.get_plan(private["id"])["revision"] == 1
