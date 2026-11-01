"""Registered generation keeps a project's saved segment recording."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.access.current_session_policy import require_current_session_access
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.project_segment_speech import SegmentSpeechReferenceIndex
from pandrator.web.project_speech_references import get_project_speech_reference
from pandrator.web.settings_policy import stable_hash
from pandrator.web.speech_plan_context import SIGNATURE_FIELDS, plan_signature
from tests.test_cast_retention_integration import admit, native_transport
from tests.test_evidence_access_routes import body, headers
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_project_tts_retention import save
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_voice_sample_execution_access import api_actor


@pytest.fixture
def segment_project(project):
    p, r = project, project.r
    body(save(p, {
        "service": "audio_cpp", "model": MODEL, "voice": "narrator-without-reference",
        "language": "en", "casting_enabled": False, "voice_mode_version": 0,
        "performance_enabled": False, "tts_concurrent_requests": 1,
    }))
    plan = r.f.services.generation.create_plan(p.source["id"], source_revision_id=None,
        settings={}, segments=[{"text": "A saved segment recording.", "language": "en",
            "voice_id": r.voice["id"]}])
    with r.f.case.database.snapshot_session() as session:
        segment_id = session.scalar(select(m.GenerationSegment.id).where(
            m.GenerationSegment.plan_revision_id == plan["active_revision_id"]))
    assert segment_id is not None
    actor, _token = api_actor(r.f.case, r.f.users.alice_id, r.f.users.workspace_id)
    response = body(r.f.users.alice.patch(f"/api/v1/generation-segments/{segment_id}",
        json={"voice_id": r.voice["id"]}, headers={**headers(r.f.users.ah,
            "save-segment-before-generation"), "If-Match": '"1"'}))
    assert response["id"] == segment_id and response["retained_voice"]["binding"]["voice_id"] == r.voice["id"]
    with execution_actor_scope(actor), r.f.case.database.snapshot_session() as session:
        stored = get_project_speech_reference(session, p.source["id"], f"segment:{segment_id}",
            authorization_guard=lambda current: require_current_session_access(current, p.source["id"]))
    assert stored is not None and stored.pin is not None
    return SimpleNamespace(p=p, r=r, plan=plan, segment_id=segment_id, stored=stored, actor=actor)


def delete_original(c):
    with c.r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, c.r.sid))
        session.delete(session.get_one(m.Voice, c.r.voice["id"]))
    c.r.path.unlink()
    with c.r.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationSegment, c.segment_id).voice_id is None
        assert session.get_one(m.ProjectSpeechReference,
            (c.p.source["id"], f"segment:{c.segment_id}")).selector_json["voice_id"] == c.r.voice["id"]


@pytest.mark.parametrize("deletion", ["before_admission", "after_admission"])
@pytest.mark.parametrize("principal", ["workspace", "project"])
def test_registered_segment_uses_retained_recording_after_library_fk_is_cleared(
    segment_project, monkeypatch, deletion, principal,
):
    c, protected_reads = segment_project, []
    p, r = c.p, c.r
    auth = None
    if principal == "project":
        _token, auth = scoped_token(r.f.case, r.f.users.alice_id,
            {"app.read", "app.write", "app.run"}, project=p.source["project_id"])
    with execution_actor_scope(c.actor), r.f.case.database.snapshot_session() as session:
        signature = plan_signature(session, c.plan["active_revision_id"])
    if deletion == "before_admission":
        delete_original(c)

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "voices.metadata_json" in statement or "voice_samples.transcript" in statement:
            protected_reads.append(statement)

    event.listen(r.f.case.database.engine, "before_cursor_execute", observe)
    try:
        calls = native_transport(monkeypatch)
        g = admit(c, auth=auth)
        if deletion == "after_admission":
            # Fixture deletion intentionally reads its source rows. Observe
            # application admission/worker activity, not that setup operation.
            event.remove(r.f.case.database.engine, "before_cursor_execute", observe)
            try:
                delete_original(c)
            finally:
                event.listen(r.f.case.database.engine, "before_cursor_execute", observe)
        with execution_actor_scope(c.actor), r.f.case.database.snapshot_session() as session:
            assert plan_signature(session, c.plan["active_revision_id"]) == signature
        monkeypatch.setattr(g.handlers, "prepare_audio_cpp_voice_reference",
            lambda *_a, **_k: pytest.fail("Saved segment reached the mutable library materializer"))
        monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index",
            lambda *_a, **_k: pytest.fail("Saved segment built a mutable library index"))
        assert g.worker.run_once()
        result = r.f.services.jobs.get(g.result["job_id"])
        assert result.status == "succeeded", result.error_message
        assert len(calls) == 1 and TRANSCRIPT in str(calls[0])
        with r.f.case.database.snapshot_session() as session:
            take = session.scalar(select(m.AudioTake).where(
                m.AudioTake.generation_run_id == g.result["generation_run_id"]))
            assert take is not None and take.artifact_id is not None
            edge = session.get(m.ArtifactEdge, (c.stored.pin["artifact_id"], take.artifact_id))
            assert edge is not None and edge.relation == "derived_from"
            artifact_id = take.artifact_id
            run = session.get_one(m.GenerationRun, g.result["generation_run_id"])
            entry = run.settings_snapshot_json["generation_control_snapshot"]["segments"][c.segment_id]
            assert entry["voice_binding"] == c.stored.selector
        peer = r.f.users.bob.get(f"/api/v1/artifacts/{artifact_id}/content")
        assert peer.status_code == 200 and peer.data.startswith(b"RIFF")
        public = body(r.f.users.bob.get(f"/api/v1/generation-runs/{g.result['generation_run_id']}"))
        assert "_project_speech_input" not in str(public) and "base64" not in str(public)
    finally:
        event.remove(r.f.case.database.engine, "before_cursor_execute", observe)
    if principal == "project":
        assert protected_reads == []


@pytest.mark.parametrize("when", ["before_synthesis", "after_request"])
def test_retired_segment_input_prevents_take_publication(segment_project, monkeypatch, when):
    c, r = segment_project, segment_project.r
    delete_original(c)
    g = admit(c)

    def retire():
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, c.stored.pin["artifact_id"]).state = "deleted"

    if when == "before_synthesis":
        retire()
    calls = native_transport(monkeypatch, after_send=retire if when == "after_request" else None)
    assert g.worker.run_once()
    result = r.f.services.jobs.get(g.result["job_id"])
    assert result.status == "failed"
    assert len(calls) == (0 if when == "before_synthesis" else 1)
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(m.AudioTake.id).where(
            m.AudioTake.generation_run_id == g.result["generation_run_id"])) is None


def test_segment_reference_index_has_no_constructor_query_and_one_scoped_revision_read(segment_project):
    c, statements = segment_project, []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with execution_actor_scope(c.actor), c.r.f.case.database.snapshot_session() as session:
        event.listen(c.r.f.case.database.engine, "before_cursor_execute", observe)
        try:
            index = SegmentSpeechReferenceIndex(session)
            assert statements == []
            first = index.for_revision(c.p.source["id"], c.plan["active_revision_id"])
            second = index.for_revision(c.p.source["id"], c.plan["active_revision_id"])
        finally:
            event.remove(c.r.f.case.database.engine, "before_cursor_execute", observe)
    assert first == second and first[f"segment:{c.segment_id}"].pin == c.stored.pin
    body_reads = [statement for statement in statements
        if "project_speech_references.selector_json" in statement]
    assert len(body_reads) == 1
    assert "generation_plans.session_id" in body_reads[0]


def test_plan_signature_without_saved_segment_keeps_legacy_hash(segment_project):
    c = segment_project
    with execution_actor_scope(c.actor), c.r.f.case.database.immediate_session() as session:
        row = session.get_one(m.ProjectSpeechReference, (c.p.source["id"], f"segment:{c.segment_id}"))
        session.delete(row)
        session.flush()
        segment = session.get_one(m.GenerationSegment, c.segment_id)
        legacy = [{key: deepcopy(getattr(segment, key)) for key in SIGNATURE_FIELDS}]
        assert plan_signature(session, c.plan["active_revision_id"]) == stable_hash(legacy)


def test_peer_reads_saved_segment_choice_after_original_is_removed(segment_project, monkeypatch):
    c, r = segment_project, segment_project.r
    delete_original(c)
    monkeypatch.setattr("pandrator.web.speech_inputs.load_speech_input",
        lambda *_a, **_k: pytest.fail("Metadata inspection loaded reference bytes"))
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index",
        lambda *_a, **_k: pytest.fail("Owned segment inspection indexed the origin library"))
    response = body(r.f.users.bob.get(
        f"/api/v1/sessions/{c.p.source['id']}/generation-segments"))
    item = response["items"][0]
    assert item["voice_id"] is None and item["voice"] is None
    saved = item["retained_voice"]
    assert set(saved) == {"binding", "display_name", "adapter", "model"}
    assert saved["binding"] == c.stored.selector
    assert saved["display_name"] == "Retained workshop voice"
    assert saved["adapter"] == "audio_cpp" and saved["model"] == MODEL
    assert c.stored.pin["artifact_id"] not in str(response)
    assert "base64" not in str(response) and "source_fingerprint" not in str(response)
    selected = body(r.f.users.bob.get(
        f"/api/v1/sessions/{c.p.source['id']}/generation-segments?fields=retained_voice"))
    assert selected["items"][0]["retained_voice"] == saved


@pytest.mark.parametrize("mode", ["single", "another_adapter"])
def test_inactive_segment_reference_layer_adds_no_metadata_read(segment_project, mode):
    c, statements = segment_project, []

    def observe(_connection, _cursor, statement, parameters, _context, _many):
        if "project_speech_references" in statement:
            statements.append(tuple(parameters.values()) if isinstance(parameters, dict) else tuple(parameters))

    with execution_actor_scope(c.actor), c.r.f.case.database.snapshot_session() as session:
        snapshot, _digest = c.r.f.services.workspace_settings.resolve_in_session(session, c.p.source["id"])
        if mode == "single":
            snapshot["tts"].update(voice_mode_version=1, casting_enabled=False)
        else:
            snapshot["tts"].update(service="gemini", tts_service="gemini", model="gemini-3.8-flash-tts",
                xtts_model="gemini-3.8-flash-tts", voice="Kore", speaker="Kore")
        segment = session.get_one(m.GenerationSegment, c.segment_id)
        event.listen(c.r.f.case.database.engine, "before_cursor_execute", observe)
        try:
            AudioIdentityContext(session, snapshot).for_segment(segment)
        finally:
            event.remove(c.r.f.case.database.engine, "before_cursor_execute", observe)
    if mode == "single":
        # The active narrator may read its tts slot; inactive segment choices
        # must not load any segment reference metadata.
        assert all("tts" in parameters for parameters in statements)
        assert not any(str(value).startswith("segment:") for parameters in statements for value in parameters)
    else:
        assert statements == []


def test_frozen_inheritance_does_not_adopt_todays_saved_segment_choice(segment_project):
    c = segment_project
    with execution_actor_scope(c.actor), c.r.f.case.database.snapshot_session() as session:
        snapshot, _digest = c.r.f.services.workspace_settings.resolve_in_session(session, c.p.source["id"])
        segment = session.get_one(m.GenerationSegment, c.segment_id)
        current = AudioIdentityContext(session, snapshot).for_segment(segment)
        snapshot["generation_control_snapshot"] = {
            "schema_version": 2, "plan_revision_id": c.plan["active_revision_id"],
            "segments": {c.segment_id: {"voice_binding": None}}, "resolved_bindings": {},
        }
        historical = AudioIdentityContext(session, snapshot).for_segment(segment)
        assert current["voice_reference_hash"] != historical["voice_reference_hash"]
    delete_original(c)
    with execution_actor_scope(c.actor), c.r.f.case.database.snapshot_session() as session:
        after = AudioIdentityContext(session, snapshot).for_segment(session.get_one(m.GenerationSegment, c.segment_id))
        assert after == historical


def test_excluded_segment_keeps_its_saved_choice_in_inspection(segment_project):
    c, r = segment_project, segment_project.r
    delete_original(c)
    with r.f.case.database.snapshot_session() as session:
        version = session.get_one(m.GenerationSegment, c.segment_id).revision
    response = body(r.f.users.alice.patch(f"/api/v1/generation-segments/{c.segment_id}",
        json={"removed": True}, headers={**headers(r.f.users.ah,
            "exclude-owned-segment"), "If-Match": f'"{version}"'}))
    public = body(r.f.users.bob.get(
        f"/api/v1/sessions/{c.p.source['id']}/generation-segments?removed=true"))
    assert len(public["items"]) == 1
    item = public["items"][0]
    assert item["removed"] is True
    assert item["retained_voice"] == response["retained_voice"]
    assert item["retained_voice"]["binding"] == c.stored.selector
