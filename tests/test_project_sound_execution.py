"""Named sound jobs prove current project authority before preparation and IO."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.logic.book_timing import AlignedBookText, BookWord
from pandrator.logic.model_acquisition import require_model_acquisition
from pandrator.web import models as m
from pandrator.web import sound_resolution_worker
from pandrator.web.jobs import Worker
from tests.test_evidence_access_routes import body, headers
from tests.test_project_sound_copy import _base
from tests.test_project_sound_workspace_access import add, cue, source
from tests.test_provider_workspace_access import scoped_token
from tests.test_sound_library_workspace_http import case as case  # noqa: F401
from tests.test_sound_library_workspace_http import library as library  # noqa: F401
from tests.test_sound_library_workspace_http import no_network as no_network  # noqa: F401


@pytest.fixture(autouse=True)
def alignment_binary(monkeypatch, tmp_path):
    monkeypatch.setattr("pandrator.logic.dubbing.crispasr.resolve_executable",
        lambda *_args, **_kwargs: str(tmp_path / "fake-crispasr"))


def scene(f, *, words=False):
    u = f.users
    record = source(u.alice, u.ah, workspace=u.workspace_id)
    base = _base(f, record["id"])
    if words:
        with f.case.database.immediate_session() as unit:
            plan = m.GenerationPlan(session_id=record["id"])
            unit.add(plan)
            unit.flush()
            revision = m.GenerationPlanRevision(
                plan_id=plan.id, revision_number=1, content_hash="sound-execution-fixture")
            unit.add(revision)
            unit.flush()
            plan.active_revision_id = revision.id
            segment = m.GenerationSegment(plan_revision_id=revision.id, ordinal=0,
                text="Hello world", language="en")
            unit.add(segment)
            unit.flush()
            take = m.AudioTake(generation_segment_id=segment.id, artifact_id=base.id,
                status="completed", is_active=True, duration_ms=2000)
            unit.add(take)
            take_segment = segment.id
        with f.case.database.immediate_session() as unit:
            unit.get_one(m.Artifact, base.id).metadata_json = {
                **base.metadata_json, "synthesized_text": "Hello world"}
        request = {"expected_revision": 0, "variant_id": f.shared[0]["id"],
            "anchor": {"kind": "after_word", "segment_id": take_segment,
                "quote": "Hello", "occurrence": 1}, "playback": {"mode": "overlay"}}
    else:
        request = cue(f.shared[0]["id"])
    saved = body(add(u.alice, u.ah, record, request, f"execution-cue-{uuid4()}"), 201)
    return SimpleNamespace(record=record, base=base, cue=saved["cue"])


def submit(f, p, action, *, client=None, csrf=None, key=None):
    values = {"expected_revision": 1}
    if action == "render":
        values["base_artifact_id"] = p.base.id
    return (client or f.users.alice).post(
        f"/api/v1/sessions/{p.record['id']}/sound-plan/{action}", json=values,
        headers=headers(csrf or f.users.ah, key or f"sound-execution-{action}"))


def run(f, queued):
    assert Worker(f.services.jobs, "scoped-sound-execution-worker",
        f.services.workflow_handlers.handlers()).run_once()
    return f.services.jobs.get(queued["job_id"])


def counts(f):
    with f.case.database.snapshot_session() as unit:
        return {model.__tablename__: unit.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.Artifact, m.ApiIdempotency)}


def test_foreign_user_and_admin_denial_precedes_json_and_processing_bodies(library, monkeypatch):
    f, u = library, library.users
    private = source(u.bob, u.bh)
    statements = []

    def observed(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    def forbidden(*_a, **_kw):
        pytest.fail("Foreign sound execution reached request JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    before = counts(f)
    event.listen(f.case.database.engine, "before_cursor_execute", observed)
    try:
        for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
            root = f"/api/v1/sessions/{private['id']}/sound-plan"
            assert client.get(root + "/mix").status_code == 404
            for action in ("render", "resolve"):
                assert client.post(root + "/" + action, json={},
                    headers=headers(csrf, "foreign-sound-execution")).status_code == 404
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert counts(f) == before
    assert not any("project_sound_cues.anchor_json" in sql
        or "sound_variants.provenance_json" in sql for sql in statements)


def test_project_viewer_reads_status_but_cannot_start_sound_work(library, monkeypatch):
    u = library.users
    private = source(u.owner, u.oh)
    root = f"/api/v1/projects/{private['project_id']}/viewers"
    revision = body(u.owner.get(root))["revision"]
    body(u.owner.put(root + "/" + u.alice_id,
        json={"expected_revision": revision}, headers=u.oh))
    assert u.alice.get(f"/api/v1/sessions/{private['id']}/sound-plan/mix").status_code == 200

    def forbidden(*_a, **_kw):
        pytest.fail("Viewer reached processing body")

    monkeypatch.setattr(Request, "get_json", forbidden)
    for action in ("render", "resolve"):
        assert u.alice.post(f"/api/v1/sessions/{private['id']}/sound-plan/{action}",
            json={}, headers=headers(u.ah, "viewer-sound-work")).status_code == 403


@pytest.mark.parametrize("action", ["render", "resolve"])
def test_current_authority_precedes_saved_admission_replay(library, monkeypatch, action):
    f, u = library, library.users
    p = scene(f, words=action == "resolve")
    queued = body(submit(f, p, action), 202)
    before = counts(f)
    original = Request.get_json

    def parse_then_revoke(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.path.endswith("/sound-plan/" + action):
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
        return result

    monkeypatch.setattr(Request, "get_json", parse_then_revoke)
    assert submit(f, p, action).status_code == 404
    assert queued["job_id"] and counts(f) == before


def test_mix_scope_checks_and_real_registered_member_render(library):
    f, u = library, library.users
    p = scene(f)
    _token, readonly = scoped_token(f.case, u.bob_id, {"app.read", "app.write"},
        workspace=u.workspace_id)
    _token, blind_run = scoped_token(f.case, u.bob_id, {"app.run"}, workspace=u.workspace_id)
    api = f.case.app.test_client()
    assert submit(f, p, "render", client=api, csrf=readonly).status_code == 403
    assert submit(f, p, "render", client=api, csrf=blind_run).status_code == 403
    _token, processing = scoped_token(f.case, u.bob_id, {"app.read", "app.run"},
        workspace=u.workspace_id)
    queued = body(submit(f, p, "render", client=api, csrf=processing), 202)
    assert api.get(f"/api/v1/sessions/{p.record['id']}/sound-plan/mix",
        headers=blind_run).status_code == 403
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    job = run(f, queued)
    assert job.status == "succeeded", job.error_message
    assert job.result_json["artifact_id"]
    assert u.bob.get(f"/api/v1/artifacts/{job.result_json['artifact_id']}/content").status_code == 200
    assert u.alice.get(f"/api/v1/sessions/{p.record['id']}/sound-plan/mix").status_code == 404


@pytest.mark.parametrize("change", ["membership", "token"])
def test_mix_rechecks_before_native_work_and_publishes_no_stale_audio(library, monkeypatch, change):
    f, u = library, library.users
    p = scene(f)
    token, bearer = scoped_token(f.case, u.bob_id, {"app.read", "app.run"},
        workspace=u.workspace_id)
    queued = body(submit(f, p, "render", client=f.case.app.test_client(), csrf=bearer), 202)
    with f.case.database.immediate_session() as unit:
        if change == "membership":
            unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.bob_id)))
        else:
            unit.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()
    touched = []

    def forbidden(*_args, **_kwargs):
        touched.append(True)
        pytest.fail("Revoked sound worker reached media preparation")

    monkeypatch.setattr("pandrator.web.sound_mix_snapshot._base_geometry", forbidden)
    job = run(f, queued)
    assert job.status != "succeeded" and touched == []
    with f.case.database.snapshot_session() as unit:
        assert unit.scalar(select(m.Artifact.id).where(m.Artifact.session_id == p.record["id"],
            m.Artifact.role == "sound_mix.audio")) is None


@pytest.mark.parametrize("outcome", ["success", "model_denied", "revoked"])
def test_registered_resolution_fences_alignment_and_publication(library, monkeypatch, outcome):
    f, u = library, library.users
    p = scene(f, words=True)
    queued = body(submit(f, p, "resolve"), 202)
    calls = []

    def align(item, _settings, _workdir, _cancel, *, before_io=None):
        assert before_io is not None
        before_io()
        calls.append(item.take_id)
        if outcome == "model_denied":
            require_model_acquisition()
            pytest.fail("A User acquired a missing model")
        if outcome == "revoked":
            with f.case.database.immediate_session() as unit:
                unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
            before_io()
        return AlignedBookText(item.transcript,
            (BookWord("Hello", 100, 600, 0, 5), BookWord("world", 700, 1200, 6, 11)),
            item.identity["engine"], {"duration_ms": 2000, "identity": item.identity})

    monkeypatch.setattr(sound_resolution_worker, "align_take", align)
    job = run(f, queued)
    assert len(calls) == 1
    with f.case.database.snapshot_session() as unit:
        from pandrator.web.sound_project_models import ProjectSoundPlan

        revision = unit.scalar(select(func.max(ProjectSoundPlan.revision))
            .where(ProjectSoundPlan.session_id == p.record["id"]))
    if outcome == "success":
        assert job.status == "succeeded", job.error_message
        assert revision == 2
        assert job.result_json["resolved"] == 1
    else:
        assert job.status != "succeeded" and revision == 1
        if outcome == "model_denied":
            assert job.error_code == "ModelAcquisitionDenied"
