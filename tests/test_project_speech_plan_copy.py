"""Portable current editorial speech-plan copy boundary and retained-input proof."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_publication as publisher
from pandrator.web.projects import copy_snapshot as capture
from pandrator.web.projects.copy_contracts import ProjectCopyError, ProjectCopySnapshot
from pandrator.web.projects.copy_sound import SOUND_FAMILIES
from pandrator.web.projects.copy_speech_plan import (
    SPEECH_FAMILIES,
    editorial_revision_settings,
    freeze_speech_plan_rows,
    remap_speech_plan_rows,
    speech_plan_signature,
    validate_speech_plan_graph,
)
from pandrator.web.speech_plan_context import plan_signature
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
from tests.test_project_cast_copy import _context, canonical
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_settings_workspace_access import put
from tests.test_voice_crud_workspace_access import create, sample


def uid(number):
    return str(UUID(int=number))


def raw_graph():
    segment = {"id": uid(5), "plan_revision_id": uid(4), "ordinal": 0,
        "source_segment_ids_json": [1, uid(8)], "speech_block_provenance_json": {
            "turn_id": uid(2), "section_id": uid(9),
            "source_cues": [{"reference": uid(8), "start_ms": 10}],
            "manual_topology": {"parent_segment_ids": [uid(90)], "boundary_offset": 3},
            "resegmentation": {"source_revision_id": uid(91), "source_spans": [{"segment_id": uid(90)}], "ratio": 1.0},
            "formation_events": [{"left_segment_id": uid(90), "right_revision_id": uid(91), "length": 9}],
        }, "alignment_group": None, "node_kind": "paragraph", "paragraph_break_after": True,
        "speaker": "Fred", "text": "Hello Fred.", "optimized_text": "Hello Fred.",
        "speech_plan_json": {"id": uid(3), "plan_id": uid(4), "speech_plan_id": uid(9),
            "parent_speech_plan_ids": [uid(91)], "parent_segment_id": uid(90),
            "speech_xml": f'<segment id="{uid(5)}"><dialogue><speaker ref="fred">Hello Fred.</speaker></dialogue></segment>'},
        "optimization_status": "completed", "optimization_source_hash": "authored-hash",
        "optimization_reviewed": True, "optimization_model": "authored-model",
        "voice_id": None, "voice": None, "language": "en", "silence_after_ms": 250,
        "marked": True, "removed": False, "status": "completed", "revision": 7}
    return {"projects": [{"id": uid(1), "workspace_id": uid(10)}],
        "sessions": [{"id": uid(2), "project_id": uid(1)}],
        "documents": [{"id": uid(7), "session_id": uid(2)}],
        "document_revisions": [{"id": uid(8), "document_id": uid(7)}],
        "artifacts": [{"id": uid(9), "workspace_id": uid(10), "project_id": uid(1), "session_id": uid(2)}],
        "knowledge_ledgers": [{"id": uid(11), "session_id": uid(2), "kind": "generation_controls", "payload_json": {"characters": [{"id": "fred", "display_name": "Fred"}], "cast": {}}}],
        "generation_plans": [{"id": uid(3), "session_id": uid(2), "active_revision_id": uid(4)}],
        "generation_plan_revisions": [{"id": uid(4), "plan_id": uid(3), "source_revision_id": uid(8),
            "parent_revision_id": uid(91), "revision_number": 7,
            "settings_json": {"_source_artifact_id": uid(9)}, "operation_json": {"action": "edit", "job_id": uid(92)},
            "content_hash": "opaque source ingestion hash"}], "generation_segments": [segment],
        "speech_plan_reviews": [{"revision_id": uid(4), "content_hash": speech_plan_signature([segment])}]}


def frozen_graph():
    rows = raw_graph()
    return {**rows, **freeze_speech_plan_rows(rows, {uid(4): rows["generation_plan_revisions"][0]["settings_json"]})}


def test_pure_root_semantics_history_xml_digest_and_review_rebind():
    raw = raw_graph()
    before = deepcopy(raw)
    frozen = {**raw, **freeze_speech_plan_rows(raw, {uid(4): raw["generation_plan_revisions"][0]["settings_json"]})}
    assert raw == before
    validate_speech_plan_graph(frozen)
    revision = frozen["generation_plan_revisions"][0]
    assert revision["parent_revision_id"] is None and revision["revision_number"] == 1
    assert revision["operation_json"] == {"action": "copy_current", "version": 1, "source_content_hash": "opaque source ingestion hash"}
    segment = frozen["generation_segments"][0]
    assert segment["status"] == "ready" and segment["revision"] == 1
    assert segment["source_segment_ids_json"] == [1, uid(8)]
    provenance = segment["speech_block_provenance_json"]
    assert provenance["manual_topology"] == {"boundary_offset": 3}
    assert provenance["resegmentation"] == {"ratio": 1.0}
    assert provenance["formation_events"] == [{"length": 9}]
    identities = {row["id"]: uid(int(UUID(row["id"])) + 100) for values in frozen.values() for row in values if "id" in row}
    mapped = deepcopy(frozen)
    mapped.update(remap_speech_plan_rows(frozen, identities))
    for table in ("projects", "sessions", "documents", "document_revisions", "artifacts", "knowledge_ledgers"):
        for row in mapped[table]:
            for key in ("id", "project_id", "session_id", "document_id"):
                if key in row:
                    row[key] = identities[row[key]]
    validate_speech_plan_graph(mapped)
    copied = mapped["generation_segments"][0]
    assert copied["speech_block_provenance_json"]["turn_id"] == uid(2)
    assert copied["speech_block_provenance_json"]["section_id"] == uid(9)
    assert copied["speech_block_provenance_json"]["source_cues"][0]["reference"] == uid(8)
    assert copied["source_segment_ids_json"] == segment["source_segment_ids_json"]
    for key in ("id", "plan_id", "speech_plan_id", "parent_speech_plan_ids"):
        assert copied["speech_plan_json"][key] == segment["speech_plan_json"][key]
    assert copied["id"] in copied["speech_plan_json"]["speech_xml"]
    assert 'ref="fred"' in copied["speech_plan_json"]["speech_xml"]
    assert mapped["generation_plan_revisions"][0]["content_hash"] != revision["content_hash"]
    assert mapped["speech_plan_reviews"][0]["content_hash"] == speech_plan_signature(mapped["generation_segments"])
    assert mapped["speech_plan_reviews"][0]["content_hash"] != frozen["speech_plan_reviews"][0]["content_hash"]


@pytest.mark.parametrize("defect", ["extra", "duplicate_id", "duplicate_ordinal", "bool_ordinal", "bool_source_label", "voice", "json_list", "nonfinite", "secret", "unknown_reference", "xml_root", "source_revision", "source_artifact", "digest", "review", "ancestor", "orphan"])
def test_strict_pure_graph_rejects_invalid_current_state(defect):
    rows = frozen_graph()
    segment = rows["generation_segments"][0]
    revision = rows["generation_plan_revisions"][0]
    if defect == "extra":
        segment["extra"] = True
    elif defect == "duplicate_id":
        rows["generation_segments"].append(deepcopy(segment))
    elif defect == "duplicate_ordinal":
        duplicate = {**deepcopy(segment), "id": uid(50)}
        duplicate["speech_plan_json"]["speech_xml"] = duplicate["speech_plan_json"]["speech_xml"].replace(uid(5), uid(50))
        rows["generation_segments"].append(duplicate)
    elif defect == "bool_ordinal":
        segment["ordinal"] = True
    elif defect == "bool_source_label":
        segment["source_segment_ids_json"] = [True]
    elif defect == "voice":
        segment["voice"] = "explicit selection"
    elif defect == "json_list":
        segment["speech_block_provenance_json"] = []
    elif defect == "nonfinite":
        segment["speech_block_provenance_json"]["measurement"] = float("nan")
    elif defect == "secret":
        segment["speech_plan_json"]["api_key"] = "unsupported secret"
    elif defect == "unknown_reference":
        segment["speech_block_provenance_json"]["source_artifact_id"] = uid(99)
    elif defect == "xml_root":
        segment["speech_plan_json"]["speech_xml"] = segment["speech_plan_json"]["speech_xml"].replace(uid(5), uid(99))
    elif defect == "source_revision":
        rows["documents"][0]["session_id"] = uid(99)
    elif defect == "source_artifact":
        rows["artifacts"][0]["session_id"] = uid(99)
    elif defect == "digest":
        revision["content_hash"] = "forged"
    elif defect == "review":
        rows["speech_plan_reviews"][0]["content_hash"] = "old review"
    elif defect == "ancestor":
        revision["parent_revision_id"] = uid(91)
    else:
        segment["plan_revision_id"] = uid(99)
    with pytest.raises((ValueError, KeyError)):
        validate_speech_plan_graph(rows)


def test_outdated_review_is_not_promoted_and_invalid_source_xml_fails_before_rebind():
    rows = raw_graph()
    rows["speech_plan_reviews"][0]["content_hash"] = "outdated"
    assert freeze_speech_plan_rows(rows, {uid(4): rows["generation_plan_revisions"][0]["settings_json"]})["speech_plan_reviews"] == []
    rows["generation_segments"][0]["speech_plan_json"]["speech_xml"] = rows["generation_segments"][0]["speech_plan_json"]["speech_xml"].replace(uid(5), uid(99))
    with pytest.raises(ValueError):
        freeze_speech_plan_rows(rows, {uid(4): rows["generation_plan_revisions"][0]["settings_json"]})


@pytest.mark.parametrize("version", range(1, 8))
def test_legacy_frozen_formats_keep_bytes_and_reject_injected_speech_families(version):
    families = set(publisher._FIELDS) - SOUND_FAMILIES - {"performance_plans"} - SPEECH_FAMILIES - {"audio_takes"}
    if version < 5:
        families.remove("project_speech_references")
    if version == 1:
        families -= {"knowledge_ledgers", "subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 2:
        families -= {"subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 3:
        families -= {"media_edit_plans", "media_edit_plan_revisions"}
    state = {"schema_version": version, "rows": {key: [] for key in families}, "binding_fields": {}, "unsupported_counts": {}}
    if version in {6, 7}:
        state["destination_connections"] = []
    encoded = canonical(state)
    snapshot = ProjectCopySnapshot(uid(1), uid(10), "legacy", encoded, hashlib.sha256(encoded.encode()).hexdigest(), (), (), 0)
    decoded, _by_id, _ids = publisher._decode(snapshot)
    assert all(decoded[key] == [] for key in SPEECH_FAMILIES)
    assert snapshot.state_json == encoded and snapshot.fingerprint == hashlib.sha256(encoded.encode()).hexdigest()
    for family in SPEECH_FAMILIES:
        forged = canonical({**state, "rows": {**state["rows"], family: []}})
        with pytest.raises(ProjectCopyError):
            publisher._decode(replace(snapshot, state_json=forged, fingerprint=hashlib.sha256(forged.encode()).hexdigest()))


@pytest.fixture
def editorial(design):
    f, users = design, design.users
    source = body(users.alice.post("/api/v1/sessions", json={"name": "Current editorial source", "workflow_kind": "voiceover", "source_language": "en"}, headers=headers(users.ah, "editorial-copy-source")), 201)
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        plan = f.services.generation.create_plan(source["id"], source_revision_id=None, settings={}, segments=[{"text": "Editable current content.", "language": "en"}])
    return SimpleNamespace(f=f, users=users, source=source, actor=actor, plan=plan,
        context=_context(f, users.alice))


def capture_editorial(p):
    with p.f.case.database.snapshot_session() as session:
        return capture.capture_current_project(session, p.context, p.source["project_id"], p.users.workspace_id, settings_service=p.f.services.workspace_settings)


@pytest.mark.parametrize("kind", ["active_audio", "voice", "null_active"])
def test_ineligible_plans_are_counted_and_body_omitted(editorial, kind):
    p = editorial
    with p.f.case.database.immediate_session() as session:
        segment = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"]))
        assert segment is not None
        segment.text = "private speech body omitted"
        revision = session.get_one(m.GenerationPlanRevision, p.plan["active_revision_id"])
        revision.settings_json = {"private": "private revision body omitted"}
        if kind == "active_audio":
            session.add(m.AudioTake(generation_segment_id=segment.id, is_active=True, status="queued"))
        elif kind == "voice":
            segment.voice = "current selected voice"
        else:
            session.get_one(m.GenerationPlan, p.plan["id"]).active_revision_id = None
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(p.f.case.database.engine, "before_cursor_execute", observe)
    try:
        snapshot = capture_editorial(p)
    finally:
        event.remove(p.f.case.database.engine, "before_cursor_execute", observe)
    state = json.loads(snapshot.state_json)
    assert not snapshot.ready and state["unsupported_counts"]["generation_plans"] == 1
    assert all(state["rows"][key] == [] for key in SPEECH_FAMILIES)
    assert "private speech body omitted" not in snapshot.state_json
    assert "private revision body omitted" not in snapshot.state_json
    assert "private reference body omitted" not in snapshot.state_json
    assert not any("generation_plan_revisions.settings_json" in sql or "generation_segments.text" in sql or "speech_plan_reviews.content_hash" in sql for sql in statements)


@pytest.mark.parametrize("defect", ["dangling_active", "foreign_document", "foreign_artifact"])
def test_source_ownership_and_active_pointer_inconsistency_fails(editorial, defect):
    p = editorial
    other = body(p.users.alice.post("/api/v1/sessions", json={"name": "Other owner scope", "workflow_kind": "voiceover"}, headers=headers(p.users.ah, "editorial-foreign-root")), 201)
    with p.f.case.database.immediate_session() as session:
        revision = session.get_one(m.GenerationPlanRevision, p.plan["active_revision_id"])
        if defect == "dangling_active":
            session.get_one(m.GenerationPlan, p.plan["id"]).active_revision_id = new_id()
        elif defect == "foreign_document":
            doc = m.Document(session_id=other["id"], stage="source")
            session.add(doc)
            session.flush()
            foreign = m.DocumentRevision(document_id=doc.id, revision_number=1, content_hash="source")
            session.add(foreign)
            session.flush()
            revision.source_revision_id = foreign.id
    if defect == "foreign_artifact":
        path = p.f.services.paths.uploads / "foreign-owned.txt"
        path.write_text("Foreign source file")
        with execution_actor_scope(p.actor):
            artifact = p.f.services.artifacts.register(path, kind="txt", role="source", session_id=other["id"])
        with p.f.case.database.immediate_session() as session:
            session.get_one(m.GenerationPlanRevision, p.plan["active_revision_id"]).settings_json = {"_source_artifact_id": artifact.id}
    with pytest.raises(ProjectCopyError) as invalid:
        capture_editorial(p)
    assert invalid.value.code == "copy_graph_invalid"


@pytest.mark.parametrize("extra_headers", [0, 2])
def test_segment_limit_fails_before_speech_body_hydration(editorial, monkeypatch, extra_headers):
    p = editorial
    with p.f.case.database.immediate_session() as session:
        for ordinal in range(1, extra_headers + 1):
            session.add(m.GenerationSegment(plan_revision_id=p.plan["active_revision_id"], ordinal=ordinal, text="Bounded body must remain omitted"))
    monkeypatch.setattr(capture, "MAX_EDITORIAL_ROWS", 2)
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(p.f.case.database.engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(ProjectCopyError) as invalid:
            capture_editorial(p)
        assert invalid.value.code == "copy_limit_exceeded"
    finally:
        event.remove(p.f.case.database.engine, "before_cursor_execute", observe)
    assert not any("generation_plan_revisions.settings_json" in sql or "generation_segments.text" in sql for sql in statements)


def test_no_plan_replaces_count_with_one_header_and_skips_new_bodies(design, monkeypatch, tmp_path):
    f, users = design, design.users
    source = body(users.alice.post("/api/v1/sessions", json={"name": "No speech state", "workflow_kind": "subtitles"}, headers=headers(users.ah, "no-plan-copy-source")), 201)
    context = _context(f, users.alice)
    def forbidden(*_a, **_k):
        pytest.fail("No-plan capture crossed retention or destination namespace")
    for target in ("pandrator.web.projects.copy_connections.destination_options",
                   "pandrator.web.projects.copy_connections.provider_namespace",
                   "pandrator.web.speech_inputs.capture_speech_input",
                   "pandrator.web.project_speech_reference_capture.capture_speech_input"):
        monkeypatch.setattr(target, forbidden)
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with f.case.database.snapshot_session() as session:
            snapshot = capture.capture_current_project(session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
            assert not session.new and not session.dirty
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert snapshot.ready and not snapshot.files
    state = json.loads(snapshot.state_json)
    assert all(state["rows"][key] == [] for key in SPEECH_FAMILIES)
    reads = [sql for sql in statements if sql.lstrip().lower().startswith("select")]
    headers_sql = [sql for sql in reads if "from generation_plans" in sql.lower()]
    assert len(headers_sql) == 1 and "count(" not in headers_sql[0].lower()
    assert not any(any("from " + table in sql.lower() for table in ("generation_plan_revisions", "generation_segments", "speech_plan_reviews", "audio_takes")) for sql in reads)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    (tmp_path / "no-plan-sql.json").write_text(json.dumps({"statements": len(statements), "plan_headers": len(headers_sql), "writes": 0}, indent=2) + "\n")


def test_changed_source_editorial_content_rejects_stale_preview(editorial):
    p = editorial
    snapshot = capture_editorial(p)
    assert snapshot.ready, snapshot.blockers
    with p.f.case.database.immediate_session() as session:
        segment = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"]))
        assert segment is not None
        segment.text = "New authored content after preview."
    response = p.users.alice.post(f"/api/v1/projects/{p.source['project_id']}/copy", json={
        "destination_workspace_id": p.users.workspace_id, "expected_fingerprint": snapshot.fingerprint,
    }, headers=headers(p.users.ah, "stale-editorial-copy"))
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error"]["code"] == "copy_preview_stale"


@pytest.fixture
def retained_editorial(editorial):
    p, f, users = editorial, editorial.f, editorial.users
    body(put(users.owner, users.oh, "services.tts", {"provider_configs": [{
        "id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-current-plan-key", "credential_backend": "database",
    }]}, revision=1, workspace=users.workspace_id))
    body(users.alice.put(f"/api/v1/sessions/{p.source['id']}/settings/tts", json={"value": {
        "service": "audio_cpp", "model": MODEL, "voice": "", "speaker": "", "voice_id": None,
        "language": "en", "voice_mode_version": 1, "casting_enabled": True,
        "performance_enabled": False, "tts_concurrent_requests": 1,
    }}, headers={**headers(users.ah, "editorial-source-tts"), "If-Match": '"0"'}))
    voices, samples = [], []
    for index in range(2):
        voice = body(create(users.alice, users.ah, f"Private editorial character {index}"), 201)
        sample_id, _artifact_id, path = sample(f.case, voice)
        with f.case.database.immediate_session() as session:
            session.get_one(m.Voice, voice["id"]).metadata_json = {"providers": {"audio_cpp": {
                "status": "ready", "resource_kind": "linked_reference", "voice_id": f"editorial-reference-{index}",
            }}}
            item = session.get_one(m.VoiceSample, sample_id)
            item.transcript, item.transcript_reviewed = f"{TRANSCRIPT} Character {index}.", True
        voices.append(voice)
        samples.append((voice, sample_id, path))
    p.controls_path = f"/api/v1/sessions/{p.source['id']}/generation-controls"
    p.controls = body(users.alice.put(p.controls_path, json={"expected_revision": 0,
        "characters": [{"id": "fred", "display_name": "Fred"}, {"id": "wilma", "display_name": "Wilma"}],
        "cast": {"characters": {name: {"voice_id": voice["id"]} for name, voice in zip(("fred", "wilma"), voices, strict=True)}},
    }, headers=headers(users.ah, "editorial-save-cast")))
    for voice, sample_id, path in samples:
        response = users.alice.delete(f"/api/v1/voices/{voice['id']}/samples/{sample_id}", headers={**users.ah, "If-Match": '"1"'})
        assert response.status_code == 200 and not path.exists()
    with f.case.database.immediate_session() as session:
        document = m.Document(session_id=p.source["id"], stage="source", language="en")
        session.add(document)
        session.flush()
        first = m.DocumentRevision(document_id=document.id, revision_number=1, content_hash="nonactive-document-source")
        latest = m.DocumentRevision(document_id=document.id, revision_number=2, content_hash="active-document-source")
        session.add_all([first, latest])
        session.flush()
        document.active_revision_id = latest.id
        p.document_source_id = first.id
    source_path = f.services.paths.uploads / "editorial-source.txt"
    source_path.write_text("Fred speaks. Wilma replies.", encoding="utf-8")
    with execution_actor_scope(p.actor):
        source_artifact = f.services.artifacts.register(source_path, kind="txt", role="source", session_id=p.source["id"])
    with f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, source_artifact.id).state = "superseded"
    p.source_artifact_id = source_artifact.id
    p.old_revision_id = p.plan["active_revision_id"]
    with execution_actor_scope(p.actor):
        p.plan = f.services.generation.create_plan(p.source["id"], source_revision_id=p.document_source_id,
            settings={"_source_artifact_id": source_artifact.id, "speech_block_max_chars": 350},
            segments=[{"text": "Fred speaks. Wilma replies.", "language": "en", "source_segment_ids": [1, p.document_source_id]},
                      {"text": "Removed authored text.", "language": "en", "source_segment_ids": [2]}])
    with f.case.database.immediate_session() as session:
        segments = list(session.scalars(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"]).order_by(m.GenerationSegment.ordinal)))
        first, removed = segments
        first.optimized_text = first.text
        first.optimization_status, first.optimization_reviewed = "completed", True
        first.optimization_model = "source-model-label"
        first.silence_after_ms, first.marked, first.paragraph_break_after = 250, True, True
        first.status, first.revision = "completed", 7
        first.speech_block_provenance_json = {"turn_id": p.source["id"], "section_id": source_artifact.id,
            "source_cues": [{"reference": p.document_source_id, "start_ms": 0}],
            "manual_topology": {"parent_revision_id": p.old_revision_id, "boundary_offset": 4}}
        first.speech_plan_json = {"id": p.plan["id"], "plan_id": p.old_revision_id,
            "parent_speech_plan_ids": [p.old_revision_id], "parent_revision_id": p.old_revision_id,
            "speech_xml": f'<segment id="{first.id}"><dialogue><speaker ref="fred">Fred speaks.</speaker></dialogue> <dialogue><speaker ref="wilma">Wilma replies.</speaker></dialogue></segment>'}
        removed.removed, removed.marked, removed.status = True, True, "completed"
        session.flush()
        signature = plan_signature(session, p.plan["active_revision_id"])
        session.add(m.SpeechPlanReview(revision_id=p.plan["active_revision_id"], content_hash=signature))
        p.source_signature = signature
    p.voices = voices
    seed_copy_model(f.case, users.workspace_id)
    return p


def test_unretained_cast_blocks_its_xml_plan_without_loading_plan_bodies(retained_editorial):
    p = retained_editorial
    with p.f.case.database.immediate_session() as session:
        reference = session.scalar(select(m.ProjectSpeechReference).where(m.ProjectSpeechReference.session_id == p.source["id"]).order_by(m.ProjectSpeechReference.slot_key))
        assert reference is not None
        # A null pin is a retained explicit selection. An absent binding row
        # cannot be reconstructed, so its controls ledger and XML plan must block.
        session.delete(reference)
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(p.f.case.database.engine, "before_cursor_execute", observe)
    try:
        snapshot = capture_editorial(p)
        public = body(p.users.alice.get(f"/api/v1/projects/{p.source['project_id']}/copy-preview", query_string={"workspace_id": p.users.workspace_id}))
    finally:
        event.remove(p.f.case.database.engine, "before_cursor_execute", observe)
    assert not snapshot.ready and not public["ready"]
    state = json.loads(snapshot.state_json)
    assert state["unsupported_counts"]["generation_plans"] == 1
    assert state["unsupported_counts"]["knowledge_ledgers"] == 1
    assert state["rows"]["knowledge_ledgers"] == []
    assert all(state["rows"][key] == [] for key in SPEECH_FAMILIES)
    assert not any("generation_plan_revisions.settings_json" in sql or "generation_segments.text" in sql or "speech_plan_reviews.content_hash" in sql for sql in statements)


def test_registered_team_copy_current_plan_member_edit_review_and_fresh_generation(retained_editorial, monkeypatch, tmp_path):
    p, f, users = retained_editorial, retained_editorial.f, retained_editorial.users
    def no_library(*_a, **_k):
        pytest.fail("Current editorial copy consulted the original private library")
    monkeypatch.setattr("pandrator.web.project_speech_reference_capture.capture_speech_input", no_library)
    monkeypatch.setattr("pandrator.web.speech_inputs.capture_speech_input", no_library)
    with f.case.database.snapshot_session() as session:
        snapshot = capture_with_connections(f.case, session, p.context, p.source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
        source_rows = {table: [dict(row) for row in session.execute(select(model.__table__)).mappings()] for table, model in (
            ("plans", m.GenerationPlan), ("revisions", m.GenerationPlanRevision), ("segments", m.GenerationSegment), ("reviews", m.SpeechPlanReview), ("controls", m.KnowledgeLedger))}
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    assert len(state["rows"]["generation_plan_revisions"]) == 1 and len(state["rows"]["generation_segments"]) == 2
    assert len(state["rows"]["speech_plan_reviews"]) == 1
    assert p.old_revision_id not in {row["id"] for row in state["rows"]["generation_plan_revisions"]}
    assert p.document_source_id in {row["id"] for row in state["rows"]["document_revisions"]}
    assert p.source_artifact_id in {row["id"] for row in state["rows"]["artifacts"]}
    source_files = {item.relative_path: hashlib.sha256((f.services.paths.root / item.relative_path).read_bytes()).hexdigest() for item in snapshot.files}
    choices = snapshot_destination_connections(snapshot)
    queued = body(users.alice.post(f"/api/v1/projects/{p.source['project_id']}/copy", json={
        "destination_workspace_id": users.workspace_id, "expected_fingerprint": snapshot.fingerprint,
        "destination_connections": choices,
    }, headers=headers(users.ah, "registered-editorial-copy")), 202)
    handlers = f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(f.services.jobs, "current-editorial-copy", handlers.handlers())
    assert worker.run_once()
    finished = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert finished["state"] == "succeeded", finished
    target = finished["result"]["source_session_id"]
    with f.case.database.snapshot_session() as session:
        copied_plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == target))
        assert copied_plan is not None
        copied_revision = session.get_one(m.GenerationPlanRevision, copied_plan.active_revision_id)
        assert copied_revision.parent_revision_id is None and copied_revision.revision_number == 1
        assert copied_revision.operation_json["source_content_hash"] == state["rows"]["generation_plan_revisions"][0]["operation_json"]["source_content_hash"]
        copied_segments = list(session.scalars(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == copied_revision.id).order_by(m.GenerationSegment.ordinal)))
        assert len(copied_segments) == 2 and copied_segments[1].removed and copied_segments[1].marked
        assert all(row.revision == 1 and row.status == "ready" for row in copied_segments)
        first = copied_segments[0]
        assert first.source_segment_ids_json == [1, p.document_source_id]
        assert first.speech_block_provenance_json["turn_id"] == p.source["id"]
        assert first.speech_block_provenance_json["section_id"] == p.source_artifact_id
        assert first.speech_plan_json["id"] == p.plan["id"] and first.speech_plan_json["plan_id"] == p.old_revision_id
        assert "parent_revision_id" not in first.speech_plan_json
        assert first.id in first.speech_plan_json["speech_xml"] and 'ref="fred"' in first.speech_plan_json["speech_xml"]
        assert copied_revision.settings_json["_source_artifact_id"] == publisher.copy_identity(queued["id"], "artifacts", p.source_artifact_id)
        assert copied_revision.source_revision_id == publisher.copy_identity(queued["id"], "document_revisions", p.document_source_id)
        assert session.get_one(m.SpeechPlanReview, copied_revision.id).content_hash == plan_signature(session, copied_revision.id)
        assert session.scalar(select(func.count()).select_from(m.GenerationPlanRevision).where(m.GenerationPlanRevision.plan_id == copied_plan.id)) == 1
        assert session.scalar(select(func.count()).select_from(m.GenerationSegmentRevision).where(m.GenerationSegmentRevision.generation_segment_id.in_([row.id for row in copied_segments]))) == 0
        assert session.scalar(select(func.count()).select_from(m.GenerationRun)) == 0
        assert session.scalar(select(func.count()).select_from(m.Voice)) == 2
        assert session.scalar(select(func.count()).select_from(m.VoiceSample)) == 0
        copied_pins = set(session.scalars(select(m.ProjectSpeechReference.input_artifact_id).where(m.ProjectSpeechReference.session_id == target)))
        assert len(copied_pins) == 2
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(f"/api/v1/sessions/{p.source['id']}").status_code == 404
    public = body(users.bob.get(f"/api/v1/sessions/{target}/generation-segments"))
    assert len(public["items"]) == 2
    editable = next(item for item in public["items"] if not item["removed"])
    changed = users.bob.patch(f"/api/v1/sessions/{target}/generation-segments", json={"updates": [{
        "id": editable["id"], "revision": editable["revision"], "changes": {"silence_after_ms": 450},
    }]}, headers=headers(users.bh, "member-edit-copied-editorial"))
    assert changed.status_code == 200, changed.get_json()
    with f.case.database.snapshot_session() as session:
        active = session.scalar(select(m.GenerationPlan.active_revision_id).where(m.GenerationPlan.session_id == target))
        assert active is not None
        signature = plan_signature(session, active)
    reviewed = body(users.bob.post(f"/api/v1/sessions/{target}/generation-plan/review", json={
        "revision_id": active, "content_signature": signature,
    }, headers=headers(users.bh, "member-review-copied-editorial")))
    assert reviewed["reviewed"]
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", no_library)
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index", no_library)
    calls = native_transport(monkeypatch)
    admission = body(users.bob.post(f"/api/v1/sessions/{target}/generation-admissions", json={}, headers=headers(users.bh, "generate-copied-current-editorial")), 202)
    assert worker.run_once()
    admitted = f.services.jobs.get(admission["id"])
    assert admitted.status == "succeeded", admitted.error_message
    assert worker.run_once()
    generated = f.services.jobs.get(admitted.result_json["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 2 and "Character 0." in canonical(calls) and "Character 1." in canonical(calls)
    with f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, admitted.result_json["generation_run_id"])
        assert run.plan_revision_id == active
        takes = list(session.scalars(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)))
        assert len(takes) == 1 and takes[0].status == "completed"
        for pin in copied_pins:
            assert session.get(m.ArtifactEdge, (pin, takes[0].artifact_id)) is not None
        for table, model in (("plans", m.GenerationPlan), ("revisions", m.GenerationPlanRevision), ("segments", m.GenerationSegment), ("reviews", m.SpeechPlanReview), ("controls", m.KnowledgeLedger)):
            actual = [dict(row) for row in session.execute(select(model.__table__)).mappings()]
            ids = {row.get("id", row.get("revision_id")) for row in source_rows[table]}
            assert [row for row in actual if row.get("id", row.get("revision_id")) in ids] == source_rows[table]
    for relative, digest in source_files.items():
        assert hashlib.sha256((f.services.paths.root / relative).read_bytes()).hexdigest() == digest
    assert body(users.alice.get(p.controls_path)) == p.controls
    (tmp_path / "registered-editorial-proof.json").write_text(json.dumps({"copied_segments": 2,
        "copied_pins": 2, "speech_requests": len(calls), "source_files": len(source_files)}, indent=2) + "\n")


def test_empty_codec_does_not_walk_unrelated_rows_and_orphans_still_fail(monkeypatch):
    from pandrator.web.projects import copy_speech_plan as codec
    def forbidden(*_a, **_k):
        pytest.fail("Empty speech codec walked unrelated project rows")
    with monkeypatch.context() as patch:
        patch.setattr(codec, "_known", forbidden)
        patch.setattr(codec, "_characters", forbidden)
        assert freeze_speech_plan_rows({"generation_plans": []}, {}) == {key: [] for key in SPEECH_FAMILIES}
        assert remap_speech_plan_rows({"generation_plans": []}, {}) == {key: [] for key in SPEECH_FAMILIES}
    rows = frozen_graph()
    rows["generation_plans"] = []
    with pytest.raises(ValueError):
        validate_speech_plan_graph(rows)


def test_duplicate_active_revision_headers_fail_before_body_read(editorial):
    p = editorial
    with p.f.case.database.immediate_session() as session:
        identifier = new_id()
        session.add(m.SessionRecord(id=identifier, storage_key=identifier,
            project_id=p.source["project_id"], name="Additional current session", workflow_kind="voiceover", source_language="en"))
        session.flush()
        session.add(m.GenerationPlan(session_id=identifier, active_revision_id=p.plan["active_revision_id"]))
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(p.f.case.database.engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(ProjectCopyError) as invalid:
            capture_editorial(p)
        assert invalid.value.code == "copy_graph_invalid"
    finally:
        event.remove(p.f.case.database.engine, "before_cursor_execute", observe)
    assert not any("generation_plan_revisions.settings_json" in sql or "generation_segments.text" in sql for sql in statements)


def test_normal_prepared_routing_is_omitted_and_destination_route_remains_authoritative(design, monkeypatch, tmp_path):
    f, users = design, design.users
    private_route = {"id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://private-speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-private-prepared-route-key", "credential_backend": "database"}
    approved_route = {**private_route, "api_base": "https://speech.invalid", "api_key": "synthetic-approved-prepared-route-key"}
    body(put(users.alice, users.ah, "services.tts", {"provider_configs": [private_route]}))
    body(put(users.owner, users.oh, "services.tts", {"provider_configs": [approved_route]}, revision=1, workspace=users.workspace_id))
    source = body(users.alice.post("/api/v1/sessions", json={"name": "Normally prepared private speech", "workflow_kind": "voiceover", "source_language": "en"}, headers=headers(users.ah, "normal-prepare-source")), 201)
    base = f"/api/v1/sessions/{source['id']}"
    body(users.alice.put(base + "/settings/tts", json={"value": {
        "service": "audio_cpp", "model": MODEL, "voice": "", "speaker": "", "voice_id": None,
        "language": "en", "voice_mode_version": 1, "casting_enabled": True,
        "performance_enabled": False, "tts_concurrent_requests": 1,
    }}, headers={**headers(users.ah, "normal-prepare-tts"), "If-Match": '"0"'}))
    voice = body(create(users.alice, users.ah, "Private prepared narrator"), 201)
    sample_id, _sample_artifact, sample_path = sample(f.case, voice)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {"providers": {"audio_cpp": {
            "status": "ready", "resource_kind": "linked_reference", "voice_id": "private-prepared-reference",
        }}}
        selected = session.get_one(m.VoiceSample, sample_id)
        selected.transcript, selected.transcript_reviewed = TRANSCRIPT, True
    controls = body(users.alice.put(base + "/generation-controls", json={"expected_revision": 0,
        "characters": [], "cast": {"narrator": {"voice_id": voice["id"]}},
    }, headers=headers(users.ah, "normal-prepare-retained-narrator")))
    response = users.alice.delete(f"/api/v1/voices/{voice['id']}/samples/{sample_id}", headers={**users.ah, "If-Match": '"1"'})
    assert response.status_code == 200 and not sample_path.exists()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nA normally prepared opening.\n\n2\n00:00:04,000 --> 00:00:06,000\nThe destination continues this text.\n"
    body(users.alice.post("/api/v1/uploads", data={"file": (io.BytesIO(srt.encode()), "normal-source.srt"), "session_id": source["id"]}, headers=headers(users.ah, "normal-prepare-upload")), 201)
    outcome = body(users.alice.get(base + "/outcome-plan"))
    body(users.alice.put(base + "/outcome-plan", json={"value": {**outcome["value"], "inputs": {"generation": "source"}}}, headers={**users.ah, "If-Match": f'"{outcome["revision"]}"'}))
    status = body(users.alice.get(base + "/generation-plan/status"))
    assert status["current_input"] is not None
    prepared = body(users.alice.post(base + "/generation-plan/prepare", json={
        "expected_revision": status["session_revision"], "expected_plan_revision_id": status["selected_revision_id"],
        "source_artifact_id": status["current_input"]["artifact_id"],
    }, headers=headers(users.ah, "normal-prepare-api")))
    source_revision = prepared["selected_revision_id"]
    with f.case.database.snapshot_session() as session:
        signature = plan_signature(session, source_revision)
    body(users.alice.post(base + "/generation-plan/review", json={"revision_id": source_revision, "content_signature": signature}, headers=headers(users.ah, "normal-review-api")))
    source_status = body(users.alice.get(base + "/generation-plan/status"))
    assert source_status["can_generate"] and source_status["warning"] is None
    with f.case.database.snapshot_session() as session:
        original = dict(session.execute(select(m.GenerationPlanRevision.__table__).where(m.GenerationPlanRevision.id == source_revision)).mappings().one())
        original_settings_bytes = canonical(original["settings_json"]).encode()
        assert "provider_configs" in original["settings_json"]
        assert "private-speech.invalid" in original_settings_bytes.decode()
        assert "secret_ref" in original_settings_bytes.decode()
        assert original["settings_json"]["_source_artifact_id"] == source_status["current_input"]["artifact_id"]
        assert session.get_one(m.SpeechPlanReview, source_revision).content_hash == signature
        protected_models = (m.Provider, m.ProviderModel, m.StoredCredential, m.Voice, m.VoiceSample)
        protected_before = {model.__tablename__: session.scalar(select(func.count()).select_from(model)) for model in protected_models}
    def no_library(*_a, **_k):
        pytest.fail("Normal prepared copy reached private mutable library material")
    monkeypatch.setattr("pandrator.web.project_speech_reference_capture.capture_speech_input", no_library)
    monkeypatch.setattr("pandrator.web.speech_inputs.capture_speech_input", no_library)
    preview = body(users.alice.get(f"/api/v1/projects/{source['project_id']}/copy-preview", query_string={"workspace_id": users.workspace_id}))
    (tmp_path / "normal-prepare-source-shape.json").write_text(json.dumps({
        "root_provider_configs_present": True, "source_review_matches": True,
        "source_matches_current_input": source_status["can_generate"], "preview_ready": preview["ready"],
        "blockers": preview["blockers"], "original_settings_sha256": hashlib.sha256(original_settings_bytes).hexdigest(),
    }, indent=2) + "\n")
    assert preview["ready"], preview["blockers"]
    context = _context(f, users.alice)
    with f.case.database.snapshot_session() as session:
        snapshot = capture.capture_current_project(session, context, source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
    assert snapshot.ready and snapshot.fingerprint == preview["fingerprint"]
    frozen = json.loads(snapshot.state_json)
    frozen_revision = frozen["rows"]["generation_plan_revisions"][0]
    assert "provider_configs" not in frozen_revision["settings_json"]
    assert "private-speech.invalid" not in canonical(frozen_revision)
    assert snapshot.manifest()["ready"] and not json.loads(snapshot.state_json)["binding_fields"]
    # Rehashed injection must fail in the publication graph check before staging.
    from pandrator.web.projects.copy_speech_plan import speech_revision_digest
    forged = deepcopy(frozen)
    forged_revision = forged["rows"]["generation_plan_revisions"][0]
    forged_revision["settings_json"]["provider_configs"] = []
    forged_revision["content_hash"] = speech_revision_digest(forged_revision, forged["rows"]["generation_segments"])
    encoded = canonical(forged)
    bad = replace(snapshot, state_json=encoded, fingerprint=hashlib.sha256(encoded.encode()).hexdigest())
    with monkeypatch.context() as patches:
        patches.setattr(publisher, "_staging", lambda *_a, **_k: pytest.fail("Forged routing reached staging"))
        with f.case.database.immediate_session() as session, pytest.raises(ProjectCopyError) as invalid:
            publisher.publish_project_copy_rows(session, bad, cast(Any, object()), copy_id=new_id(), created_by_user_id=users.alice_id)
        assert invalid.value.code == "copy_graph_invalid"
    source_files = {item.relative_path: hashlib.sha256((f.services.paths.root / item.relative_path).read_bytes()).hexdigest() for item in snapshot.files}
    queued = body(users.alice.post(f"/api/v1/projects/{source['project_id']}/copy", json={
        "destination_workspace_id": users.workspace_id, "expected_fingerprint": preview["fingerprint"],
    }, headers=headers(users.ah, "normal-prepared-copy")), 202)
    handlers = f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(f.services.jobs, "normal-prepared-copy", handlers.handlers())
    assert worker.run_once()
    finished = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert finished["state"] == "succeeded", finished
    target = finished["result"]["source_session_id"]
    with f.case.database.snapshot_session() as session:
        copied_plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == target))
        assert copied_plan is not None
        copied = session.get_one(m.GenerationPlanRevision, copied_plan.active_revision_id)
        assert copied.revision_number == 1 and copied.parent_revision_id is None
        assert "provider_configs" not in copied.settings_json and "private-speech.invalid" not in canonical(copied.settings_json)
        assert copied.source_revision_id == publisher.copy_identity(queued["id"], "document_revisions", original["source_revision_id"])
        assert session.get_one(m.SpeechPlanReview, copied.id).content_hash == plan_signature(session, copied.id)
        assert session.scalar(select(func.count()).select_from(m.GenerationPlanRevision).where(m.GenerationPlanRevision.plan_id == copied_plan.id)) == 1
        assert session.scalar(select(func.count()).select_from(m.GenerationRun)) == 0
        assert {model.__tablename__: session.scalar(select(func.count()).select_from(model)) for model in protected_models} == protected_before
        pin = session.scalar(select(m.ProjectSpeechReference.input_artifact_id).where(m.ProjectSpeechReference.session_id == target))
        assert pin is not None
    # Retire the source route only after proving copy itself cloned no credentials.
    body(put(users.alice, users.ah, "services.tts", {"provider_configs": [{**private_route, "enabled": False}]}, revision=1))
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(base).status_code == 404
    copied_status = body(users.bob.get(f"/api/v1/sessions/{target}/generation-plan/status"))
    assert copied_status["can_generate"] and copied_status["warning"] is None
    with f.case.database.snapshot_session() as session:
        copied = session.get_one(m.GenerationPlanRevision, copied_status["selected_revision_id"])
        assert copied.settings_json["_source_artifact_id"] == copied_status["current_input"]["artifact_id"]
    assert body(users.bob.get(f"/api/v1/sessions/{target}/generation-controls"))["cast"] == controls["cast"]
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", no_library)
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index", no_library)
    calls = native_transport(monkeypatch)  # Exact approved Team endpoint only.
    admission = body(users.bob.post(f"/api/v1/sessions/{target}/generation-admissions", json={}, headers=headers(users.bh, "normal-prepared-member-generation")), 202)
    assert worker.run_once()
    admitted = f.services.jobs.get(admission["id"])
    assert admitted.status == "succeeded", admitted.error_message
    assert worker.run_once()
    generated = f.services.jobs.get(admitted.result_json["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert calls and all(TRANSCRIPT in canonical(call) for call in calls)
    with f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, admitted.result_json["generation_run_id"])
        takes = list(session.scalars(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)))
        assert takes and all(take.status == "completed" for take in takes)
        assert all(session.get(m.ArtifactEdge, (pin, take.artifact_id)) is not None for take in takes)
        after = dict(session.execute(select(m.GenerationPlanRevision.__table__).where(m.GenerationPlanRevision.id == source_revision)).mappings().one())
        assert after == original and canonical(after["settings_json"]).encode() == original_settings_bytes
        assert session.get_one(m.SpeechPlanReview, source_revision).content_hash == signature
    for relative, digest in source_files.items():
        assert hashlib.sha256((f.services.paths.root / relative).read_bytes()).hexdigest() == digest
    (tmp_path / "normal-prepare-copy-proof.json").write_text(json.dumps({"source_root_routing_preserved": True,
        "destination_root_routing_absent": True, "private_route_disabled": True, "approved_endpoint_only": True,
        "speech_requests": len(calls), "retained_pins": 1, "source_files_unchanged": len(source_files)}, indent=2) + "\n")


def test_editorial_revision_projection_is_detached_and_omits_only_known_root_routing():
    raw = {"provider_configs": [{"api_base": "https://source.invalid", "secret_ref": "source-reference"}],
        "_source_artifact_id": uid(9), "speech_block_max_chars": 350,
        "authored_metadata": {"provider_configs": {"label": "An authored label"}}}
    before = deepcopy(raw)
    projected = editorial_revision_settings(raw)
    assert projected == {key: value for key, value in before.items() if key != "provider_configs"}
    assert raw == before
    projected["authored_metadata"]["provider_configs"]["label"] = "Changed detached label"
    assert raw == before
    with pytest.raises(ValueError):
        editorial_revision_settings([])
    for key, value, expected_blocker in (
        ("api_base", "https://unapproved.invalid", ("destination_binding_required", "speech_plan")),
        ("llm_model", "custom:private/model", ("unsupported_state", "custom_settings")),
        ("voice_id", uid(99), ("unsupported_state", "reusable_editorial_dependency")),
    ):
        settings = editorial_revision_settings({**raw, key: value})
        assert settings[key] == value
        blockers, removed = set(), {}
        capture._settings(settings, {}, "speech_plan", {uid(9)}, blockers, removed)
        assert expected_blocker in blockers
    rows = raw_graph()
    rows["generation_plan_revisions"][0]["settings_json"] = deepcopy(raw)
    rows["generation_segments"][0]["speech_plan_json"]["provider_configs"] = {"label": "Segment-authored metadata"}
    frozen = {**rows, **freeze_speech_plan_rows(rows, {uid(4): editorial_revision_settings(raw)})}
    validate_speech_plan_graph(frozen)
    assert frozen["generation_segments"][0]["speech_plan_json"]["provider_configs"] == {"label": "Segment-authored metadata"}
    assert frozen["generation_plan_revisions"][0]["settings_json"]["authored_metadata"] == raw["authored_metadata"]


def test_rehashed_frozen_root_routing_injection_is_rejected():
    from pandrator.web.projects.copy_speech_plan import speech_revision_digest
    rows = frozen_graph()
    revision = rows["generation_plan_revisions"][0]
    revision["settings_json"]["provider_configs"] = []
    revision["content_hash"] = speech_revision_digest(revision, rows["generation_segments"])
    assert revision["content_hash"] == speech_revision_digest(revision, rows["generation_segments"])
    with pytest.raises(ValueError):
        validate_speech_plan_graph(rows)
