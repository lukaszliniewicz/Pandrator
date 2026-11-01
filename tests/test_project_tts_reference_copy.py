"""A copied saved narrator uses destination-owned inputs, without its private library."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_publication as publisher
from pandrator.web.projects.copy_contracts import (
    COPY_STATE_VERSION,
    ProjectCopyError,
    ProjectCopySnapshot,
)
from pandrator.web.projects.copy_files import StagedProjectFiles
from pandrator.web.projects.copy_snapshot import capture_current_project
from pandrator.web.projects.copy_sound import SOUND_FAMILIES
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT, transport
from tests.test_settings_workspace_access import put
from tests.test_voice_crud_workspace_access import create, sample


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("forged_new_family", [False, True])
def test_legacy_copy_formats_keep_exact_bytes_and_reject_a_new_family(version, forged_new_family):
    families = set(publisher._FIELDS) - SOUND_FAMILIES - {"performance_plans"} - {"project_speech_references", "audio_takes"}
    families -= {"generation_plans", "generation_plan_revisions", "generation_segments", "speech_plan_reviews"}
    if version == 1:
        families -= {"knowledge_ledgers", "subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 2:
        families -= {"subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 3:
        families -= {"media_edit_plans", "media_edit_plan_revisions"}
    rows = {name: [] for name in sorted(families)}
    if forged_new_family:
        rows["project_speech_references"] = []
    state = _canonical({"schema_version": version, "rows": rows, "binding_fields": {}, "unsupported_counts": {}})
    snapshot = ProjectCopySnapshot("source", "destination", "Legacy", state,
        hashlib.sha256(state.encode()).hexdigest(), (), (), 0)
    if forged_new_family:
        with pytest.raises(ProjectCopyError, match="incomplete or inconsistent"):
            publisher._decode(snapshot)
    else:
        decoded, _identities, _ids = publisher._decode(snapshot)
        assert decoded["project_speech_references"] == []
        assert snapshot.state_json == state
        assert snapshot.fingerprint == hashlib.sha256(state.encode()).hexdigest()


def test_public_copy_of_saved_private_narrator_runs_for_member_without_source_access(design, monkeypatch):
    f, users = design, design.users
    body(put(users.owner, users.oh, "services.tts", {"provider_configs": [{
        "id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-destination-copy-key", "credential_backend": "database",
    }]}, revision=1, workspace=users.workspace_id))
    source = body(users.alice.post("/api/v1/sessions", json={
        "name": "Private retained narrator", "workflow_kind": "subtitles", "source_language": "en",
    }, headers=headers(users.ah, "private-narrator-copy-source")), 201)
    voice = body(create(users.alice, users.ah, "Private copy narrator"), 201)
    sid, _aid, original_path = sample(f.case, voice)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {"providers": {"audio_cpp": {
            "status": "ready", "resource_kind": "linked_reference", "voice_id": "private-copy-reference",
        }}}
        selected = session.get_one(m.VoiceSample, sid)
        selected.transcript, selected.transcript_reviewed = TRANSCRIPT, True
    settings_path = f"/api/v1/sessions/{source['id']}/settings/tts"
    saved = body(users.alice.put(settings_path, json={"value": {
        "service": "audio_cpp", "model": MODEL, "voice": "private-copy-reference", "voice_id": voice["id"],
        "language": "en", "voice_mode_version": 1, "performance_enabled": False, "casting_enabled": False,
    }}, headers={**headers(users.ah, "private-narrator-save-before-copy"), "If-Match": '"0"'}))
    assert saved["revision"] == 1
    removed = users.alice.delete(f"/api/v1/voices/{voice['id']}/samples/{sid}",
        headers={**users.ah, "If-Match": '"1"'})
    assert removed.status_code == 200, removed.get_json()
    assert not original_path.exists()
    principal = current_browser_principal(replace(f.case, client=users.alice))
    actor = actor_from_principal(principal)
    assert actor is not None
    context = AccessContext(actor)
    with f.case.database.snapshot_session() as session:
        snapshot = capture_current_project(session, context, source["project_id"], users.workspace_id,
            settings_service=f.services.workspace_settings)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    assert state["schema_version"] == COPY_STATE_VERSION
    references = state["rows"]["project_speech_references"]
    assert len(references) == 1 and references[0]["selector_json"]["voice_id"] == voice["id"]
    assert len(snapshot.files) == 2
    source_reference = deepcopy(references[0])
    source_settings = body(users.alice.get(settings_path))

    # Unsupported saved slots are counted without serializing their bodies.
    private_cast_marker = "unpublished cast reference body"
    with f.case.database.immediate_session() as session:
        session.add(m.ProjectSpeechReference(**{
            **deepcopy(source_reference), "slot_key": "cast:narrator",
            "selector_json": {"unsupported_private_payload": private_cast_marker},
        }))
    with f.case.database.snapshot_session() as session:
        blocked = capture_current_project(session, context, source["project_id"], users.workspace_id,
            settings_service=f.services.workspace_settings)
    blocked_state = json.loads(blocked.state_json)
    assert not blocked.ready
    assert ("unsupported_state", "project_speech_references") in blocked.blockers
    assert blocked_state["unsupported_counts"]["project_speech_references"] == 1
    assert blocked_state["rows"]["project_speech_references"] == [source_reference]
    assert private_cast_marker not in blocked.state_json
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.ProjectSpeechReference, (source["id"], "cast:narrator")))
        unretained = session.get_one(m.ProjectSpeechReference, (source["id"], "tts"))
        unretained.input_artifact_id = None
        unretained.input_content_hash = None
    with f.case.database.snapshot_session() as session:
        blocked = capture_current_project(session, context, source["project_id"], users.workspace_id,
            settings_service=f.services.workspace_settings)
    blocked_state = json.loads(blocked.state_json)
    assert blocked.ready, blocked.blockers
    assert "project_speech_references" not in blocked_state["unsupported_counts"]
    assert blocked_state["rows"]["project_speech_references"] == [{
        **source_reference, "input_artifact_id": None, "input_content_hash": None,
    }]
    with f.case.database.immediate_session() as session:
        restored = session.get_one(m.ProjectSpeechReference, (source["id"], "tts"))
        restored.input_artifact_id = source_reference["input_artifact_id"]
        restored.input_content_hash = source_reference["input_content_hash"]
    with f.case.database.snapshot_session() as session:
        restored_snapshot = capture_current_project(session, context, source["project_id"], users.workspace_id,
            settings_service=f.services.workspace_settings)
    assert restored_snapshot.ready and restored_snapshot.fingerprint == snapshot.fingerprint

    # A malformed durable graph must fail at its validation boundary, before
    # staged proof handling or any insert can hide a missing domain check.
    with f.case.database.snapshot_session() as session:
        before = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (m.Project, m.SessionRecord, m.Artifact, m.ProjectSpeechReference)}
    for mutation in ("selector_hash", "foreign_pin", "settings_selection"):
        forged = deepcopy(state)
        if mutation == "selector_hash":
            forged["rows"]["project_speech_references"][0]["selector_hash"] = "f" * 64
        elif mutation == "foreign_pin":
            forged["rows"]["project_speech_references"][0]["input_artifact_id"] = new_id()
        else:
            tts = next(row for row in forged["rows"]["session_settings"] if row["section"] == "tts")
            tts["value_json"]["voice_id"] = new_id()
        encoded = _canonical(forged)
        bad = replace(snapshot, state_json=encoded, fingerprint=hashlib.sha256(encoded.encode()).hexdigest())
        with monkeypatch.context() as patches:
            patches.setattr(publisher, "_staging", lambda *_a, **_k: pytest.fail("invalid speech graph passed validation"))
            with f.case.database.immediate_session() as session:
                with pytest.raises(ProjectCopyError) as invalid:
                    publisher.publish_project_copy_rows(session, bad, cast(StagedProjectFiles, object()),
                        copy_id=new_id(), created_by_user_id=users.alice_id)
                assert invalid.value.code == "copy_graph_invalid"
        with f.case.database.snapshot_session() as session:
            assert before == {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (m.Project, m.SessionRecord, m.Artifact, m.ProjectSpeechReference)}

    def no_library(*_args, **_kwargs):
        pytest.fail("Copied narrator consulted the original voice library")
    monkeypatch.setattr("pandrator.web.project_speech_reference_capture.capture_speech_input", no_library)
    monkeypatch.setattr("pandrator.web.speech_inputs.capture_speech_input", no_library)
    preview = body(users.alice.get(f"/api/v1/projects/{source['project_id']}/copy-preview",
        query_string={"workspace_id": users.workspace_id}))
    assert preview["ready"] and preview["file_count"] == 2, preview["blockers"]
    assert preview["fingerprint"] == snapshot.fingerprint
    queued = body(users.alice.post(f"/api/v1/projects/{source['project_id']}/copy", json={
        "destination_workspace_id": users.workspace_id, "expected_fingerprint": preview["fingerprint"],
        "name": "Team retained narrator",
    }, headers=headers(users.ah, "copy-private-saved-narrator")), 202)
    extension = f.case.app.extensions["pandrator"]
    handlers = extension["workflow_handlers"]
    worker = Worker(f.services.jobs, "saved-narrator-copy", handlers.handlers())
    assert worker.run_once()
    finished = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert finished["state"] == "succeeded", finished
    copied = finished["result"]
    copied_sid = copied["source_session_id"]
    with f.case.database.snapshot_session() as session:
        reference = session.get_one(m.ProjectSpeechReference, (copied_sid, "tts"))
        pin_id = publisher.copy_identity(queued["id"], "artifacts", source_reference["input_artifact_id"])
        assert reference.input_artifact_id == pin_id
        assert reference.input_content_hash == source_reference["input_content_hash"]
        assert reference.selector_json == source_reference["selector_json"]
        assert reference.selector_hash == source_reference["selector_hash"] and reference.revision == 1
        assert session.scalar(select(func.count()).select_from(m.Voice)) == 1
        assert session.scalar(select(func.count()).select_from(m.VoiceSample)) == 0
    assert body(users.bob.get(f"/api/v1/sessions/{copied_sid}"))["project_id"] == copied["project_id"]
    assert users.bob.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    assert users.bob.get(f"/api/v1/voices/{voice['id']}").status_code == 404
    assert body(users.alice.get(settings_path)) == source_settings
    with f.case.database.snapshot_session() as session:
        unchanged = capture_current_project(session, context, source["project_id"], users.workspace_id,
            settings_service=f.services.workspace_settings)
    assert json.loads(unchanged.state_json)["rows"]["project_speech_references"] == [source_reference]

    bob = actor_from_principal(current_browser_principal(replace(f.case, client=users.bob)))
    assert bob is not None
    with execution_actor_scope(bob):
        f.services.generation.create_plan(copied_sid, source_revision_id=None, settings={},
            segments=[{"text": "A copied narrator belongs to the workspace.", "language": "en"}])
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", no_library)
    calls = transport(monkeypatch)
    admission = body(users.bob.post(f"/api/v1/sessions/{copied_sid}/generation-admissions", json={},
        headers=headers(users.bh, "copied-narrator-member-generation")), 202)
    assert worker.run_once()
    admitted = f.services.jobs.get(admission["id"])
    assert admitted.status == "succeeded", admitted.error_message
    assert worker.run_once()
    generation_job = f.services.jobs.get(admitted.result_json["job_id"])
    assert generation_job.status == "succeeded", generation_job.error_message
    assert len(calls) == 1
    with f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, admitted.result_json["generation_run_id"])
        assert run.settings_snapshot_json["tts"]["_project_speech_input"]["artifact_id"] == pin_id
        take = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id))
        assert take is not None and take.status == "completed" and take.artifact_id is not None
        artifact_id = take.artifact_id
    assert users.bob.get(f"/api/v1/artifacts/{artifact_id}/content").data.startswith(b"RIFF")
