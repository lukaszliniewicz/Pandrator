"""Saved cast recordings survive shared generation and scoped historical recast."""

import base64
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.generation_cast_runtime import (
    cast_resolution_key,
    resolve_project_cast_binding,
    segment_render_parts,
)
from pandrator.web.generation_performance_snapshot import performance_runtime_settings
from pandrator.web.generation_recast import prepare_recast
from pandrator.web.jobs import Worker
from pandrator.web.project_settings_policy import public_project_settings
from pandrator.web.speech_input_contracts import PROJECT_SPEECH_INPUT_KEY
from tests.test_evidence_access_routes import body, headers
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_project_tts_retention import save
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_voice_lifecycle_idempotency import silent_wav
from tests.test_voice_sample_execution_access import api_actor

SECOND_TRANSCRIPT = "The later recording has its own reviewed transcript."
TEXT = "Fred speaks. Wilma replies."


def cast_write(p, cast, *, revision, characters=None, key, auth=None):
    value = {"expected_revision": revision, "cast": cast}
    if characters is not None:
        value["characters"] = characters
    return p.r.f.users.alice.put(
        f"/api/v1/sessions/{p.source['id']}/generation-controls", json=value,
        headers=headers(auth or p.r.f.users.ah, key),
    )


def cast_rows(p):
    with p.r.f.case.database.snapshot_session() as session:
        return {
            row.slot_key: SimpleNamespace(
                pin=None if row.input_artifact_id is None else {
                    "version": 1, "artifact_id": row.input_artifact_id,
                    "content_hash": row.input_content_hash,
                }, binding=deepcopy(row.binding_json), revision=row.revision,
            )
            for row in session.scalars(select(m.ProjectSpeechReference).where(
                m.ProjectSpeechReference.session_id == p.source["id"],
                m.ProjectSpeechReference.slot_key.like("cast:%"),
            ))
        }


@pytest.fixture
def cast_project(project, request: pytest.FixtureRequest):
    p, r = project, project.r
    body(save(p, {"service": "audio_cpp", "model": MODEL, "voice": "narrator-without-reference",
        "language": "en", "casting_enabled": True, "voice_mode_version": 1,
        "performance_enabled": False, "tts_concurrent_requests": 1}))
    binding = {"voice_id": r.voice["id"]}
    cast = {"characters": {"fred": binding}}
    body(cast_write(p, cast, revision=0, key="first-cast-recording", characters=[
        {"id": "fred", "display_name": "Fred"}, {"id": "wilma", "display_name": "Wilma"},
    ]))
    with r.f.case.database.immediate_session() as session:
        sample = session.get_one(m.VoiceSample, r.sid)
        sample.transcript = SECOND_TRANSCRIPT
    cast["characters"]["wilma"] = binding
    body(cast_write(p, cast, revision=1, key="second-cast-recording"))
    references = cast_rows(p)
    fred, wilma = references['cast:["characters","fred"]'], references['cast:["characters","wilma"]']
    assert fred.pin is not None and wilma.pin is not None and fred.pin != wilma.pin
    plan = r.f.services.generation.create_plan(p.source["id"], source_revision_id=None,
        settings={}, segments=[{"text": TEXT, "language": "en"}])
    segment_id = r.f.services.generation.list_segments(p.source["id"])["items"][0]["id"]
    with r.f.case.database.immediate_session() as session:
        segment = session.get_one(m.GenerationSegment, segment_id)
        segment.speech_plan_json = {"speech_xml": f'<segment id="{segment_id}">'
            '<dialogue><speaker ref="fred">Fred speaks.</speaker></dialogue> '
            '<dialogue><speaker ref="wilma">Wilma replies.</speaker></dialogue></segment>'}
        if not getattr(request, "param", False):
            session.delete(session.get_one(m.VoiceSample, r.sid))
            session.delete(session.get_one(m.Voice, r.voice["id"]))
    if not getattr(request, "param", False):
        r.path.unlink()
    return SimpleNamespace(p=p, r=r, cast=cast, binding=binding, fred=fred, wilma=wilma,
        plan=plan, segment_id=segment_id)


