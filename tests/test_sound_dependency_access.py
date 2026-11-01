"""Current cues publish audio bytes within current project and credential rights."""

from __future__ import annotations

from sqlalchemy import event, select, text

from pandrator.web import models as m
from pandrator.web.projects.models import ProjectMembership
from tests.test_evidence_access_routes import body, headers
from tests.test_project_sound_workspace_access import add, cue, source
from tests.test_provider_workspace_access import scoped_token
from tests.test_sound_library_workspace_access import _candidate
from tests.test_sound_library_workspace_http import case as case  # noqa: F401
from tests.test_sound_library_workspace_http import library as library  # noqa: F401
from tests.test_sound_library_workspace_http import no_network as no_network  # noqa: F401


def viewed_sound(f):
    u = f.users
    record = source(u.owner, u.oh)
    saved = body(add(u.owner, u.oh, record, cue(f.owner[0]["id"])), 201)
    path = f"/api/v1/projects/{record['project_id']}/viewers"
    revision = body(u.owner.get(path))["revision"]
    body(u.owner.put(path + "/" + u.alice_id,
        json={"expected_revision": revision}, headers=u.oh))
    with f.case.database.snapshot_session() as session:
        artifact = session.get_one(m.Artifact, f.owner[0]["artifact_id"])
        original = f.services.paths.managed_path(artifact.relative_path).read_bytes()
    return record, saved, original


def test_viewer_bytes_range_head_and_etag_never_hydrate_library_metadata(library):
    f, u = library, library.users
    _record, _saved, expected = viewed_sound(f)
    artifact_id = f.owner[0]["artifact_id"]
    unrelated = _candidate(f.case, f.original)
    with f.case.database.immediate_session() as session:
        other = session.get_one(m.SoundVariant, unrelated).artifact_id
        # Invalid private JSON proves serving bytes never decodes the body.
        session.execute(text("UPDATE artifacts SET metadata_json=:value WHERE id=:id"),
            {"value": "private invalid JSON sentinel", "id": artifact_id})
    statements = []

    def observed(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    path = f"/api/v1/artifacts/{artifact_id}/content"
    event.listen(f.case.database.engine, "before_cursor_execute", observed)
    try:
        response = u.alice.get(path)
        assert response.status_code == 200 and response.data == expected
        assert u.alice.head(path).status_code == 200
        part = u.alice.get(path, headers={"Range": "bytes=0-15"})
        assert part.status_code == 206 and part.data == expected[:16]
        unchanged = u.alice.get(path, headers={"If-None-Match": response.headers["ETag"]})
        assert unchanged.status_code == 304
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert not any("artifacts.metadata_json" in sql
        or "sound_variants.provenance_json" in sql
        or "project_sound_cues.anchor_json" in sql for sql in statements)
    assert u.alice.get(f"/api/v1/artifacts/{artifact_id}/context").status_code == 404
    assert u.alice.get(f"/api/v1/artifacts/{other}/content").status_code == 404
    assert u.bob.get(path).status_code == 404
    assert u.alice.get("/api/v1/sounds",
        query_string={"workspace_id": f.original}).status_code == 404


def test_latest_cue_removal_closes_bytes_while_history_remains(library):
    f, u = library, library.users
    record, saved, _expected = viewed_sound(f)
    path = f"/api/v1/artifacts/{f.owner[0]['artifact_id']}/content"
    assert u.alice.get(path).status_code == 200
    body(u.owner.delete(
        f"/api/v1/sessions/{record['id']}/sound-plan/cues/{saved['cue']['logical_id']}",
        headers={**headers(u.oh, "remove-published-sound"), "If-Match": "1"}))
    assert u.alice.get(path).status_code == 404
    with f.case.database.snapshot_session() as session:
        from pandrator.web.sound_project_models import ProjectSoundCue

        assert session.scalar(select(ProjectSoundCue.id).limit(1)) is not None


def test_project_credential_can_read_selected_bytes_without_workspace_ceiling(library):
    f, u = library, library.users
    record, _saved, expected = viewed_sound(f)
    _token, project = scoped_token(f.case, u.alice_id, {"app.read"},
        project=record["project_id"])
    _token, wrong_workspace = scoped_token(f.case, u.alice_id, {"app.read"},
        workspace=u.workspace_id)
    api = f.case.app.test_client()
    path = f"/api/v1/artifacts/{f.owner[0]['artifact_id']}/content"
    assert api.get(path, headers=project).data == expected
    assert api.get(path, headers=wrong_workspace).status_code == 404
    assert api.get(f"/api/v1/artifacts/{f.owner[0]['artifact_id']}/context",
        headers=project).status_code == 404
    assert api.get("/api/v1/sounds", headers=project,
        query_string={"workspace_id": f.original}).status_code == 404


def test_current_grant_and_source_lifetime_are_rechecked_for_sound_bytes(library):
    f, u = library, library.users
    record, _saved, _expected = viewed_sound(f)
    path = f"/api/v1/artifacts/{f.owner[0]['artifact_id']}/content"
    assert u.alice.get(path).status_code == 200
    with f.case.database.immediate_session() as session:
        session.get_one(m.SessionRecord, record["id"]).trashed_at = m.utcnow()
    assert u.alice.get(path).status_code == 404
    with f.case.database.immediate_session() as session:
        session.get_one(m.SessionRecord, record["id"]).trashed_at = None
    assert u.alice.get(path).status_code == 200
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(ProjectMembership,
            (record["project_id"], u.alice_id)))
    assert u.alice.get(path).status_code == 404


def test_ordinary_owner_content_has_one_artifact_lookup_and_no_sound_join(library):
    f, u = library, library.users
    path = f"/api/v1/artifacts/{f.owner[0]['artifact_id']}/content"
    statements = []

    def observed(_c, _cu, sql, _p, _ctx, _many):
        if "FROM artifacts" in sql:
            statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observed)
    try:
        assert u.owner.get(path).status_code == 200
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert len(statements) == 1
    assert "project_sound_cues" not in statements[0]
    assert "metadata_json" not in statements[0]
