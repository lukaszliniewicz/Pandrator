"""Finite saved-cast copy, retained ownership and frozen-format compatibility."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.generation_control_schemas import CastSettings
from pandrator.web.generation_controls import iter_cast_voice_bindings
from pandrator.web.jobs import Worker
from pandrator.web.projects import copy_publication as publisher
from pandrator.web.projects import copy_snapshot as capture
from pandrator.web.projects.copy_contracts import ProjectCopyError, ProjectCopySnapshot
from pandrator.web.projects.copy_sound import SOUND_FAMILIES
from pandrator.web.projects.copy_speech import (
    freeze_cast_reference,
    freeze_generation_controls,
    remap_cast_reference,
    remap_tts_reference,
    validate_speech_copy_graph,
)
from pandrator.web.speech_input_contracts import SpeechInputOwner
from tests.project_copy_test_support import (
    capture_with_connections,
    seed_copy_model,
    snapshot_destination_connections,
)
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_cast_retention_integration import native_transport
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_settings_workspace_access import put
from tests.test_voice_crud_workspace_access import create, sample

PIN = "00000000-0000-0000-0000-000000000001"

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def controls(payload=None):
    return {"id": "ledger", "session_id": "session", "kind": "generation_controls",
            "source_language": "auto", "target_language": "",
            "payload_json": {"characters": [], "cast": {}} if payload is None else payload}


def pure_graph():
    choice = {"voice": "authored-voice", "service": "audio_cpp", "model": MODEL}
    ledger = controls({"characters": [{"id": "character", "display_name": "A person",
                                      "notes": "Source prose"}],
                       "cast": {"narrator": choice, "categories": {"male": choice},
                                "characters": {"character": choice},
                                "source_speakers": {"Speaker 1": choice}}})
    ledger["payload_json"] = freeze_generation_controls(ledger)
    owner = {"workspace_id": "workspace", "project_id": "project", "session_id": "session"}
    artifacts = [dict(id=PIN, state="current", kind="speech_voice_snapshot", role="speech_input",
                      mime_type="application/json", size_bytes=1, content_hash="a" * 64, **owner),
                 dict(id="audio", state="current", kind="audio", role="speech_reference_audio",
                      mime_type="audio/wav", size_bytes=1, content_hash="b" * 64, **owner)]
    refs = [{"session_id": "session", "slot_key": slot,
             "selector_json": binding.model_dump(mode="json"), "selector_hash": "",
             "binding_json": {"adapter": "audio_cpp", "model": MODEL, "voice": "authored-voice", "display_name": "Authored voice"},
             "input_artifact_id": PIN, "input_content_hash": "a" * 64, "revision": 7}
            for slot, binding in iter_cast_voice_bindings(CastSettings.model_validate(ledger["payload_json"]["cast"]))]
    from pandrator.web.project_speech_references import speech_reference_selector_hash
    for ref in refs:
        ref["selector_hash"] = speech_reference_selector_hash(ref["selector_json"])
    return {"projects": [{"id": "project", "workspace_id": "workspace"}],
            "sessions": [{"id": "session", "project_id": "project"}],
            "session_settings": [{"session_id": "session", "section": "tts", "value_json": {
                "service": "audio_cpp", "model": MODEL, "voice": "unused"}}],
            "knowledge_ledgers": [ledger], "artifacts": artifacts,
            "artifact_edges": [{"parent_artifact_id": "audio", "child_artifact_id": PIN,
                                "relation": "speech_reference"}], "project_speech_references": refs}


def test_canonical_controls_all_forms_semantics_and_immutable_source():
    rows = pure_graph()
    before = deepcopy(rows)
    validate_speech_copy_graph(rows)
    assert rows == before
    for ref in rows["project_speech_references"]:
        mapped = remap_cast_reference(ref, {"session": "target-session", PIN: "target-pin"})
        assert mapped == {**ref, "session_id": "target-session", "input_artifact_id": "target-pin", "revision": 1}
    assert freeze_generation_controls(controls())["characters"] == []
    assert freeze_generation_controls(controls({"characters": [{"id": "semantic-id", "display_name": "Name"}], "cast": {}}))["characters"][0]["id"] == "semantic-id"


@pytest.mark.parametrize("defect", ["duplicate", "foreign", "extra", "scope", "too_many", "blank", "bad_category", "bad_speaker"])
def test_controls_reject_invalid_finite_payload(defect):
    row = deepcopy(pure_graph()["knowledge_ledgers"][0])
    payload = row["payload_json"]
    if defect == "duplicate":
        payload["characters"] *= 2
    elif defect == "foreign":
        payload["cast"]["characters"]["undeclared"] = {"voice": "v"}
    elif defect == "extra":
        payload["private"] = "not allowed"
    elif defect == "scope":
        row["source_language"] = "en"
    elif defect == "too_many":
        payload["characters"] = [{"id": f"c{i}", "display_name": "X"} for i in range(2001)]
    elif defect == "blank":
        payload["characters"][0]["id"] = " "
    elif defect == "bad_category":
        payload["cast"]["categories"] = {"alien": {"voice": "v"}}
    else:
        payload["cast"]["source_speakers"] = {" ": {"voice": "v"}}
    with pytest.raises(ValueError):
        freeze_generation_controls(row)


@pytest.mark.parametrize("defect", ["selector", "selector_hash", "binding", "owner", "edge", "hash", "missing", "null", "extra", "slot", "service", "literal", "audio_hash"])
def test_cast_graph_rejects_inconsistent_proof(defect):
    rows = pure_graph()
    ref = rows["project_speech_references"][0]
    if defect == "selector":
        ref["selector_json"]["voice"] = "other"
    elif defect == "selector_hash":
        ref["selector_hash"] = "f" * 64
    elif defect == "binding":
        ref["binding_json"]["model"] = "other"
    elif defect == "owner":
        rows["artifacts"][0]["project_id"] = "foreign"
    elif defect == "edge":
        rows["artifact_edges"] *= 2
    elif defect == "hash":
        rows["artifacts"][0]["content_hash"] = "c" * 64
    elif defect == "missing":
        rows["project_speech_references"].pop()
    elif defect == "null":
        ref["input_artifact_id"] = ref["input_content_hash"] = None
    elif defect == "extra":
        rows["project_speech_references"].append({**deepcopy(ref), "slot_key": 'cast:["characters","orphan"]'})
    elif defect == "slot":
        ref["slot_key"] = 'cast:[ "categories", "male" ]'
    elif defect == "service":
        rows["session_settings"][0]["value_json"]["service"] = "gemini"
    elif defect == "literal":
        ref["binding_json"]["voice"] = "other"
    else:
        rows["artifacts"][1]["content_hash"] = "invalid"
    with pytest.raises(ValueError):
        validate_speech_copy_graph(rows)


def test_standalone_cast_ref_rejects_noncanonical_slots():
    rows = pure_graph()
    ref = rows["project_speech_references"][0]
    binding = CastSettings.model_validate(rows["knowledge_ledgers"][0]["payload_json"]["cast"]).narrator
    assert binding is not None
    for slot in ("tts", "segment:00000000-0000-0000-0000-000000000000", 'cast:["categories","alien"]'):
        with pytest.raises(ValueError):
            freeze_cast_reference({**ref, "slot_key": slot}, rows["session_settings"][0]["value_json"],
                binding=binding, owner=SpeechInputOwner("workspace", "project", "session"),
                artifacts={row["id"]: row for row in rows["artifacts"]}, edges=rows["artifact_edges"])


@pytest.mark.parametrize("version", range(1, 9))
def test_frozen_legacy_bytes_and_strict_new_kind_rejection(version):
    families = set(publisher._FIELDS) - SOUND_FAMILIES - {"performance_plans"} - {"audio_takes"}
    if version < 8:
        families -= {"generation_plans", "generation_plan_revisions", "generation_segments", "speech_plan_reviews"}
    if version < 5:
        families.remove("project_speech_references")
    if version == 1:
        families -= {"knowledge_ledgers", "subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 2:
        families -= {"subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 3:
        families -= {"media_edit_plans", "media_edit_plan_revisions"}
    state = {"schema_version": version, "rows": {name: [] for name in families}, "binding_fields": {}, "unsupported_counts": {}}
    if version in {6, 7, 8}:
        state["destination_connections"] = []
    text = canonical(state)
    snapshot = ProjectCopySnapshot("source", "destination", "Legacy", text, hashlib.sha256(text.encode()).hexdigest(), (), (), 0)
    publisher._decode(snapshot)
    if version in {6, 7, 8}:
        from pandrator.web.projects.copy_connections import validate_connection_state
        assert validate_connection_state(state, state["rows"]) == []
        with pytest.raises(ProjectCopyError):
            validate_connection_state({key: value for key, value in state.items() if key != "destination_connections"}, state["rows"])
    assert snapshot.state_json == text and snapshot.fingerprint == hashlib.sha256(text.encode()).hexdigest()
    if version < 7 and "knowledge_ledgers" in families:
        state["rows"]["knowledge_ledgers"] = [controls()]
        forged = canonical(state)
        with pytest.raises(ProjectCopyError):
            publisher._decode(replace(snapshot, state_json=forged, fingerprint=hashlib.sha256(forged.encode()).hexdigest()))

    if version < 7:
        state["rows"]["project_speech_references"] = [pure_graph()["project_speech_references"][0]]
        if "knowledge_ledgers" in families:
            state["rows"]["knowledge_ledgers"] = []
        forged = canonical(state)
        with pytest.raises(ProjectCopyError):
            publisher._decode(replace(snapshot, state_json=forged, fingerprint=hashlib.sha256(forged.encode()).hexdigest()))


def _context(f, client):
    from pandrator.web.access.contracts import AccessContext
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=client)))
    assert actor is not None
    return AccessContext(actor)


def test_no_controls_snapshot_avoids_cast_reads_retention_and_namespace(design, monkeypatch):
    f, users = design, design.users
    source = body(users.alice.post("/api/v1/sessions", json={
        "name": "No saved controls", "workflow_kind": "subtitles",
    }, headers=headers(users.ah, "cast-query-no-controls")), 201)
    context = _context(f, users.alice)

    def forbidden(*_args, **_kwargs):
        pytest.fail("No-controls snapshot crossed a cast, retention or namespace boundary")

    for target in (
        "pandrator.web.projects.copy_connections.destination_options",
        "pandrator.web.projects.copy_connections.provider_namespace",
        "pandrator.web.projects.copy_snapshot.freeze_cast_reference",
        "pandrator.web.projects.copy_snapshot._selected_settings",
        "pandrator.web.project_speech_reference_capture.capture_speech_input",
        "pandrator.web.speech_inputs.capture_speech_input",
        "pandrator.web.speech_inputs.prepare_speech_input",
    ):
        monkeypatch.setattr(target, forbidden)
    statements = []

    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with f.case.database.snapshot_session() as session:
            snapshot = capture.capture_current_project(
                session, context, source["project_id"], users.workspace_id,
                settings_service=f.services.workspace_settings,
            )
            assert snapshot.ready, snapshot.blockers
            assert not session.new and not session.dirty
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    state = json.loads(snapshot.state_json)
    assert state["rows"]["project_speech_references"] == []
    assert not any(row["kind"] == "generation_controls" for row in state["rows"]["knowledge_ledgers"])
    assert "generation_controls" not in snapshot.manifest()["counts"]
    assert snapshot.files == ()
    assert statements
    assert not any("project_speech_references.selector_json" in sql for sql in statements)
    assert not any("namespace" in sql.lower() for sql in statements)
    assert not any("voices.metadata_json" in sql or "voice_samples.transcript" in sql for sql in statements)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)


@pytest.mark.parametrize("workflow_kind", ["subtitles", "voiceover"])
def test_registered_copy_to_team_consumes_both_destination_pins_after_creator_leaves(design, monkeypatch, tmp_path, workflow_kind):
    f, users = design, design.users
    body(put(users.owner, users.oh, "services.tts", {"provider_configs": [{
        "id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-cast-copy-key", "credential_backend": "database",
    }]}, revision=1, workspace=users.workspace_id))
    source = body(users.alice.post("/api/v1/sessions", json={"name": "Private cast source", "workflow_kind": workflow_kind, "source_language": "en"}, headers=headers(users.ah, "cast-copy-source")), 201)
    settings_path = f"/api/v1/sessions/{source['id']}/settings/tts"
    body(users.alice.put(settings_path, json={"value": {
        "service": "audio_cpp", "model": MODEL, "voice": "", "speaker": "", "voice_id": None,
        "language": "en", "voice_mode_version": 1, "casting_enabled": True,
        "performance_enabled": False, "tts_concurrent_requests": 1,
    }}, headers={**headers(users.ah, "cast-copy-source-settings"), "If-Match": '"0"'}))
    voices, original_paths, samples = [], [], []
    for index in range(2):
        voice = body(create(users.alice, users.ah, f"Private character {index}"), 201)
        sample_id, _aid, path = sample(f.case, voice)
        with f.case.database.immediate_session() as session:
            session.get_one(m.Voice, voice["id"]).metadata_json = {"providers": {"audio_cpp": {
                "status": "ready", "resource_kind": "linked_reference", "voice_id": f"character-reference-{index}",
            }}}
            item = session.get_one(m.VoiceSample, sample_id)
            item.transcript, item.transcript_reviewed = f"{TRANSCRIPT} Character {index}.", True
        voices.append(voice)
        samples.append(sample_id)
        original_paths.append(path)
    characters = [{"id": "fred", "display_name": "Fred", "notes": "Keep Fred's prose."},
                  {"id": "wilma", "display_name": "Wilma", "notes": "Keep Wilma's prose."}]
    cast = {"characters": {name: {"voice_id": voice["id"]} for name, voice in zip(("fred", "wilma"), voices, strict=True)}}
    controls_path = f"/api/v1/sessions/{source['id']}/generation-controls"
    saved = body(users.alice.put(controls_path, json={"expected_revision": 0, "characters": characters, "cast": cast}, headers=headers(users.ah, "cast-copy-save")))
    for voice, sample_id, path in zip(voices, samples, original_paths, strict=True):
        response = users.alice.delete(f"/api/v1/voices/{voice['id']}/samples/{sample_id}", headers={**users.ah, "If-Match": '"1"'})
        assert response.status_code == 200, response.get_json()
        assert not path.exists()
    context = _context(f, users.alice)
    seed_copy_model(f.case, users.workspace_id)

    def no_library(*_args, **_kwargs):
        pytest.fail("Saved cast copy consulted its original private library")
    monkeypatch.setattr("pandrator.web.project_speech_reference_capture.capture_speech_input", no_library)
    monkeypatch.setattr("pandrator.web.speech_inputs.capture_speech_input", no_library)
    with f.case.database.snapshot_session() as session:
        snapshot = capture_with_connections(f.case, session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
        assert snapshot.ready, snapshot.blockers
        assert session.scalar(select(func.count()).select_from(m.GenerationPlan).where(m.GenerationPlan.session_id == source["id"])) == 0
    state = json.loads(snapshot.state_json)
    source_refs = deepcopy(state["rows"]["project_speech_references"])
    assert len(source_refs) == 2 and len({row["input_artifact_id"] for row in source_refs}) == 2
    assert snapshot.manifest()["counts"]["generation_controls"] == 1 and len(snapshot.files) == 4
    source_settings = body(users.alice.get(settings_path))
    source_files = {item.relative_path: hashlib.sha256((f.services.paths.root / item.relative_path).read_bytes()).hexdigest() for item in snapshot.files}

    # An unsupported cast slot cannot be reconstructed and blocks the copy; its
    # selector body is never read or projected.
    marker = "private body must stay omitted"
    orphan_slot = 'cast:["characters","orphan"]'
    with f.case.database.immediate_session() as session:
        session.add(m.ProjectSpeechReference(**{**deepcopy(source_refs[0]), "slot_key": orphan_slot, "selector_json": {"private": marker}}))
    with f.case.database.snapshot_session() as session:
        blocked = capture_with_connections(f.case, session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
    assert not blocked.ready and marker not in blocked.state_json
    assert json.loads(blocked.state_json)["unsupported_counts"]["project_speech_references"] == 1
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.ProjectSpeechReference, (source["id"], orphan_slot)))

    # A non-current segment slot belongs to excluded revision history: it is
    # omitted without blocking and without hydrating its selector body.
    segment_slot = "segment:00000000-0000-0000-0000-000000000000"
    with f.case.database.immediate_session() as session:
        session.add(m.ProjectSpeechReference(**{**deepcopy(source_refs[0]), "slot_key": segment_slot, "selector_json": {"private": marker}}))
    with f.case.database.snapshot_session() as session:
        omitted = capture_with_connections(f.case, session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
    omitted_state = json.loads(omitted.state_json)
    assert omitted.ready and marker not in omitted.state_json
    assert "project_speech_references" not in omitted_state["unsupported_counts"]
    assert all(row["slot_key"] != segment_slot for row in omitted_state["rows"]["project_speech_references"])
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.ProjectSpeechReference, (source["id"], segment_slot)))
    for missing in (False, True):
        with f.case.database.immediate_session() as session:
            reference = session.get_one(m.ProjectSpeechReference, (source["id"], source_refs[0]["slot_key"]))
            if missing:
                session.delete(reference)
            else:
                reference.input_artifact_id = reference.input_content_hash = None
        with f.case.database.snapshot_session() as session:
            blocked = capture_with_connections(f.case, session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
        blocked_rows = json.loads(blocked.state_json)
        if missing:
            assert not blocked.ready
            assert blocked_rows["rows"]["knowledge_ledgers"] == [] and blocked_rows["rows"]["project_speech_references"] == []
            assert blocked_rows["unsupported_counts"]["knowledge_ledgers"] == 1
        else:
            assert blocked.ready, blocked.blockers
            assert len(blocked_rows["rows"]["knowledge_ledgers"]) == 1
            assert len(blocked_rows["rows"]["project_speech_references"]) == 2
            assert sum(ref["input_artifact_id"] is None for ref in blocked_rows["rows"]["project_speech_references"]) == 1
        with f.case.database.immediate_session() as session:
            old = session.get(m.ProjectSpeechReference, (source["id"], source_refs[0]["slot_key"]))
            if old is None:
                session.add(m.ProjectSpeechReference(**deepcopy(source_refs[0])))
            else:
                old.input_artifact_id = source_refs[0]["input_artifact_id"]
                old.input_content_hash = source_refs[0]["input_content_hash"]

    # Observe the registered copy and its preview. No capture, credential/library
    # read or protected row insert can hide behind the synthetic transport.
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        initial = body(users.alice.get(f"/api/v1/projects/{source['project_id']}/copy-preview", query_string={"workspace_id": users.workspace_id}))
        assert initial["ready"]
        choices = snapshot_destination_connections(snapshot)
        preview = body(users.alice.post(f"/api/v1/projects/{source['project_id']}/copy-preview", json={"destination_workspace_id": users.workspace_id, "destination_connections": choices}, headers=headers(users.ah, "cast-copy-preview")))
        assert preview["ready"] and preview["fingerprint"] == snapshot.fingerprint
        preview_statements = list(statements)
        choices = snapshot_destination_connections(snapshot)
        queued = body(users.alice.post(f"/api/v1/projects/{source['project_id']}/copy", json={
            "destination_workspace_id": users.workspace_id, "expected_fingerprint": preview["fingerprint"],
            "name": snapshot.name, "destination_connections": choices,
        }, headers=headers(users.ah, "cast-copy-registered")), 202)
        handlers = f.case.app.extensions["pandrator"]["workflow_handlers"]
        worker = Worker(f.services.jobs, "saved-cast-copy", handlers.handlers())
        assert worker.run_once()
        finished = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
        assert finished["state"] == "succeeded", finished
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    preview_writes = [sql for sql in preview_statements if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    (tmp_path / "preview-write-statements.json").write_text(json.dumps(preview_writes, indent=2) + "\n")
    assert not any(any(table in sql.lower() for table in ("artifacts", "project_speech_references", "knowledge_ledgers", "voices", "voice_samples")) for sql in preview_writes)
    assert not any("voices.metadata_json" in sql or "voice_samples.transcript" in sql or "workspace_credentials" in sql for sql in statements)
    cast_body_reads = [sql for sql in preview_statements if "project_speech_references.selector_json" in sql]
    # The helper previews twice to bind destination LLM selection, one body read
    # per snapshot, regardless of its two independent bindings.
    assert len(cast_body_reads) == 2
    (tmp_path / "controls-sql.json").write_text(json.dumps({"bindings": 2, "snapshots": 2,
        "cast_body_reads": len(cast_body_reads), "preview_statements": len(preview_statements)}, indent=2) + "\n")
    copied = finished["result"]
    copied_sid = copied["source_session_id"]
    with f.case.database.snapshot_session() as session:
        copied_refs = list(session.scalars(select(m.ProjectSpeechReference).where(m.ProjectSpeechReference.session_id == copied_sid)))
        assert len(copied_refs) == 2
        for original, reference in zip(sorted(source_refs, key=lambda row: row["slot_key"]), sorted(copied_refs, key=lambda row: row.slot_key), strict=True):
            assert reference.input_artifact_id == publisher.copy_identity(queued["id"], "artifacts", original["input_artifact_id"])
            assert reference.selector_json == original["selector_json"] and reference.binding_json == original["binding_json"]
            assert reference.selector_hash == original["selector_hash"] and reference.revision == 1
        assert session.scalar(select(func.count()).select_from(m.Voice)) == 2
        assert session.scalar(select(func.count()).select_from(m.VoiceSample)) == 0
        pins = {ref.input_artifact_id for ref in copied_refs}
    assert body(users.alice.get(settings_path)) == source_settings
    assert body(users.alice.get(controls_path)) == saved
    for relative, digest in source_files.items():
        assert hashlib.sha256((f.services.paths.root / relative).read_bytes()).hexdigest() == digest
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    for voice in voices:
        assert users.bob.get(f"/api/v1/voices/{voice['id']}").status_code == 404
    copied_controls = body(users.bob.get(f"/api/v1/sessions/{copied_sid}/generation-controls"))
    assert copied_controls["cast"] == saved["cast"] and copied_controls["characters"] == saved["characters"]
    bob = actor_from_principal(current_browser_principal(replace(f.case, client=users.bob)))
    assert bob is not None
    with execution_actor_scope(bob):
        f.services.generation.create_plan(copied_sid, source_revision_id=None, settings={}, segments=[{"text": "Fred speaks. Wilma replies.", "language": "en"}])
        segment_id = f.services.generation.list_segments(copied_sid)["items"][0]["id"]
        with f.case.database.immediate_session() as session:
            session.get_one(m.GenerationSegment, segment_id).speech_plan_json = {"speech_xml": f'<segment id="{segment_id}"><dialogue><speaker ref="fred">Fred speaks.</speaker></dialogue> <dialogue><speaker ref="wilma">Wilma replies.</speaker></dialogue></segment>'}
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", no_library)
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index", no_library)
    calls = native_transport(monkeypatch)
    admission = body(users.bob.post(f"/api/v1/sessions/{copied_sid}/generation-admissions", json={}, headers=headers(users.bh, "cast-copy-member-generation")), 202)
    assert worker.run_once()
    admitted = f.services.jobs.get(admission["id"])
    assert admitted.status == "succeeded", admitted.error_message
    assert worker.run_once()
    generated = f.services.jobs.get(admitted.result_json["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 2
    wire = canonical(calls)
    assert "Character 0." in wire and "Character 1." in wire
    with f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, admitted.result_json["generation_run_id"])
        assert pins <= {value for value in _strings(run.settings_snapshot_json)}
        takes = list(session.scalars(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)))
        assert takes and all(take.status == "completed" for take in takes)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def test_empty_controls_and_bounded_headers_precede_reference_bodies(design, monkeypatch):
    f, users = design, design.users
    source = body(users.alice.post("/api/v1/sessions", json={"name": "Empty dictionary", "workflow_kind": "subtitles"}, headers=headers(users.ah, "empty-controls-copy")), 201)
    seed_copy_model(f.case, users.workspace_id)
    row = controls({"characters": [{"id": "literal-id", "display_name": "No voice yet"}], "cast": {}})
    row["session_id"] = source["id"]
    row["id"] = "empty-controls-ledger"
    with f.case.database.immediate_session() as session:
        session.add(m.KnowledgeLedger(**row))
    context = _context(f, users.alice)
    with f.case.database.snapshot_session() as session:
        empty = capture_with_connections(f.case, session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
    assert empty.ready and not empty.files
    assert empty.manifest()["counts"]["generation_controls"] == 1
    assert json.loads(empty.state_json)["rows"]["project_speech_references"] == []
    with f.case.database.immediate_session() as session:
        for slot in ("cast:narrator", 'cast:["characters","orphan"]', "segment:00000000-0000-0000-0000-000000000000"):
            session.add(m.ProjectSpeechReference(**{**pure_graph()["project_speech_references"][0],
                "session_id": source["id"], "slot_key": slot, "input_artifact_id": None,
                "input_content_hash": None, "selector_json": {"private": "never hydrate"}}))
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    monkeypatch.setattr(capture, "MAX_ARTIFACTS", 2)
    try:
        with f.case.database.snapshot_session() as session, pytest.raises(ProjectCopyError) as invalid:
            capture.capture_current_project(session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
        assert invalid.value.code == "copy_limit_exceeded"
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert not any("project_speech_references.selector_json" in sql for sql in statements)


def test_legacy_unhashable_slot_keeps_value_error_semantics():
    ref = pure_graph()["project_speech_references"][0]
    with pytest.raises(ValueError, match="Unsupported saved speech reference slot"):
        remap_tts_reference({**ref, "slot_key": []}, {})


def test_v7_preserves_nonempty_destination_selection_and_requires_mapped_reproof():
    from pandrator.web.projects.copy_connections import (
        collect_requirements,
        validate_connection_state,
    )
    data: dict[str, Any] = {"sessions": [{"id": "session", "name": "English", "workflow_kind": "subtitles",
                          "workflow_preset": "custom", "included_stages_json": ["correct"]}],
            "session_settings": [{"session_id": "session", "section": "correction",
                                  "value_json": {"model_name": "custom:source/model"}},
                                 {"session_id": "session", "section": "translation", "value_json": {}},
                                 {"session_id": "session", "section": "text", "value_json": {}}],
            "outcome_plans": []}
    bindings = collect_requirements(data, set())
    assert len(bindings) == 1
    bindings[0].update(provider_id="00000000-0000-0000-0000-000000000002",
                       provider_model_id="00000000-0000-0000-0000-000000000003",
                       selector="custom:destination/approved-model",
                       provider_revision=1, model_revision=2)
    data["session_settings"][0]["value_json"]["model_name"] = bindings[0]["selector"]
    state = {"schema_version": 7, "rows": data, "binding_fields": {},
             "unsupported_counts": {}, "destination_connections": bindings}
    assert validate_connection_state(state, data) == bindings
    mapped = deepcopy(data)
    mapped["sessions"][0]["id"] = "destination-session"
    for setting in mapped["session_settings"]:
        setting["session_id"] = "destination-session"
    with pytest.raises(ProjectCopyError):
        validate_connection_state({**state, "rows": mapped}, mapped)
    mapped_bindings = deepcopy(bindings)
    mapped_bindings[0]["targets"][0]["session_id"] = "destination-session"
    assert validate_connection_state({**state, "rows": mapped,
        "destination_connections": mapped_bindings}, mapped) == mapped_bindings
    with pytest.raises(ProjectCopyError):
        validate_connection_state({**state, "destination_connections": []}, data)