def native_transport(monkeypatch, *, after_send=None):
    calls = []

    def post(url, **options):
        assert url == "https://speech.invalid/v1/audio/speech"
        assert options["allow_redirects"] is False
        assert base64.b64encode(silent_wav()).decode() in json.dumps(options["json"])
        calls.append(options["json"])
        if after_send:
            after_send()
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = silent_wav()
        response._content_consumed = True
        response.headers["Content-Type"] = "audio/wav"
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.Session, "post", lambda _session, url, **kwargs: post(url, **kwargs))
    return calls


def admit(c, *, auth=None):
    p, r = c.p, c.r
    handlers = r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(r.f.services.jobs, "retained-cast-integration", handlers.handlers())
    queued = body(r.f.users.alice.post(
        f"/api/v1/sessions/{p.source['id']}/generation-admissions", json={},
        headers=headers(auth or r.f.users.ah, "saved-cast-generation-admission"),
    ), 202)
    assert worker.run_once()
    admission = r.f.services.jobs.get(queued["id"])
    assert admission.status == "succeeded", admission.error_message
    return SimpleNamespace(worker=worker, handlers=handlers, result=admission.result_json)


@pytest.mark.parametrize("principal_mode", ["workspace", "project", "logout", "reassigned"])
def test_registered_shared_cast_keeps_two_versions_after_source_deletion(cast_project, monkeypatch, principal_mode):
    c, protected_reads = cast_project, []
    p, r = c.p, c.r
    auth = None
    if principal_mode == "project":
        _token, auth = scoped_token(r.f.case, r.f.users.alice_id,
            {"app.read", "app.write", "app.run"}, project=p.source["project_id"])

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "voices.metadata_json" in statement or "voice_samples.transcript" in statement:
            protected_reads.append(statement)

    event.listen(r.f.case.database.engine, "before_cursor_execute", observe)
    try:
        calls = native_transport(monkeypatch)
        g = admit(c, auth=auth)
        if principal_mode == "logout":
            assert r.f.users.alice.post("/api/v1/auth/logout", headers=r.f.users.ah).status_code == 200
        if principal_mode == "reassigned":
            c.cast["characters"]["wilma"] = {"voice": "replacement-after-admission"}
            body(cast_write(p, c.cast, revision=2, key="new-cast-after-frozen-admission"))
        monkeypatch.setattr(g.handlers, "prepare_audio_cpp_voice_reference",
            lambda *_a, **_k: pytest.fail("Saved cast reached the mutable library materializer"))
        monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index",
            lambda *_a, **_k: pytest.fail("Saved cast built a mutable library index"))
        assert g.worker.run_once()
        result = r.f.services.jobs.get(g.result["job_id"])
        assert result.status == "succeeded", result.error_message
        assert len(calls) == 2
        assert TRANSCRIPT in json.dumps(calls[0]) and SECOND_TRANSCRIPT in json.dumps(calls[1])
        with r.f.case.database.snapshot_session() as session:
            take = session.scalar(select(m.AudioTake).where(
                m.AudioTake.generation_run_id == g.result["generation_run_id"],
            ))
            assert take is not None and take.artifact_id is not None
            for pin in [c.fred.pin, c.wilma.pin]:
                edge = session.get(m.ArtifactEdge, (pin["artifact_id"], take.artifact_id))
                assert edge is not None and edge.relation == "derived_from"
            run = session.get_one(m.GenerationRun, g.result["generation_run_id"])
            assert run.settings_snapshot_json["generation_control_snapshot"]["schema_version"] == 2
            artifact_id = take.artifact_id
        peer = r.f.users.bob.get(f"/api/v1/artifacts/{artifact_id}/content")
        assert peer.status_code == 200 and peer.data.startswith(b"RIFF")
        public = body(r.f.users.bob.get(f"/api/v1/generation-runs/{g.result['generation_run_id']}"))
        assert "_project_speech_input" not in str(public) and "base64" not in str(public)
    finally:
        event.remove(r.f.case.database.engine, "before_cursor_execute", observe)
    if principal_mode == "project":
        assert protected_reads == []


