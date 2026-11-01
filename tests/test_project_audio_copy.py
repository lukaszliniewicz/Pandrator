"""Selected current audio is copied as owned bytes/evidence, never operational history."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from pandrator.logic.speech_performance import content_hash
from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.generation_cast_runtime import cast_resolution_key
from pandrator.web.generation_recast import _recorded_snapshots
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from pandrator.web.project_speech_references import (
    speech_reference_selector,
    speech_reference_selector_hash,
)
from pandrator.web.projects import copy_audio, copy_publication, copy_snapshot
from pandrator.web.projects.copy_contracts import ProjectCopyError, ProjectCopySnapshot
from pandrator.web.projects.copy_sound import SOUND_FAMILIES
from pandrator.web.projects.copy_speech_plan import (
    SPEECH_FAMILIES,
    freeze_speech_plan_rows,
    validate_speech_plan_graph,
)
from pandrator.web.recorded_speech_contracts import (
    PROVENANCE_KEY,
    project_recorded_snapshot,
    recorded_input_pins,
    remap_recorded_snapshot,
    validate_recorded_snapshot,
)
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY as BINDING,
)
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_INPUT_KEY as PIN,
)
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_SELECTOR_KEY as SELECTOR,
)
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_SOURCE_KEY as SOURCE,
)
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_cast_retention_integration import native_transport
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_project_cast_copy import _context, canonical
from tests.test_project_speech_plan_copy import editorial as editorial  # noqa: F401
from tests.test_project_speech_plan_copy import raw_graph
from tests.test_project_speech_plan_copy import (
    retained_editorial as retained_editorial,  # noqa: F401
)
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_settings_workspace_access import put
from tests.test_voice_crud_workspace_access import create, sample

TEXT = "Fred speaks. Wilma replies."


def uid(number):
    return str(UUID(int=number))


def source_snapshot(*, segment_id=None, unused=0):
    segment_id = segment_id or uid(5)
    choices = {"fred": {"voice_id": uid(1)}, "wilma": {"voice_id": uid(2)}}
    characters = [{"id": name, "display_name": name.title()} for name in choices]
    characters += [{"id": f"unused-{i}", "display_name": f"Unused {i}"} for i in range(unused)]
    cast = {"characters": {**choices, **{f"unused-{i}": {"voice_id": uid(60 + i)} for i in range(unused)}}}
    resolutions = {}
    for index, (name, choice) in enumerate(choices.items()):
        selector = speech_reference_selector(choice)
        binding = {"adapter": "audio_cpp", "model": MODEL, "voice": name + "-reference", "display_name": name.title()}
        pin = {"version": 1, "artifact_id": uid(20 + index * 2), "content_hash": str(index + 1) * 64}
        resolution = {**selector, "voice": binding["voice"], PIN: pin, BINDING: binding,
            SELECTOR: speech_reference_selector_hash(selector), "_recast_reference_fingerprint": [pin["content_hash"]],
            SOURCE: {"version": 2, "pin": deepcopy(pin), "binding": deepcopy(binding), "reference_identity": {
                "sample_id": uid(1), "artifact_id": uid(20), "content_hash": "a" * 64, "transcript_hash": "b" * 64}}}
        resolutions[cast_resolution_key(selector, f'cast:["characters","{name}"]', schema_version=2)] = resolution
    entry = {"text_hash": content_hash(TEXT), "speech_xml": f'<segment id="{segment_id}"><dialogue><speaker ref="fred">Fred speaks.</speaker></dialogue> <dialogue><speaker ref="wilma">Wilma replies.</speaker></dialogue></segment>',
        "source_speaker": None, "subtitle_timed": False, "voice_binding": None}
    return {"tts": {"service": "audio_cpp", "model": MODEL, "voice": "", "speaker": "", "voice_id": None,
        "casting_enabled": True, "performance_enabled": False, "voice_mode_version": 1,
        "provider_configs": [{"api_base": "https://never-copied.invalid", "secret_ref": "never-copied"}]},
        "generation_control_snapshot": {"schema_version": 2, "plan_revision_id": uid(4),
            "controls": {"id": uid(11), "session_id": uid(2), "revision": 9, "characters": characters, "cast": cast},
            "segments": {segment_id: entry}, "resolved_bindings": resolutions}}


def evidence():
    return project_recorded_snapshot(source_snapshot(), entry_id=uid(5), segment_id=uid(5), revision_id=uid(4), text=TEXT)


def test_recorded_projection_is_pruned_closed_and_preserves_audible_semantics():
    source = source_snapshot(unused=1000)
    before = deepcopy(source)
    selected = project_recorded_snapshot(source, entry_id=uid(5), segment_id=uid(5), revision_id=uid(4), text=TEXT)
    assert source == before
    minimal = evidence()
    assert selected == minimal
    frozen = selected["generation_control_snapshot"]
    assert [item["id"] for item in frozen["controls"]["characters"]] == ["fred", "wilma"]
    assert set(frozen["controls"]["cast"]["characters"]) == {"fred", "wilma"}
    assert len(canonical(selected).encode()) < 8000
    assert len(frozen["resolved_bindings"]) == 2 and len(recorded_input_pins(selected)) == 2
    assert "never-copied" not in canonical(selected) and "session_id" not in frozen["controls"]
    assert set(frozen["segments"][uid(5)]) == {"text_hash", "speech_xml", "source_speaker", "subtitle_timed", "voice_binding"}
    identities = {uid(20): uid(120), uid(22): uid(122), uid(1): uid(101), uid(2): uid(102)}
    mapped = remap_recorded_snapshot(selected, identities, segment_id=uid(105), revision_id=uid(104), text=TEXT)
    assert mapped["generation_control_snapshot"]["plan_revision_id"] == uid(104)
    assert uid(105) in mapped["generation_control_snapshot"]["segments"][uid(105)]["speech_xml"]
    assert 'ref="fred"' in mapped["generation_control_snapshot"]["segments"][uid(105)]["speech_xml"]
    for original, target in zip(frozen["resolved_bindings"].values(), mapped["generation_control_snapshot"]["resolved_bindings"].values(), strict=True):
        assert target["voice_id"] == original["voice_id"]
        assert target[PIN]["artifact_id"] == identities[original[PIN]["artifact_id"]]
        assert target[SOURCE]["pin"]["artifact_id"] == target[PIN]["artifact_id"]
        assert target[SOURCE]["reference_identity"] == original[SOURCE]["reference_identity"]
        assert target[SELECTOR] == original[SELECTOR] and target["_recast_reference_fingerprint"] == original["_recast_reference_fingerprint"]


@pytest.mark.parametrize("defect", ["outer_extra", "version_bool", "version1", "entry_extra", "text_hash", "xml_root", "voice_binding", "resolution_extra", "pin_null", "pin_hash", "selector_hash", "binding_adapter", "fingerprint", "unknown_resolution", "controls_extra", "duplicate_character", "padded_controls", "source_identity", "nonfinite"])
def test_closed_recorded_snapshot_rejects_invalid_evidence(defect):
    value = evidence()
    frozen = value["generation_control_snapshot"]
    entry = frozen["segments"][uid(5)]
    resolution = next(iter(frozen["resolved_bindings"].values()))
    if defect == "outer_extra":
        value["api_base"] = ""
    elif defect == "version_bool":
        frozen["schema_version"] = True
    elif defect == "version1":
        frozen["schema_version"] = 1
    elif defect == "entry_extra":
        entry["resolved_bindings"] = {}
    elif defect == "text_hash":
        entry["text_hash"] = "f" * 64
    elif defect == "xml_root":
        entry["speech_xml"] = entry["speech_xml"].replace(uid(5), uid(99))
    elif defect == "voice_binding":
        entry["voice_binding"] = {"voice": "unadapted segment voice"}
    elif defect == "resolution_extra":
        resolution["api_base"] = ""
    elif defect == "pin_null":
        resolution[PIN] = None
    elif defect == "pin_hash":
        resolution[PIN]["content_hash"] = "invalid"
    elif defect == "selector_hash":
        resolution[SELECTOR] = "f" * 64
    elif defect == "binding_adapter":
        resolution[BINDING]["adapter"] = "other"
    elif defect == "fingerprint":
        resolution["_recast_reference_fingerprint"] = ["f" * 64]
    elif defect == "unknown_resolution":
        frozen["resolved_bindings"][uid(77)] = deepcopy(resolution)
    elif defect == "controls_extra":
        frozen["controls"]["session_id"] = uid(2)
    elif defect == "duplicate_character":
        frozen["controls"]["characters"].append(deepcopy(frozen["controls"]["characters"][0]))
    elif defect == "padded_controls":
        unused = deepcopy(frozen["controls"]["characters"][0])
        unused["id"] = "unused"
        frozen["controls"]["characters"].append(unused)
    elif defect == "source_identity":
        resolution[SOURCE]["reference_identity"]["content_hash"] = "bad"
    else:
        value["tts"]["unrelated"] = float("nan")
    with pytest.raises((ValueError, TypeError, KeyError)):
        validate_recorded_snapshot(value, segment_id=uid(5), revision_id=uid(4), text=TEXT)


def audio_graph(*, shared=False):
    rows = raw_graph()
    segment = rows["generation_segments"][0]
    segment.update(text=TEXT, optimized_text=TEXT, source_segment_ids_json=[1], speech_block_provenance_json={},
                   speech_plan_json={"speech_xml": source_snapshot()["generation_control_snapshot"]["segments"][uid(5)]["speech_xml"]})
    rows["generation_plan_revisions"][0].update(settings_json={}, source_revision_id=None)
    rows["speech_plan_reviews"] = []
    rows["knowledge_ledgers"][0]["payload_json"]["characters"] = [{"id": "fred", "display_name": "Fred"}, {"id": "wilma", "display_name": "Wilma"}]
    rows["artifacts"][0].update(role="source", metadata_json={})
    owner = {"workspace_id": uid(10), "project_id": uid(1), "session_id": uid(2)}
    rows["artifact_edges"] = []
    for index in range(2):
        rows["artifacts"] += [dict(id=uid(20 + index * 2), kind="speech_voice_snapshot", role="speech_input", mime_type="application/json", size_bytes=100, content_hash=str(index + 1) * 64, state="current", metadata_json={}, **owner),
                              dict(id=uid(21 + index * 2), kind="audio", role="speech_reference_audio", mime_type="audio/wav", size_bytes=100, content_hash="a" * 64, state="current", metadata_json={}, **owner)]
        rows["artifact_edges"].append({"parent_artifact_id": uid(21 + index * 2), "child_artifact_id": uid(20 + index * 2), "relation": "speech_reference"})
        rows["artifact_edges"].append({"parent_artifact_id": uid(20 + index * 2), "child_artifact_id": uid(30), "relation": "derived_from"})
    snapshots = {uid(5): evidence()}
    rows["audio_takes"] = [{"id": uid(40), "generation_segment_id": uid(5), "generation_run_id": None,
        "parent_take_id": None, "artifact_id": uid(30), "kind": "tts", "status": "completed", "settings_hash": "opaque-take-settings",
        "duration_ms": 20, "is_active": True, "revision": 1}]
    if shared:
        other = {**deepcopy(segment), "id": uid(6), "ordinal": 1}
        other["speech_plan_json"]["speech_xml"] = other["speech_plan_json"]["speech_xml"].replace(uid(5), uid(6))
        rows["generation_segments"].append(other)
        snapshots[uid(6)] = remap_recorded_snapshot(evidence(), {uid(20): uid(20), uid(22): uid(22)}, segment_id=uid(6), revision_id=uid(4), text=TEXT)
        rows["audio_takes"].append({**deepcopy(rows["audio_takes"][0]), "id": uid(41), "generation_segment_id": uid(6)})
    metadata = {"kind": "tts", "source_text": TEXT, "synthesized_text": TEXT, "speaker": None,
        "llm_optimized": False, "llm_model": None, "generation_audio_identity": {"schema_version": 1,
        "settings_hash": "c" * 64, "voice_reference_hash": "d" * 64, "performance_request_hash": "e" * 64},
        PROVENANCE_KEY: {"version": 1, "segments": snapshots}}
    rows["artifacts"].append(dict(id=uid(30), kind="audio", role="generation_take", mime_type="audio/wav", size_bytes=100,
        content_hash="b" * 64, state="current", metadata_json=metadata, relative_path="artifacts/selected.wav", settings_hash=None, **owner))
    rows.update(freeze_speech_plan_rows(rows, {uid(4): {}}))
    return rows


def test_owned_audio_graph_supports_shared_artifact_and_two_entry_remap():
    rows = audio_graph(shared=True)
    before = deepcopy(rows)
    copy_audio.validate_audio_copy_graph(rows)
    validate_speech_plan_graph(rows)
    assert rows == before and all(row["status"] == "completed" for row in rows["generation_segments"])
    identities = {row["id"]: uid(int(UUID(row["id"])) + 100) for values in rows.values() for row in values if "id" in row}
    metadata = copy_audio.remap_audio_metadata(rows, identities)[uid(30)]
    assert set(metadata[PROVENANCE_KEY]["segments"]) == {uid(105), uid(106)}
    assert metadata["generation_audio_identity"] == rows["artifacts"][-1]["metadata_json"]["generation_audio_identity"]
    assert {row["generation_segment_id"] for row in copy_audio.remap_audio_rows(rows, identities)} == {uid(105), uid(106)}


@pytest.mark.parametrize("defect", ["duplicate", "run_id", "parent", "duration_bool", "kind", "status", "owner", "pin_owner", "pin_edge", "dependency", "orphan_entry", "identity", "metadata_extra", "removed"])
def test_audio_graph_rejects_forged_rows_and_unowned_dependencies(defect):
    rows = audio_graph()
    take, artifact = rows["audio_takes"][0], rows["artifacts"][-1]
    if defect == "duplicate":
        rows["audio_takes"].append({**take, "id": uid(99)})
    elif defect == "run_id":
        take["generation_run_id"] = uid(99)
    elif defect == "parent":
        take["parent_take_id"] = uid(99)
    elif defect == "duration_bool":
        take["duration_ms"] = True
    elif defect == "kind":
        take["kind"] = "rvc"
    elif defect == "status":
        take["status"] = "queued"
    elif defect == "owner":
        artifact["project_id"] = uid(99)
    elif defect == "pin_owner":
        rows["artifacts"][1]["session_id"] = uid(99)
    elif defect == "pin_edge":
        rows["artifact_edges"] = [edge for edge in rows["artifact_edges"] if edge["relation"] != "speech_reference"]
    elif defect == "dependency":
        rows["artifact_edges"] = [edge for edge in rows["artifact_edges"] if edge["relation"] != "derived_from"]
    elif defect == "orphan_entry":
        artifact["metadata_json"][PROVENANCE_KEY]["segments"][uid(99)] = evidence()
    elif defect == "identity":
        artifact["metadata_json"]["generation_audio_identity"]["settings_hash"] = "unknown"
    elif defect == "metadata_extra":
        artifact["metadata_json"]["api_key"] = ""
    else:
        rows["generation_segments"][0]["removed"] = True
    with pytest.raises((ValueError, TypeError, KeyError)):
        copy_audio.validate_audio_copy_graph(rows)


@pytest.mark.parametrize("version", range(1, 9))
def test_old_formats_keep_bytes_and_reject_audio_family_and_metadata(version):
    families = set(copy_publication._FIELDS) - SOUND_FAMILIES - {"performance_plans"} - {"audio_takes"}
    if version < 8:
        families -= SPEECH_FAMILIES
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
    snapshot = ProjectCopySnapshot(uid(1), uid(10), "Legacy audio", text, hashlib.sha256(text.encode()).hexdigest(), (), (), 0)
    rows, _ids, _all = copy_publication._decode(snapshot)
    assert rows["audio_takes"] == [] and snapshot.state_json == text
    for mutation in ("family", "metadata"):
        bad = deepcopy(state)
        if mutation == "family":
            bad["rows"]["audio_takes"] = []
        else:
            artifact = audio_graph()["artifacts"][-1]
            bad["rows"]["artifacts"] = [{key: artifact[key] for key in copy_publication._FIELDS["artifacts"].split()}]
        forged = canonical(bad)
        with pytest.raises(ProjectCopyError):
            copy_publication._decode(replace(snapshot, state_json=forged, fingerprint=hashlib.sha256(forged.encode()).hexdigest()))


@pytest.mark.parametrize("defect", ["orphan_source", "base_binding_extra", "base_source_extra", "unused_valid_base", "unused_root_pin"])
def test_unused_marker_objects_are_not_canonical_evidence(defect):
    value = evidence()
    resolution = deepcopy(next(iter(value["generation_control_snapshot"]["resolved_bindings"].values())))
    if defect == "orphan_source":
        value["tts"][SOURCE] = {"arbitrary": "hidden"}
    else:
        base = {key: item for key, item in resolution.items() if key in {"voice", "voice_id", PIN, BINDING, SELECTOR, SOURCE}}
        if defect == "base_binding_extra":
            base[BINDING]["arbitrary"] = "hidden"
        elif defect == "base_source_extra":
            base[SOURCE]["arbitrary"] = "hidden"
        if defect == "unused_root_pin":
            value["tts"].update(base)
        else:
            value["generation_control_snapshot"]["controls"]["_base_voice_settings"] = base
    with pytest.raises(ValueError):
        validate_recorded_snapshot(value, segment_id=uid(5), revision_id=uid(4), text=TEXT)


def test_dependency_only_metadata_uses_existing_sanitizer():
    rows = audio_graph()
    selected = rows["artifacts"][-1]
    selected["metadata_json"]["render_parts"] = [{"index": 0, "range": [0, len(TEXT)], "voice": uid(1),
        "source": "cast", "fallback": False, "speaker_ids": [uid(2)], "duration_ms": 20}]
    dependency = {**deepcopy(selected), "id": uid(31), "metadata_json": {"api_base": "https://private.invalid", "path": "/private/source", "ordinary": "kept"}}
    rows["artifacts"].append(dependency)
    identities = {uid(20): uid(120), uid(22): uid(122), uid(5): uid(105), uid(4): uid(104), uid(1): uid(101), uid(2): uid(102)}
    overrides = copy_audio.remap_audio_metadata(rows, identities)
    assert uid(31) not in overrides
    assert overrides[uid(30)]["render_parts"] == selected["metadata_json"]["render_parts"]
    sanitized = copy_publication._remap_json(dependency["metadata_json"], identities, {})
    assert sanitized == {"ordinary": "kept"}


def capture(p):
    with p.f.case.database.snapshot_session() as session:
        return copy_snapshot.capture_current_project(session, p.context, p.source["project_id"], p.users.workspace_id,
            settings_service=p.f.services.workspace_settings)


def observe_sql(database, operation):
    statements = []
    def observe(_c, _cu, statement, _p, _co, _e):
        statements.append(statement)
    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        result = operation()
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
    return result, statements


def fake_take(p, *, status="completed", kind="tts", metadata=None):
    from tests.test_voice_lifecycle_idempotency import silent_wav
    path = p.f.services.paths.uploads / (new_id() + ".wav")
    path.write_bytes(silent_wav())
    with execution_actor_scope(p.actor):
        artifact = p.f.services.artifacts.register(path, kind="audio", role="generation_take", session_id=p.source["id"],
            metadata=metadata or {"synthesized_text": "wrong current text"})
    with p.f.case.database.immediate_session() as session:
        segment = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"]).order_by(m.GenerationSegment.ordinal))
        assert segment is not None
        segment.status = "completed"
        take = m.AudioTake(generation_segment_id=segment.id, artifact_id=artifact.id, status=status, kind=kind,
            duration_ms=20, settings_hash="original-settings", is_active=True)
        session.add(take)
        session.flush()
        return take.id, artifact.id, segment.id


@pytest.mark.parametrize("defect", ["pending", "failed", "rvc", "stale", "missing_text", "no_executor"])
def test_unsupported_recordings_remain_counted_body_omitted(editorial, defect):
    p = editorial
    metadata = audio_graph()["artifacts"][-1]["metadata_json"]
    metadata = {key: item for key, item in metadata.items() if key != PROVENANCE_KEY}
    if defect == "missing_text":
        metadata.pop("synthesized_text")
    elif defect == "stale":
        metadata["synthesized_text"] = "Earlier audible text."
    else:
        metadata["synthesized_text"] = "Editable current content."
    fake_take(p, status="queued" if defect == "pending" else "failed" if defect == "failed" else "completed",
        kind="rvc" if defect == "rvc" else "tts", metadata=metadata)
    snapshot = capture(p)
    state = json.loads(snapshot.state_json)
    assert not snapshot.ready
    assert state["unsupported_counts"]["audio_takes"] == 1
    assert state["unsupported_counts"]["generation_plans"] == 1
    assert state["rows"]["audio_takes"] == []
    assert all(state["rows"][family] == [] for family in SPEECH_FAMILIES)
    assert "Editable current content." not in snapshot.state_json
    assert not any(row["role"] == "generation_take" for row in state["rows"]["artifacts"])


@pytest.mark.parametrize("defect", ["duplicate", "foreign", "dangling", "cycle", "budget"])
def test_scalar_take_ancestry_fails_before_protected_run_bodies(editorial, monkeypatch, defect):
    p = editorial
    take_id, artifact_id, segment_id = fake_take(p)
    other = body(p.users.alice.post("/api/v1/sessions", json={"name": "Other finite owner", "workflow_kind": "voiceover"}, headers=headers(p.users.ah, "audio-other-scope")), 201)
    with execution_actor_scope(p.actor):
        other_plan = p.f.services.generation.create_plan(other["id"], source_revision_id=None, settings={}, segments=[{"text": "Other text."}])
    with p.f.case.database.immediate_session() as session:
        root = session.get_one(m.AudioTake, take_id)
        if defect == "duplicate":
            session.add(m.AudioTake(generation_segment_id=segment_id, artifact_id=artifact_id, status="completed", duration_ms=20, is_active=True))
        elif defect == "cycle":
            root.parent_take_id = take_id
        elif defect in {"foreign", "dangling"}:
            run = m.GenerationRun(session_id=other["id"], plan_revision_id=other_plan["active_revision_id"], settings_snapshot_json={"private": "never hydrate"})
            session.add(run)
            session.flush()
            root.generation_run_id = run.id
            if defect == "dangling":
                # A missing executor locator is persisted artifact metadata, not a broken FK.
                root.generation_run_id = None
                session.get_one(m.Artifact, artifact_id).metadata_json = {"generation_task_run_id": new_id()}
    if defect == "budget":
        monkeypatch.setattr(copy_snapshot, "MAX_EDITORIAL_ROWS", 3)
    statements = []
    def observe(_c, _cu, sql, _p, _co, _e):
        statements.append(sql)
    event.listen(p.f.case.database.engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(ProjectCopyError):
            capture(p)
    finally:
        event.remove(p.f.case.database.engine, "before_cursor_execute", observe)
    assert not any("generation_runs.settings_snapshot_json" in sql for sql in statements)
    if defect in {"duplicate", "budget"}:
        assert not any("generation_plan_revisions.settings_json" in sql for sql in statements)


def test_no_plan_and_no_audio_skip_lineage_bodies_and_writes(editorial, monkeypatch, tmp_path):
    p = editorial
    def forbidden(*_args, **_kwargs):
        pytest.fail("Ordinary no-audio capture reached selected-audio evidence")
    monkeypatch.setattr(copy_snapshot, "capture_audio_evidence", forbidden)
    snapshot, statements = observe_sql(p.f.case.database, lambda: capture(p))
    assert snapshot.ready and json.loads(snapshot.state_json)["rows"]["audio_takes"] == []
    assert len([sql for sql in statements if "FROM audio_takes" in sql]) == 1
    (tmp_path / "no-audio-sql.json").write_text(json.dumps(statements, indent=2))
    run_reads = [sql for sql in statements if "\nFROM generation_runs" in sql]
    assert len(run_reads) == 1
    assert all(sql.split("\nFROM generation_runs", 1)[0].strip() == "SELECT generation_runs.id"
               and "generation_runs.status = ?" in sql for sql in run_reads)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    source = body(p.users.alice.post("/api/v1/sessions", json={"name": "No editorial plan", "workflow_kind": "voiceover"}, headers=headers(p.users.ah, "audio-no-plan")), 201)
    p.source = source
    no_plan, no_plan_sql = observe_sql(p.f.case.database, lambda: capture(p))
    assert no_plan.ready
    assert not any("FROM audio_takes" in sql for sql in no_plan_sql)
    no_plan_run_reads = [sql for sql in no_plan_sql if "\nFROM generation_runs" in sql]
    assert len(no_plan_run_reads) == 1
    assert all(sql.split("\nFROM generation_runs", 1)[0].strip() == "SELECT generation_runs.id"
               and "generation_runs.status = ?" in sql for sql in no_plan_run_reads)
    (tmp_path / "no-audio-query-receipt.json").write_text(json.dumps({"no_audio_sql": len(statements), "no_plan_sql": len(no_plan_sql), "audio_header_selects": 1, "lineage_selects": 0, "existing_running_id_guard":len(run_reads), "writes": 0}, indent=2))


def test_copied_recast_batches_owned_segments_and_normal_path_has_zero_extra_queries(editorial, monkeypatch, tmp_path):
    p = editorial
    count = 503
    with p.f.case.database.immediate_session() as session:
        original = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"]))
        assert original is not None
        session.delete(original)
        recorded = {}
        artifact = m.Artifact(workspace_id=session.get_one(m.Project, p.source["project_id"]).workspace_id, project_id=p.source["project_id"], session_id=p.source["id"],
            kind="audio", role="generation_take", state="current", relative_path="artifacts/bounded-not-read.wav", mime_type="audio/wav", size_bytes=100, metadata_json={})
        session.add(artifact)
        session.flush()
        entries = {}
        for index in range(count):
            segment = m.GenerationSegment(plan_revision_id=p.plan["active_revision_id"], ordinal=index, text=TEXT, status="completed")
            session.add(segment)
            session.flush()
            take = m.AudioTake(generation_segment_id=segment.id, artifact_id=artifact.id, status="completed", duration_ms=20, is_active=True)
            session.add(take)
            session.flush()
            recorded[segment.id] = {"id": take.id}
            entries[segment.id] = remap_recorded_snapshot(evidence(), {uid(20):uid(20), uid(22):uid(22)}, segment_id=segment.id, revision_id=p.plan["active_revision_id"], text=TEXT)
        artifact.metadata_json = {PROVENANCE_KEY: {"version":1, "segments":entries}}
        artifact_id = artifact.id
    def read():
        with p.f.case.database.snapshot_session() as session:
            return _recorded_snapshots(session, p.source["id"], recorded)
    from pandrator.web import recorded_speech_contracts
    wrapper_calls = []
    original_validator = recorded_speech_contracts.validate_speech_provenance
    def observe_wrapper(value):
        wrapper_calls.append(len(value["segments"]))
        return original_validator(value)
    monkeypatch.setattr(recorded_speech_contracts, "validate_speech_provenance", observe_wrapper)
    result, statements = observe_sql(p.f.case.database, read)
    assert wrapper_calls == [count]
    assert len(result) == count
    assert len([sql for sql in statements if "FROM projects JOIN sessions" in sql]) == 1
    assert len([sql for sql in statements if "FROM generation_segments" in sql]) == 2
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 6  # two take chunks, one artifact, one owner, two segment chunks
    with p.f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, artifact_id).metadata_json = {}
    normal, normal_sql = observe_sql(p.f.case.database, read)
    assert normal == {}
    normal_reads = [sql for sql in normal_sql if sql.lstrip().upper().startswith("SELECT")]
    assert len(normal_reads) == 3
    assert not any("FROM projects" in sql or "FROM generation_segments" in sql for sql in normal_sql)
    assert wrapper_calls == [count]
    (tmp_path / "copied-recast-query-receipt.json").write_text(json.dumps({
        "entries":count, "copied_selects":len(reads), "normal_selects":len(normal_reads),
        "wrapper_validations":len(wrapper_calls), "owner_selects":1, "segment_batches":2,
    }, indent=2))


def route_config(client, auth, *, workspace=None, revision=0, endpoint="https://speech.invalid"):
    return body(put(client, auth, "services.tts", {"provider_configs": [{"id":"audio_cpp", "enabled":True,
        "models":[MODEL], "default_model":MODEL, "api_base":endpoint, "connection_mode":"external",
        "api_key":"synthetic-audio-copy-only", "credential_backend":"database"}]}, revision=revision, workspace=workspace))


def worker_for(f):
    handlers = f.case.app.extensions["pandrator"]["workflow_handlers"]
    return Worker(f.services.jobs, "selected-current-audio-copy", handlers.handlers())


def generate(f, client, auth, sid, worker, key, *, operation="generate"):
    queued = body(client.post(f"/api/v1/sessions/{sid}/generation-admissions", json={"operation":operation}, headers=headers(auth, key)), 202)
    assert worker.run_once()
    admitted = f.services.jobs.get(queued["id"])
    assert admitted.status == "succeeded", admitted.error_message
    assert worker.run_once()
    generated = f.services.jobs.get(admitted.result_json["job_id"])
    assert generated.status == "succeeded", generated.error_message
    return admitted.result_json["generation_run_id"]


def registered_copy(p, worker, key, *, source=None, client=None, auth=None):
    from tests.project_copy_test_support import (
        capture_with_connections,
        snapshot_destination_connections,
    )
    source = source or p.source
    client, auth = client or p.users.alice, auth or p.users.ah
    with p.f.case.database.snapshot_session() as session:
        snapshot = capture_with_connections(p.f.case, session, _context(p.f, client), source["project_id"], p.users.workspace_id,
            settings_service=p.f.services.workspace_settings)
    assert snapshot.ready, snapshot.blockers
    queued = body(client.post(f"/api/v1/projects/{source['project_id']}/copy", json={
        "destination_workspace_id":p.users.workspace_id, "expected_fingerprint":snapshot.fingerprint,
        "destination_connections":snapshot_destination_connections(snapshot)}, headers=headers(auth, key)), 202)
    assert worker.run_once()
    finished = body(client.get(f"/api/v1/project-copies/{queued['id']}"))
    assert finished["state"] == "succeeded", finished
    return snapshot, finished["result"], queued["id"]


def active_rows(f, sid):
    with f.case.database.snapshot_session() as session:
        plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == sid))
        assert plan is not None
        segments = list(session.scalars(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == plan.active_revision_id).order_by(m.GenerationSegment.ordinal)))
        result = []
        for segment in segments:
            take = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_segment_id == segment.id, m.AudioTake.is_active.is_(True)))
            artifact = session.get(m.Artifact, take.artifact_id) if take else None
            result.append(SimpleNamespace(segment=segment, take=take, artifact=artifact,
                data=(f.services.paths.root / artifact.relative_path).read_bytes() if artifact else None))
        return result


def test_real_mixed_selected_copy_member_assembly_and_partial_recast(retained_editorial, monkeypatch, tmp_path):
    from tests.project_copy_test_support import capture_with_connections
    p, f, users = retained_editorial, retained_editorial.f, retained_editorial.users
    route_config(users.alice, users.ah)
    with f.case.database.immediate_session() as session:
        blocks = list(session.scalars(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"]).order_by(m.GenerationSegment.ordinal)))
        second = blocks[1]
        second.removed, second.text, second.optimized_text = False, TEXT, TEXT
        second.speech_plan_json = {"speech_xml": source_snapshot(segment_id=second.id)["generation_control_snapshot"]["segments"][second.id]["speech_xml"]}
        second.optimization_status, second.optimization_reviewed = "completed", True
        session.delete(session.get_one(m.SpeechPlanReview, p.plan["active_revision_id"]))
        from pandrator.web.speech_plan_context import plan_signature
        session.flush()
        session.add(m.SpeechPlanReview(revision_id=p.plan["active_revision_id"], content_hash=plan_signature(session, p.plan["active_revision_id"])))
    calls = native_transport(monkeypatch)
    worker = worker_for(f)
    first_run = generate(f, users.alice, users.ah, p.source["id"], worker, "audio-first-generation")
    earlier = active_rows(f, p.source["id"])
    second_run = generate(f, users.alice, users.ah, p.source["id"], worker, "audio-second-generation", operation="regenerate")
    assert first_run != second_run and len(calls) == 8
    newer = active_rows(f, p.source["id"])
    with f.case.database.snapshot_session() as session:
        stale = capture_with_connections(f.case, session, p.context, p.source["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
    assert stale.ready, stale.blockers
    body(users.alice.post(f"/api/v1/generation-segments/{newer[0].segment.id}/takes/{earlier[0].take.id}/select", json={},
        headers={**headers(users.ah, "audio-select-older"), "If-Match":str(newer[0].segment.revision)}))
    rejected = users.alice.post(f"/api/v1/projects/{p.source['project_id']}/copy", json={"destination_workspace_id":users.workspace_id,
        "expected_fingerprint":stale.fingerprint}, headers=headers(users.ah, "audio-stale-copy"))
    assert rejected.status_code == 409, rejected.get_json()
    mixed = active_rows(f, p.source["id"])
    assert [row.take.id for row in mixed] == [earlier[0].take.id, newer[1].take.id]
    source_rows = {}
    with f.case.database.snapshot_session() as session:
        for model in (m.AudioTake, m.GenerationRun, m.GenerationSegment, m.ProjectSpeechReference):
            source_rows[model] = [dict(row) for row in session.execute(select(model.__table__)).mappings()]
        external_before = {model: session.scalar(select(func.count()).select_from(model)) for model in (m.Voice, m.StoredCredential, m.ProviderModel)}
    source_files = {row.artifact.relative_path:hashlib.sha256(row.data).hexdigest() for row in mixed}
    def no_library(*_a, **_k):
        pytest.fail("Copied retained recording consulted the source voice library")
    from pandrator.web import project_speech_reference_capture, speech_inputs
    capture_reference, capture_input = project_speech_reference_capture.capture_speech_input, speech_inputs.capture_speech_input
    monkeypatch.setattr(project_speech_reference_capture, "capture_speech_input", no_library)
    monkeypatch.setattr(speech_inputs, "capture_speech_input", no_library)
    snapshot, result, copy_id = registered_copy(p, worker, "audio-mixed-copy")
    state = json.loads(snapshot.state_json)
    assert {row["id"] for row in state["rows"]["audio_takes"]} == {row.take.id for row in mixed}
    target = result["source_session_id"]
    copied = active_rows(f, target)
    assert len(copied) == 2
    for original, row in zip(mixed, copied, strict=True):
        assert row.segment.status == "completed" and row.segment.revision == 1
        assert row.take.generation_run_id is None and row.take.parent_take_id is None and row.take.revision == 1
        assert row.take.duration_ms == original.take.duration_ms and row.take.settings_hash == original.take.settings_hash
        assert row.data == original.data
        before, after = original.artifact.metadata_json, row.artifact.metadata_json
        for key in ("generation_audio_identity", "render_parts", "speech_timing", "audio_verification", "source_text", "synthesized_text"):
            assert after.get(key) == before.get(key)
        assert not {"generation_segment_id", "generation_run_id", "generation_task_run_id"} & after.keys()
        frozen = after[PROVENANCE_KEY]["segments"][row.segment.id]["generation_control_snapshot"]
        assert frozen["plan_revision_id"] == row.segment.plan_revision_id
    with f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.GenerationRun).where(m.GenerationRun.session_id == target)) == 0
        assert session.scalar(select(func.count()).select_from(m.AudioTake).where(m.AudioTake.generation_segment_id.in_([row.segment.id for row in copied]))) == 2
        for model, expected in external_before.items():
            assert session.scalar(select(func.count()).select_from(model)) == expected
        for row in copied:
            pins = recorded_input_pins(row.artifact.metadata_json[PROVENANCE_KEY]["segments"][row.segment.id])
            assert len(pins) == 2
            for pin in pins:
                owned = session.get_one(m.Artifact, pin["artifact_id"])
                assert (owned.workspace_id, owned.project_id, owned.session_id) == (users.workspace_id, result["project_id"], target)
                assert session.get(m.ArtifactEdge, (owned.id, row.artifact.id)) is not None
    # Recomputed fingerprints cannot turn forged operational history into a copy.
    forged = deepcopy(state)
    forged["rows"]["audio_takes"][0]["generation_run_id"] = first_run
    encoded = canonical(forged)
    with f.case.database.immediate_session() as session:
        before_count = session.scalar(select(func.count()).select_from(m.Project))
        with pytest.raises(ProjectCopyError):
            copy_publication.publish_project_copy_rows(session, replace(snapshot, state_json=encoded, fingerprint=hashlib.sha256(encoded.encode()).hexdigest()),
                cast(Any, object()), copy_id=new_id(), created_by_user_id=users.alice_id)
        assert session.scalar(select(func.count()).select_from(m.Project)) == before_count
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(f"/api/v1/sessions/{p.source['id']}").status_code == 404
    for voice in p.voices:
        assert users.bob.get(f"/api/v1/voices/{voice['id']}").status_code == 404
    for original, row in zip(mixed, copied, strict=True):
        assert users.bob.get(f"/api/v1/artifacts/{original.artifact.id}/content").status_code == 404
        played = users.bob.get(f"/api/v1/artifacts/{row.artifact.id}/content")
        assert played.status_code == 200 and played.data == original.data
    current = body(users.bob.get(f"/api/v1/sessions/{target}/generation-segments"))["items"]
    assert all(row["audio_reuse_reason"] == "reusable" for row in current), current
    body(users.bob.patch(f"/api/v1/sessions/{target}/generation-segments", json={"updates":[{"id":current[0]["id"], "revision":current[0]["revision"],
        "changes":{"marked":not current[0]["marked"]}}]}, headers=headers(users.bh, "audio-copied-mark")))
    assembly = body(users.bob.post(f"/api/v1/sessions/{target}/output-assemblies", json={}, headers=headers(users.bh, "audio-no-run-assembly")), 202)
    assert worker.run_once()
    job = f.services.jobs.get(assembly["job_id"])
    assert job.status == "succeeded", job.error_message
    with f.case.database.snapshot_session() as session:
        output = session.get_one(m.OutputAssembly, assembly["id"])
        assert output.generation_run_id is None and output.status == "completed"
        artifact = session.get_one(m.Artifact, output.artifact_id)
        assert (f.services.paths.root / artifact.relative_path).read_bytes().startswith(b"RIFF")
    # A saved assembly becomes a download, with fresh runtime assembly kept local.
    with f.case.database.snapshot_session() as session:
        archived = copy_snapshot.capture_current_project(session, _context(f, users.bob), result["project_id"], users.workspace_id, settings_service=f.services.workspace_settings)
    assert archived.ready, archived.blockers
    archived_rows = json.loads(archived.state_json)["rows"]
    saved_outputs = [row for row in archived_rows["artifacts"] if row["role"] == "export_saved_speech"]
    assert len(saved_outputs) == 1 and saved_outputs[0]["content_hash"] == artifact.content_hash
    assert saved_outputs[0]["metadata_json"]["copied_output"] == {"version":1, "kind":"speech_assembly"}
    assert "output_assemblies" not in archived_rows
    assert not any(edge["child_artifact_id"] == saved_outputs[0]["id"] for edge in archived_rows["artifact_edges"])
    # Allow only capture of a NEW accessible Team voice before fencing the library again.
    monkeypatch.setattr(project_speech_reference_capture, "capture_speech_input", capture_reference)
    monkeypatch.setattr(speech_inputs, "capture_speech_input", capture_input)
    calls = native_transport(monkeypatch)
    team_voice = body(create(users.bob, users.bh, "New Team Fred", wid=users.workspace_id), 201)
    sid, _aid, _path = sample(f.case, team_voice)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, team_voice["id"]).metadata_json = {"providers":{"audio_cpp":{"status":"ready", "resource_kind":"linked_reference", "voice_id":"team-fred-reference"}}}
        sample_row = session.get_one(m.VoiceSample, sid)
        sample_row.transcript, sample_row.transcript_reviewed = TRANSCRIPT + " New Fred.", True
    controls = body(users.bob.get(f"/api/v1/sessions/{target}/generation-controls"))
    changed_cast = deepcopy(controls["cast"])
    changed_cast["characters"]["fred"] = {"voice_id":team_voice["id"]}
    body(users.bob.put(f"/api/v1/sessions/{target}/generation-controls", json={"expected_revision":controls["revision"],
        "characters":controls["characters"], "cast":changed_cast}, headers=headers(users.bh, "audio-recast-new-fred")))
    handlers = f.case.app.extensions["pandrator"]["workflow_handlers"]
    for path in ("pandrator.web.project_speech_reference_capture.capture_speech_input", "pandrator.web.speech_inputs.capture_speech_input", "pandrator.web.generation_audio_identity.voice_reference_index"):
        monkeypatch.setattr(path, no_library)
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", no_library)
    preview = body(users.bob.post(f"/api/v1/sessions/{target}/generation-recast/preview", json={"selector_keys":["character:fred"]}, headers=headers(users.bh, "audio-recast-preview")))
    assert preview["blocked_reason"] is None, preview
    recast = body(users.bob.post(f"/api/v1/sessions/{target}/generation-recast", json={"selector_keys":preview["selected_keys"], "expected_selection_hash":preview["selection_hash"]}, headers=headers(users.bh, "audio-recast-start")), 201)
    assert worker.run_once()
    recast_job = f.services.jobs.get(recast["job_id"])
    assert recast_job.status == "succeeded", recast_job.error_message
    assert len(calls) == 4
    assert sum("Character 1." in canonical(call) for call in calls) == 2
    assert sum("New Fred." in canonical(call) for call in calls) == 2
    before_calls = len(calls)
    route_config(users.owner, users.oh, workspace=users.workspace_id, revision=2, endpoint="https://speech-route-two.invalid")
    after_route = body(users.bob.get(f"/api/v1/sessions/{target}/generation-segments"))["items"]
    assert all(row["audio_reuse_reason"] == "generation_settings_changed" for row in after_route), after_route
    assert len(calls) == before_calls
    with f.case.database.snapshot_session() as session:
        for original in copied:
            assert session.get_one(m.Artifact, original.artifact.id).metadata_json == original.artifact.metadata_json
        for model, expected in source_rows.items():
            columns = [column.name for column in model.__table__.primary_key]
            identities = {tuple(row[column] for column in columns) for row in expected}
            actual = [dict(row) for row in session.execute(select(model.__table__)).mappings()
                if tuple(row[column] for column in columns) in identities]
            assert sorted(actual, key=lambda row:tuple(row[column] for column in columns)) == sorted(
                expected, key=lambda row:tuple(row[column] for column in columns))
    for relative, digest in source_files.items():
        assert hashlib.sha256((f.services.paths.root / relative).read_bytes()).hexdigest() == digest
    (tmp_path / "mixed-selected-audio-proof.json").write_text(json.dumps({"source_runs":2, "copied_takes":2, "copied_runs":0,
        "mixed_take_ids":[row.take.id for row in mixed], "copy_id":copy_id, "member_assembly":True, "recast_requests":len(calls)}, indent=2))


def test_real_plain_retained_narrator_copy_and_recopy(design, monkeypatch, tmp_path):
    f, users = design, design.users
    route_config(users.owner, users.oh, workspace=users.workspace_id, revision=1)
    route_config(users.alice, users.ah)
    source = body(users.alice.post("/api/v1/sessions", json={"name":"Plain retained recording", "workflow_kind":"voiceover", "source_language":"en"}, headers=headers(users.ah, "plain-audio-session")), 201)
    voice = body(create(users.alice, users.ah, "Private plain narrator"), 201)
    sample_id, _artifact_id, path = sample(f.case, voice)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {"providers":{"audio_cpp":{"status":"ready", "resource_kind":"linked_reference", "voice_id":"plain-retained-reference"}}}
        item = session.get_one(m.VoiceSample, sample_id)
        item.transcript, item.transcript_reviewed = TRANSCRIPT, True
    body(users.alice.put(f"/api/v1/sessions/{source['id']}/settings/tts", json={"value":{"service":"audio_cpp", "model":MODEL,
        "voice":"plain-retained-reference", "voice_id":voice["id"], "language":"en", "casting_enabled":False,
        "performance_enabled":False, "voice_mode_version":1, "tts_concurrent_requests":1}}, headers={**headers(users.ah, "plain-audio-tts"), "If-Match":'"0"'}))
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        f.services.generation.create_plan(source["id"], source_revision_id=None, settings={}, segments=[{"text":TEXT, "language":"en"}])
    deleted = users.alice.delete(f"/api/v1/voices/{voice['id']}/samples/{sample_id}", headers={**users.ah, "If-Match":'"1"'})
    assert deleted.status_code == 200 and not path.exists()
    calls = native_transport(monkeypatch)
    worker = worker_for(f)
    run_id = generate(f, users.alice, users.ah, source["id"], worker, "plain-audio-generation")
    original = active_rows(f, source["id"])[0]
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, run_id).settings_snapshot_json.get("generation_control_snapshot") is None
    p = SimpleNamespace(f=f, users=users, source=source)
    def no_library(*_a, **_k):
        pytest.fail("Plain retained copy consulted original library")
    for target in ("pandrator.web.project_speech_reference_capture.capture_speech_input", "pandrator.web.speech_inputs.capture_speech_input"):
        monkeypatch.setattr(target, no_library)
    _snapshot, first, _copy_id = registered_copy(p, worker, "plain-first-copy")
    first_row = active_rows(f, first["source_session_id"])[0]
    first_entry = first_row.artifact.metadata_json[PROVENANCE_KEY]["segments"][first_row.segment.id]
    assert first_entry["generation_control_snapshot"] is None
    assert len(recorded_input_pins(first_entry)) == 1
    _second_snapshot, second, _second_id = registered_copy(p, worker, "plain-second-copy", source={"project_id":first["project_id"]})
    second_row = active_rows(f, second["source_session_id"])[0]
    second_entry = second_row.artifact.metadata_json[PROVENANCE_KEY]["segments"][second_row.segment.id]
    assert second_entry["generation_control_snapshot"] is None
    assert first_entry["tts"][PIN]["artifact_id"] != second_entry["tts"][PIN]["artifact_id"]
    assert first_entry["tts"][PIN]["content_hash"] == second_entry["tts"][PIN]["content_hash"]
    for row in (first_row, second_row):
        assert row.data == original.data and row.take.duration_ms == original.take.duration_ms
        assert row.take.generation_run_id is None and row.take.parent_take_id is None
        assert row.artifact.metadata_json["generation_audio_identity"] == original.artifact.metadata_json["generation_audio_identity"]
        with f.case.database.snapshot_session() as session:
            assert session.scalar(select(func.count()).select_from(m.GenerationRun).where(m.GenerationRun.session_id == row.artifact.session_id)) == 0
            assert _recorded_snapshots(session, row.artifact.session_id, {row.segment.id:{"id":row.take.id}}) == {}
            pin = recorded_input_pins(row.artifact.metadata_json[PROVENANCE_KEY]["segments"][row.segment.id])[0]
            assert session.get(m.ArtifactEdge, (pin["artifact_id"], row.artifact.id)) is not None
    assert len(calls) == 1
    (tmp_path / "plain-recording-copy-proof.json").write_text(json.dumps({"generations":1, "copies":2, "invented_runs":0, "cast_snapshot":None}, indent=2))


@pytest.mark.parametrize("container", [[], None, "invalid"])
def test_source_projection_wrong_segments_container_is_value_error(container):
    source = source_snapshot()
    source["generation_control_snapshot"]["segments"] = container
    with pytest.raises(ValueError):
        project_recorded_snapshot(source, entry_id=uid(5), segment_id=uid(5), revision_id=uid(4), text=TEXT)


def test_producer_recast_all_cast_omits_unused_base_before_validation():
    from pandrator.web.generation_recast import _BASE_KEYS
    source = source_snapshot()
    base: dict[str, Any] = {key: None for key in _BASE_KEYS}
    base.update(voice="", speaker="", voice_description="", voice_name="")
    source["generation_control_snapshot"]["controls"]["_base_voice_settings"] = base
    before = deepcopy(source)
    projected = project_recorded_snapshot(source, entry_id=uid(5), segment_id=uid(5), revision_id=uid(4), text=TEXT)
    assert projected == evidence() and source == before
    assert "_base_voice_settings" not in projected["generation_control_snapshot"]["controls"]
    # The exact producer vector is input compatibility, not an expanded canonical schema.
    projected["generation_control_snapshot"]["controls"]["_base_voice_settings"] = base
    with pytest.raises(ValueError):
        validate_recorded_snapshot(projected, segment_id=uid(5), revision_id=uid(4), text=TEXT)


def fallback_source():
    from pandrator.web.generation_recast import _BASE_KEYS
    source = source_snapshot()
    frozen = source["generation_control_snapshot"]
    frozen["segments"][uid(5)]["speech_xml"] = None
    frozen["controls"]["cast"] = {}
    frozen["resolved_bindings"] = {}
    selector = speech_reference_selector({"voice":"", "voice_id":uid(1), "service":"audio_cpp", "model":MODEL})
    base: dict[str, Any] = {key:None for key in _BASE_KEYS}
    base.update(voice="narrator-reference", speaker="narrator-reference", voice_id=uid(1), voice_name="",
        **{PIN:{"version":1, "artifact_id":uid(20), "content_hash":"1"*64},
           BINDING:{"adapter":"audio_cpp", "model":MODEL, "voice":"narrator-reference", "display_name":"Narrator"},
           SELECTOR:speech_reference_selector_hash(selector)})
    frozen["controls"]["_base_voice_settings"] = base
    return source


def test_producer_recast_audible_base_preserves_only_closed_retained_selection():
    source = fallback_source()
    projected = project_recorded_snapshot(source, entry_id=uid(5), segment_id=uid(5), revision_id=uid(4), text=TEXT)
    base = projected["generation_control_snapshot"]["controls"]["_base_voice_settings"]
    assert base[PIN] == source["generation_control_snapshot"]["controls"]["_base_voice_settings"][PIN]
    assert "voice_name" not in base and "voice_description" not in base and SOURCE not in base
    assert projected["generation_control_snapshot"]["controls"]["characters"] == []
    assert recorded_input_pins(projected) == [base[PIN]]
    assert validate_recorded_snapshot(projected, segment_id=uid(5), revision_id=uid(4), text=TEXT) == projected


@pytest.mark.parametrize("field", ["voice_description", "voice_name", "elevenlabs_voice_id", "audio_cpp_voice_ref", "audio_cpp_voice_ref_hash", "audio_cpp_reference_text", "api_base"])
def test_producer_recast_audible_base_blocks_unadapted_nonempty_voice_controls(field):
    source = fallback_source()
    source["generation_control_snapshot"]["controls"]["_base_voice_settings"][field] = "unsupported nonempty source control"
    with pytest.raises(ValueError):
        project_recorded_snapshot(source, entry_id=uid(5), segment_id=uid(5), revision_id=uid(4), text=TEXT)


def signal_metadata():
    from pydub import AudioSegment

    from pandrator.web.audio_verification import verify_audio
    metadata = deepcopy(audio_graph()["artifacts"][-1]["metadata_json"])
    metadata["audio_verification"] = verify_audio(AudioSegment.silent(duration=20, frame_rate=24000), TEXT,
        {"audio_verification_mode":"signal"})
    return metadata


def test_signal_verification_producer_duration_range_is_preserved():
    metadata = signal_metadata()
    bounds = metadata["audio_verification"]["metrics"]["expected_duration_range_ms"]
    assert len(bounds) == 2 and all(type(value) is int for value in bounds)
    assert copy_audio.audio_metadata(metadata, TEXT) == metadata


@pytest.mark.parametrize("bounds", [[True, 200], [200, 100], [100], [100, 200, 300], [100, float("inf")], [100.0, 200], [-1, 200], {"min":100, "max":200}])
def test_signal_verification_rejects_malformed_duration_bounds(bounds):
    metadata = signal_metadata()
    metadata["audio_verification"]["metrics"]["expected_duration_range_ms"] = bounds
    with pytest.raises(ValueError):
        copy_audio.audio_metadata(metadata, TEXT)
