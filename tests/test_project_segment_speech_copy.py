"""Current segment choices copy independently of their private library and creator."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
import requests
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_publication as publisher
from pandrator.web.projects.copy_contracts import COPY_STATE_VERSION, ProjectCopyError
from pandrator.web.projects.copy_files import StagedProjectFiles
from pandrator.web.speech_plan_context import plan_signature
from tests.project_copy_test_support import snapshot_destination_connections
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_cast_retention_integration import native_transport
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_project_null_speech_copy import capture_source
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_settings_workspace_access import put
from tests.test_voice_crud_workspace_access import create, sample
from tests.test_voice_lifecycle_idempotency import silent_wav


def identity_transport(monkeypatch):
    calls = []

    def post(url, **options):
        assert url == "https://speech.invalid/v1/audio/speech"
        assert options["allow_redirects"] is False
        calls.append(options["json"])
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "audio/wav"
        response._content = silent_wav()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.Session, "post", lambda _session, url, **kwargs: post(url, **kwargs))
    return calls


def source_choice(f, *, pinned):
    users = f.users
    body(put(users.owner, users.oh, "services.tts", {"provider_configs": [{
        "id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-team-segment-key", "credential_backend": "database",
    }]}, revision=1, workspace=users.workspace_id))
    source = body(users.alice.post("/api/v1/sessions", json={
        "name": "Private current segment choice", "workflow_kind": "voiceover",
        "source_language": "en",
    }, headers=headers(users.ah, "segment-copy-source")), 201)
    body(users.alice.put(f"/api/v1/sessions/{source['id']}/settings/tts", json={"value": {
        "service": "audio_cpp", "model": MODEL, "voice": "narrator-without-reference",
        "language": "en", "voice_mode_version": 0, "casting_enabled": False,
        "performance_enabled": False, "tts_concurrent_requests": 1,
    }}, headers={**headers(users.ah, "segment-copy-tts"), "If-Match": '"0"'}))
    voice = sample_id = original_path = None
    if pinned:
        voice = body(create(users.alice, users.ah, "Private segment recording"), 201)
        sample_id, _artifact_id, original_path = sample(f.case, voice)
        with f.case.database.immediate_session() as session:
            session.get_one(m.Voice, voice["id"]).metadata_json = {"providers": {"audio_cpp": {
                "status": "ready", "resource_kind": "linked_reference", "voice_id": "private-segment-alias",
            }}}
            selected = session.get_one(m.VoiceSample, sample_id)
            selected.transcript, selected.transcript_reviewed = TRANSCRIPT, True
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        plan = f.services.generation.create_plan(source["id"], source_revision_id=None,
            settings={}, segments=[{"text": "The saved segment belongs to the project.", "language": "en"}])
    with f.case.database.snapshot_session() as session:
        segment_id = session.scalar(select(m.GenerationSegment.id).where(
            m.GenerationSegment.plan_revision_id == plan["active_revision_id"]))
    assert segment_id is not None
    selection = {"voice_id": voice["id"]} if voice is not None else {"voice": "identity-only-segment"}
    saved = body(users.alice.patch(f"/api/v1/generation-segments/{segment_id}", json=selection,
        headers={**headers(users.ah, "save-segment-before-copy"), "If-Match": '"1"'}))
    assert saved["retained_voice"]["model"] == MODEL
    with f.case.database.snapshot_session() as session:
        reference = session.get_one(m.ProjectSpeechReference, (source["id"], f"segment:{segment_id}"))
        assert (reference.input_artifact_id is not None) == pinned
        signature = plan_signature(session, plan["active_revision_id"])
    body(users.alice.post(f"/api/v1/sessions/{source['id']}/generation-plan/review",
        json={"revision_id": plan["active_revision_id"], "content_signature": signature},
        headers=headers(users.ah, "review-current-segment-choice")))
    return SimpleNamespace(f=f, source=source, actor=actor, plan=plan, segment_id=segment_id,
        saved=saved, pinned=pinned, voice=voice, sample_id=sample_id, original_path=original_path)


def copy_current(c, *, key="copy-current-saved-segment"):
    f, users = c.f, c.f.users
    snapshot = capture_source(f, c.source)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    assert state["schema_version"] == COPY_STATE_VERSION == 15
    segment = state["rows"]["generation_segments"][0]
    assert segment["voice_id"] is None and segment["voice"] is None
    assert len(state["rows"]["speech_plan_reviews"]) == 1
    queued = body(users.alice.post(f"/api/v1/projects/{c.source['project_id']}/copy", json={
        "destination_workspace_id": users.workspace_id, "expected_fingerprint": snapshot.fingerprint,
        "destination_connections": snapshot_destination_connections(snapshot), "name": snapshot.name,
    }, headers=headers(users.ah, key)), 202)
    worker = Worker(f.services.jobs, "current-segment-copy-worker", f.services.workflow_handlers.handlers())
    assert worker.run_once()
    result = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert result["state"] == "succeeded", result
    target = result["result"]["source_session_id"]
    segment_id = publisher.copy_identity(queued["id"], "generation_segments", c.segment_id)
    with f.case.database.snapshot_session() as session:
        row = session.get_one(m.GenerationSegment, segment_id)
        assert row.voice is None and row.voice_id is None
        reference = session.get_one(m.ProjectSpeechReference, (target, f"segment:{segment_id}"))
        assert reference.revision == 1
        assert reference.selector_json == c.saved["retained_voice"]["binding"]
        pin = reference.input_artifact_id
        revision_id = row.plan_revision_id
        assert session.get_one(m.SpeechPlanReview, revision_id).content_hash == plan_signature(session, revision_id)
    return SimpleNamespace(worker=worker, target=target, segment_id=segment_id, pin=pin,
        revision_id=revision_id, snapshot=snapshot)


def depart(c, copied):
    f, users = c.f, c.f.users
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
        if c.voice is not None:
            session.delete(session.get_one(m.VoiceSample, c.sample_id))
            session.delete(session.get_one(m.Voice, c.voice["id"]))
    if c.original_path is not None:
        c.original_path.unlink()
    assert users.bob.get(f"/api/v1/sessions/{c.source['id']}").status_code == 404
    if c.voice is not None:
        assert users.bob.get(f"/api/v1/voices/{c.voice['id']}").status_code == 404
    loaded = body(users.bob.get(f"/api/v1/sessions/{copied.target}/generation-segments"))
    item = loaded["items"][0]
    assert item["id"] == copied.segment_id and item["voice"] is None and item["voice_id"] is None
    assert item["retained_voice"] == c.saved["retained_voice"]
    assert "input_artifact_id" not in str(loaded) and "base64" not in str(loaded)


def member_generation(c, copied, *, auth=None, operation=None):
    f, users = c.f, c.f.users
    queued = body(users.bob.post(f"/api/v1/sessions/{copied.target}/generation-admissions",
        json={"operation": operation} if operation else {},
        headers=headers(auth or users.bh, "member-generate-copied-segment")), 202)
    assert copied.worker.run_once()
    admitted = f.services.jobs.get(queued["id"])
    assert admitted.status == "succeeded", admitted.error_message
    return admitted.result_json


@pytest.mark.parametrize("pinned", [False, True])
def test_registered_segment_copy_keeps_review_and_member_generation_after_departure(design, monkeypatch, pinned):
    c = source_choice(design, pinned=pinned)
    f, users = c.f, c.f.users
    copied = copy_current(c)
    with f.case.database.snapshot_session() as session:
        source = session.get_one(m.GenerationSegment, c.segment_id)
        assert (source.voice_id == c.voice["id"]) if c.voice is not None else source.voice == "identity-only-segment"
        source_review = session.get_one(m.SpeechPlanReview, c.plan["active_revision_id"]).content_hash
    depart(c, copied)
    with f.case.database.snapshot_session() as session:
        assert plan_signature(session, c.plan["active_revision_id"]) == source_review
    project_id = body(users.bob.get(f"/api/v1/sessions/{copied.target}"))["project_id"]
    _token, auth = scoped_token(f.case, users.bob_id,
        {"app.read", "app.write", "app.run"}, project=project_id)

    def no_library(*_args, **_kwargs):
        pytest.fail("Copied current segment consulted the mutable source library")

    monkeypatch.setattr("pandrator.web.project_speech_reference_capture.capture_speech_input", no_library)
    monkeypatch.setattr("pandrator.web.speech_inputs.capture_speech_input", no_library)
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index", no_library)
    monkeypatch.setattr(f.services.workflow_handlers, "prepare_audio_cpp_voice_reference", no_library)
    reads = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "voices.metadata_json" in statement or "voice_samples.transcript" in statement:
            reads.append(statement)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        calls = native_transport(monkeypatch) if pinned else identity_transport(monkeypatch)
        admitted = member_generation(c, copied, auth=auth)
        assert copied.worker.run_once()
        job = f.services.jobs.get(admitted["job_id"])
        assert job.status == "succeeded", job.error_message
        assert len(calls) == 1
        with f.case.database.snapshot_session() as session:
            take = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_run_id == admitted["generation_run_id"]))
            assert take is not None and take.artifact_id is not None and take.status == "completed"
            artifact_id = take.artifact_id
            if pinned:
                assert session.get(m.ArtifactEdge, (copied.pin, artifact_id)) is not None
            run = session.get_one(m.GenerationRun, admitted["generation_run_id"])
            entry = run.settings_snapshot_json["generation_control_snapshot"]["segments"][copied.segment_id]
            assert entry["voice_binding"] == c.saved["retained_voice"]["binding"]
        assert users.bob.get(f"/api/v1/artifacts/{artifact_id}/content", headers=auth).data.startswith(b"RIFF")
        public = body(users.bob.get(f"/api/v1/generation-runs/{admitted['generation_run_id']}", headers=auth))
        assert "_project_speech_input" not in str(public) and "base64" not in str(public)
        assert reads == []
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    patched = body(users.bob.patch(f"/api/v1/generation-segments/{copied.segment_id}",
        json={"silence_after_ms": 250}, headers={**headers(auth, "member-edit-copied-choice"), "If-Match": '"1"'}))
    assert patched["retained_voice"] == c.saved["retained_voice"]


def test_forged_current_segment_graph_and_old_format_fail_before_staging(design, monkeypatch):
    c = source_choice(design, pinned=True)
    snapshot = capture_source(c.f, c.source)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    for defect in ("old_format", "foreign_slot", "foreign_session", "duplicate", "selector", "model", "pin", "live_fk"):
        forged = deepcopy(state)
        refs = forged["rows"]["project_speech_references"]
        reference = next(row for row in refs if row["slot_key"].startswith("segment:"))
        if defect == "old_format":
            forged["schema_version"] = 11
        elif defect == "foreign_slot":
            reference["slot_key"] = "segment:" + new_id()
        elif defect == "foreign_session":
            reference["session_id"] = new_id()
        elif defect == "duplicate":
            refs.append(deepcopy(reference))
        elif defect == "selector":
            reference["selector_hash"] = "f" * 64
        elif defect == "model":
            reference["binding_json"]["model"] = "audio_cpp:other-model"
        elif defect == "pin":
            reference["input_artifact_id"] = new_id()
        else:
            forged["rows"]["generation_segments"][0]["voice_id"] = c.voice["id"]
        encoded = json.dumps(forged, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        invalid = replace(snapshot, state_json=encoded, fingerprint=hashlib.sha256(encoded.encode()).hexdigest())
        with monkeypatch.context() as patches:
            patches.setattr(publisher, "_staging", lambda *_a, **_k: pytest.fail("Invalid segment graph passed staging boundary"))
            with c.f.case.database.immediate_session() as session:
                before = session.scalar(select(func.count()).select_from(m.Project))
                with pytest.raises(ProjectCopyError) as caught:
                    publisher.publish_project_copy_rows(session, invalid, cast(StagedProjectFiles, object()),
                        copy_id=new_id(), created_by_user_id=c.f.users.alice_id)
                assert caught.value.code == "copy_graph_invalid", defect
                assert session.scalar(select(func.count()).select_from(m.Project)) == before


def test_noncurrent_segment_reference_body_is_excluded_and_missing_current_choice_stays_blocked(design):
    c = source_choice(design, pinned=False)
    with c.f.case.database.immediate_session() as session:
        session.add(m.ProjectSpeechReference(session_id=c.source["id"], slot_key="segment:" + new_id(),
            selector_json={"not_current": "historical private choice body"}, selector_hash="f" * 64,
            binding_json={}, input_artifact_id=None, input_content_hash=None, revision=1))
    snapshot = capture_source(c.f, c.source)
    assert snapshot.ready and "historical private choice body" not in snapshot.state_json
    references = json.loads(snapshot.state_json)["rows"]["project_speech_references"]
    assert len([row for row in references if row["slot_key"].startswith("segment:")]) == 1
    with c.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.ProjectSpeechReference,
            (c.source["id"], f"segment:{c.segment_id}")))
        session.get_one(m.GenerationSegment, c.segment_id).text = "missing choice speech body"
    snapshot = capture_source(c.f, c.source)
    assert not snapshot.ready and ("unsupported_state", "generation_plans") in snapshot.blockers
    assert "historical private choice body" not in snapshot.state_json
    assert "missing choice speech body" not in snapshot.state_json
    assert json.loads(snapshot.state_json)["rows"]["generation_segments"] == []
    with c.f.case.database.immediate_session() as session:
        session.get_one(m.GenerationSegment, c.segment_id).voice = None
    inherited = capture_source(c.f, c.source)
    assert inherited.ready, inherited.blockers
    rows = json.loads(inherited.state_json)["rows"]
    assert not any(row["slot_key"].startswith("segment:") for row in rows["project_speech_references"])
    assert rows["speech_plan_reviews"] == []


@pytest.mark.parametrize("when", ["before_synthesis", "after_request"])
def test_retired_copied_segment_pin_prevents_take_publication(design, monkeypatch, when):
    c = source_choice(design, pinned=True)
    copied = copy_current(c)
    depart(c, copied)
    admitted = member_generation(c, copied)

    def retire():
        with c.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, copied.pin).state = "deleted"

    if when == "before_synthesis":
        retire()
    calls = native_transport(monkeypatch, after_send=retire if when == "after_request" else None)
    assert copied.worker.run_once()
    result = c.f.services.jobs.get(admitted["job_id"])
    assert result.status == "failed"
    assert len(calls) == (1 if when == "after_request" else 0)
    with c.f.case.database.snapshot_session() as session:
        assert session.scalar(select(m.AudioTake.id).where(m.AudioTake.generation_run_id == admitted["generation_run_id"])) is None


@pytest.mark.parametrize("pinned", [False, True])
def test_current_recorded_segment_copy_and_recopy_keep_owned_audio_and_saved_choice(design, monkeypatch, pinned):
    from pandrator.web.access.current_session_policy import require_current_session_access
    from pandrator.web.generation_recast import _recorded_snapshots
    from pandrator.web.recorded_speech_contracts import PROVENANCE_KEY
    from tests.test_project_audio_copy import generate, route_config, worker_for

    c = source_choice(design, pinned=pinned)
    f, users = c.f, c.f.users
    route_config(users.alice, users.ah)
    calls = native_transport(monkeypatch) if pinned else identity_transport(monkeypatch)
    generate(f, users.alice, users.ah, c.source["id"], worker_for(f), "source-recorded-segment")
    with f.case.database.snapshot_session() as session:
        take = session.scalar(select(m.AudioTake).where(
            m.AudioTake.generation_segment_id == c.segment_id, m.AudioTake.is_active.is_(True)))
        assert take is not None and take.artifact_id is not None
        original = session.get_one(m.Artifact, take.artifact_id)
        original_bytes = f.services.paths.managed_path(original.relative_path).read_bytes()
        original_hash = original.content_hash
    assert len(calls) == 1
    copied = copy_current(c)
    state = json.loads(copied.snapshot.state_json)
    wrappers = [row["metadata_json"][PROVENANCE_KEY] for row in state["rows"]["artifacts"]
                if PROVENANCE_KEY in row["metadata_json"]]
    assert len(wrappers) == 1 and wrappers[0]["version"] == 2
    assert len(state["rows"]["audio_takes"]) == 1

    # Older copy formats must not acquire expanded evidence under a valid hash.
    for version, envelope_version in ((11, 2), (12, 2), (13, 1)):
        forged = {**deepcopy(state), "schema_version": version}
        for row in forged["rows"]["artifacts"]:
            if PROVENANCE_KEY in row["metadata_json"]:
                row["metadata_json"][PROVENANCE_KEY]["version"] = envelope_version
        encoded = json.dumps(forged, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        invalid = replace(copied.snapshot, state_json=encoded,
            fingerprint=hashlib.sha256(encoded.encode()).hexdigest())
        with monkeypatch.context() as patches:
            patches.setattr(publisher, "_staging", lambda *_a, **_k: pytest.fail("Expanded recording passed old-format staging boundary"))
            with f.case.database.immediate_session() as session:
                with pytest.raises(ProjectCopyError):
                    publisher.publish_project_copy_rows(session, invalid, cast(StagedProjectFiles, object()),
                        copy_id=new_id(), created_by_user_id=users.alice_id)

    # A second current copy consumes its own frozen recorded evidence, not runs.
    current = body(users.alice.get(f"/api/v1/sessions/{copied.target}"))
    recopy_source = SimpleNamespace(f=f, source=current, segment_id=copied.segment_id, saved=c.saved)
    recopied = copy_current(recopy_source, key="recopy-current-saved-segment")
    depart(c, copied)
    assert users.bob.get(f"/api/v1/sessions/{recopied.target}").status_code == 200
    bob = actor_from_principal(current_browser_principal(replace(f.case, client=users.bob)))
    assert bob is not None
    with execution_actor_scope(bob), f.case.database.snapshot_session() as session:
        require_current_session_access(session, copied.target)
        recorded = session.scalar(select(m.AudioTake).where(
            m.AudioTake.generation_segment_id == copied.segment_id, m.AudioTake.is_active.is_(True)))
        assert recorded is not None and recorded.artifact_id is not None and recorded.generation_run_id is None
        artifact = session.get_one(m.Artifact, recorded.artifact_id)
        assert artifact.content_hash == original_hash
        assert f.services.paths.managed_path(artifact.relative_path).read_bytes() == original_bytes
        evidence = _recorded_snapshots(session, copied.target, {copied.segment_id: {"id": recorded.id}})
        assert copied.segment_id in evidence
        assert evidence[copied.segment_id][2].startswith("copied-speech:")
        assert evidence[copied.segment_id][1]["voice_binding"] == c.saved["retained_voice"]["binding"]
        artifact_id = artifact.id
    project_id = current["project_id"]
    _token, auth = scoped_token(f.case, users.bob_id,
        {"app.read", "app.write", "app.run"}, project=project_id)
    played = users.bob.get(f"/api/v1/artifacts/{artifact_id}/content", headers=auth)
    assert played.status_code == 200 and played.data == original_bytes

    def no_library(*_args, **_kwargs):
        pytest.fail("Copied recorded segment reached the original library")

    for path in ("pandrator.web.project_speech_reference_capture.capture_speech_input",
                 "pandrator.web.speech_inputs.capture_speech_input",
                 "pandrator.web.generation_audio_identity.voice_reference_index"):
        monkeypatch.setattr(path, no_library)
    monkeypatch.setattr(f.services.workflow_handlers, "prepare_audio_cpp_voice_reference", no_library)
    regenerated = member_generation(c, copied, auth=auth, operation="regenerate")
    assert copied.worker.run_once()
    result = f.services.jobs.get(regenerated["job_id"])
    assert result.status == "succeeded", result.error_message
    assert len(calls) == 2
    assert users.bob.get(f"/api/v1/sessions/{c.source['id']}").status_code == 404


def test_current_recorded_null_narrator_copy_keeps_owned_audio_without_frozen_cast(design, monkeypatch):
    from pandrator.web.recorded_speech_contracts import PROVENANCE_KEY
    from tests.test_project_audio_copy import generate, route_config, worker_for
    from tests.test_project_null_speech_copy import null_source

    f, users = design, design.users
    route_config(users.owner, users.oh, workspace=users.workspace_id, revision=1)
    route_config(users.alice, users.ah)
    source = null_source(f)
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        f.services.generation.create_plan(source["id"], source_revision_id=None, settings={},
            segments=[{"text": "An explicit saved narrator without audio input.", "language": "en"}])
    calls = identity_transport(monkeypatch)
    worker = worker_for(f)
    generate(f, users.alice, users.ah, source["id"], worker, "source-null-recorded-narrator")
    snapshot = capture_source(f, source)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    assert len(state["rows"]["audio_takes"]) == 1 and len(snapshot.files) == 1
    copied_audio = next(row for row in state["rows"]["artifacts"] if PROVENANCE_KEY in row["metadata_json"])
    evidence = copied_audio["metadata_json"][PROVENANCE_KEY]
    assert evidence["version"] == 2
    selected = next(iter(evidence["segments"].values()))
    assert selected["generation_control_snapshot"] is None
    queued = body(users.alice.post(f"/api/v1/projects/{source['project_id']}/copy", json={
        "destination_workspace_id": users.workspace_id, "expected_fingerprint": snapshot.fingerprint,
        "destination_connections": snapshot_destination_connections(snapshot),
    }, headers=headers(users.ah, "copy-null-recorded-narrator")), 202)
    assert worker.run_once()
    result = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert result["state"] == "succeeded", result
    target = result["result"]["source_session_id"]
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    artifact_id = publisher.copy_identity(queued["id"], "artifacts", copied_audio["id"])
    assert users.bob.get(f"/api/v1/artifacts/{artifact_id}/content").data.startswith(b"RIFF")
    regenerated = SimpleNamespace(target=target, worker=worker)
    admitted = member_generation(SimpleNamespace(f=f), regenerated, operation="regenerate")
    assert worker.run_once()
    generated = f.services.jobs.get(admitted["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 2