@pytest.mark.parametrize("when", ["before_synthesis", "after_first_request"])
def test_retired_cast_reference_prevents_take_publication(cast_project, monkeypatch, when):
    c, r = cast_project, cast_project.r
    g = admit(c)

    def retire():
        target = c.fred.pin if when == "before_synthesis" else c.wilma.pin
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, target["artifact_id"]).state = "deleted"

    if when == "before_synthesis":
        retire()
    calls = native_transport(monkeypatch, after_send=retire if when == "after_first_request" else None)
    assert g.worker.run_once()
    result = r.f.services.jobs.get(g.result["job_id"])
    assert result.status == "failed"
    assert len(calls) == (0 if when == "before_synthesis" else 1)
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(m.AudioTake.id).where(
            m.AudioTake.generation_run_id == g.result["generation_run_id"],
        )) is None


def test_partial_recast_retains_unselected_historical_cast_recording(cast_project, monkeypatch):
    c, p, r = cast_project, cast_project.p, cast_project.r
    calls = native_transport(monkeypatch)
    g = admit(c)
    assert g.worker.run_once()
    assert r.f.services.jobs.get(g.result["job_id"]).status == "succeeded"
    assert len(calls) == 2
    c.cast["characters"]["fred"] = {"voice": "new-unrecorded-reference"}
    body(cast_write(p, c.cast, revision=2, key="reassign-selected-character"))
    actor, _token = api_actor(r.f.case, r.f.users.alice_id, r.f.users.workspace_id)
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index",
        lambda *_a, **_k: pytest.fail("Retained recast indexed the deleted source library"))
    with execution_actor_scope(actor):
        prepared = prepare_recast(r.f.case.database, r.f.services.workspace_settings,
            p.source["id"], selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None, prepared["preview"]
    assert prepared["target_segment_ids"] == [c.segment_id]
    snapshot = prepared["frozen_snapshot"]
    parts = segment_render_parts(performance_runtime_settings(snapshot), snapshot, c.segment_id, TEXT)
    assert [part["settings"][PROJECT_SPEECH_INPUT_KEY] for part in parts] == [None, c.wilma.pin]
    assert "_retained_cast_resolutions" not in str(public_project_settings(snapshot))
    assert len(calls) == 2  # Recast preparation compiles without synthesis.


@pytest.mark.parametrize("cast_project", [True], indirect=True)
def test_segment_identity_cache_keeps_distinct_retained_versions(cast_project):
    c, p, r = cast_project, cast_project.p, cast_project.r
    plan = r.f.services.generation.create_plan(p.source["id"], source_revision_id=None,
        settings={}, segments=[{"text": "First block."}, {"text": "Second block."}])
    ids = [row["id"] for row in r.f.services.generation.list_segments(p.source["id"])["items"]]
    actor, _token = api_actor(r.f.case, r.f.users.alice_id, r.f.users.workspace_id)
    with execution_actor_scope(actor), r.f.case.database.immediate_session() as session:
        snapshot, _digest = r.f.services.workspace_settings.resolve_in_session(session, p.source["id"])
        settings = performance_runtime_settings(snapshot)
        snapshot["tts"].update(casting_enabled=False, voice_mode_version=0)
        settings.update(casting_enabled=False, voice_mode_version=0)
        resolutions, entries = {}, {}
        for sid, member in zip(ids, ["fred", "wilma"], strict=True):
            segment = session.get_one(m.GenerationSegment, sid)
            segment.voice_id = r.voice["id"]
            resolved = resolve_project_cast_binding(session, p.source["id"], c.binding,
                settings, f'cast:["characters","{member}"]')
            assert resolved is not None
            resolutions[cast_resolution_key(c.binding, f"segment:{sid}", schema_version=2)] = resolved
            entries[sid] = {"voice_binding": c.binding}
        snapshot["generation_control_snapshot"] = {"schema_version": 2,
            "plan_revision_id": plan["active_revision_id"], "segments": entries,
            "resolved_bindings": resolutions}
        context = AudioIdentityContext(session, snapshot)
        identities = [context.for_segment(session.get_one(m.GenerationSegment, sid)) for sid in ids]
        assert all("performance_error" not in value for value in identities), identities
        assert identities[0]["voice_reference_hash"] != identities[1]["voice_reference_hash"]
        assert len(context.cache) == 2
