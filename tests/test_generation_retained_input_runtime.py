"""Offline retained generation material and caller-proof ordering tests."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

import pytest
from sqlalchemy import delete, event

from pandrator.web import generation_speech_inputs as runtime_module
from pandrator.web import speech_inputs
from pandrator.web.generation_speech_inputs import (
    GenerationSpeechInputs,
    retained_generation_input,
)
from pandrator.web.models import Artifact, SessionRecord, Voice, VoiceSample
from pandrator.web.projects.identity import create_session_project
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PreparedSpeechInput,
    SpeechInputError,
    SpeechInputOwner,
)
from tests.test_retained_speech_inputs import Harness
from tests.test_retained_speech_inputs import harness as harness


def forbidden(*_args, **_kwargs):
    raise AssertionError("Unexpected legacy, source, graph or file operation")


@dataclass
class RetainedGraph:
    h: Harness
    pin: dict[str, Any]
    binding: dict[str, str]
    prepared: PreparedSpeechInput

    def settings(self, **updates) -> dict[str, Any]:
        result = deepcopy(self.h.settings)
        result[PROJECT_SPEECH_INPUT_KEY] = deepcopy(self.pin)
        result[PROJECT_SPEECH_BINDING_KEY] = deepcopy(self.binding)
        result.update(updates)
        return result


@pytest.fixture
def graph(harness: Harness) -> RetainedGraph:
    h = harness
    with h.database.immediate_session() as session:
        record = SessionRecord(name="Generation retained input", workflow_kind="audiobook")
        project = create_session_project(session, record, workspace_id=h.owner.workspace_id)
        h.owner = SpeechInputOwner(h.owner.workspace_id, project.id, record.id)
    captured = h.capture()
    prepared = h.prepare(captured)
    pin = h.publish(captured, prepared)
    assert captured.envelope_json is not None
    envelope = json.loads(captured.envelope_json)
    binding = {name: envelope[name] for name in ("adapter", "model", "voice", "display_name")}
    return RetainedGraph(h, pin, binding, prepared)


def current_runtime(graph: RetainedGraph, events: list[str] | None = None):
    h = graph.h

    def proof():
        with h.database.snapshot_session() as session:
            if events is not None:
                events.append("current-proof")
            runtime.check(session)

    runtime = GenerationSpeechInputs(h.database, h.paths, h.owner, proof)
    return runtime


def test_metadata_is_pure_strict_and_defensively_copied(graph: RetainedGraph, monkeypatch):
    settings = graph.settings(provider_configs=[{"api_key": "private-test-value"}])
    monkeypatch.setattr(runtime_module, "load_speech_input", forbidden)
    monkeypatch.setattr(runtime_module, "check_speech_input", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    metadata = retained_generation_input(settings)
    assert metadata is not None and metadata.pin == graph.pin and metadata.binding == graph.binding
    pin_copy = metadata.pin
    assert pin_copy is not None
    pin_copy["content_hash"] = "0" * 64
    metadata.binding["voice"] = "Changed"
    assert metadata.pin == graph.pin and metadata.binding == graph.binding
    assert "private-test-value" not in repr(metadata)
    assert "provider_configs" not in repr(metadata)
    assert retained_generation_input(graph.h.settings) is None
    speaker_settings = graph.settings(voice="", speaker=" chosen ")
    assert retained_generation_input(speaker_settings) == metadata
    custom_settings = graph.settings(
        service="Custom", preview_service_id="selected-profile", preview_adapter="audio_cpp"
    )
    assert retained_generation_input(custom_settings) == metadata


def test_materializes_actual_retained_graph_after_origin_deletion_without_origin_queries(
    graph: RetainedGraph, monkeypatch
):
    h = graph.h
    with h.database.immediate_session() as session:
        session.execute(delete(VoiceSample).where(VoiceSample.voice_id == h.voice_id))
        session.execute(delete(Voice).where(Voice.id == h.voice_id))
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    events: list[str] = []
    runtime = current_runtime(graph, events)
    original_load = runtime_module.load_speech_input

    def load(*args, **kwargs):
        assert events and events[-1] == "current-proof"
        return original_load(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "load_speech_input", load)
    result = runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    assert result["audio_cpp_voice_ref"]["data"].startswith("data:audio/wav;base64,")
    assert result["audio_cpp_voice_ref_hash"] == graph.prepared.audio.content_hash
    assert result["audio_cpp_reference_text"] == "Reviewed source."
    assert not any("FROM voices" in q or "FROM voice_samples" in q for q in statements)


def test_repeat_prepares_cache_only_material_and_return_independent_settings(graph, monkeypatch):
    reads: list[str] = []
    original_read = speech_inputs.read_verified_file

    def read(paths, relative, **kwargs):
        reads.append(relative)
        return original_read(paths, relative, **kwargs)

    monkeypatch.setattr(speech_inputs, "read_verified_file", read)
    runtime = current_runtime(graph)
    first_settings = graph.settings(provider_configs=[{"api_key": "first"}], speed=1)
    first = runtime.prepare(first_settings, legacy_prepare=forbidden)
    assert reads == [graph.prepared.snapshot.relative_path, graph.prepared.audio.relative_path]
    verified = deepcopy(first["audio_cpp_voice_ref"])
    first["audio_cpp_voice_ref"]["data"] = "mutated"
    first["audio_cpp_reference_text"] = "mutated"
    first["provider_configs"][0]["api_key"] = "mutated"
    second_settings = graph.settings(provider_configs=[{"api_key": "second"}], speed=2)
    second = runtime.prepare(second_settings, legacy_prepare=forbidden)
    assert second["audio_cpp_voice_ref"] == verified
    assert second["audio_cpp_reference_text"] == "Reviewed source."
    assert second["provider_configs"] == [{"api_key": "second"}] and second["speed"] == 2
    assert len(reads) == 2
    assert first_settings["provider_configs"] == [{"api_key": "first"}]
    assert "audio_cpp_voice_ref" not in first_settings


def test_parallel_preparations_share_one_verified_material_read(graph, monkeypatch):
    reads: list[str] = []
    original_read = speech_inputs.read_verified_file
    gate = threading.Barrier(3)

    def read(paths, relative, **kwargs):
        reads.append(relative)
        return original_read(paths, relative, **kwargs)

    monkeypatch.setattr(speech_inputs, "read_verified_file", read)
    runtime = current_runtime(graph)

    def prepare(index):
        gate.wait(timeout=5)
        return runtime.prepare(graph.settings(speed=index), legacy_prepare=forbidden)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(prepare, 1), pool.submit(prepare, 2)
        gate.wait(timeout=5)
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert len(reads) == 2
    assert {result["speed"] for result in results} == {1, 2}
    assert results[0]["audio_cpp_voice_ref"] is not results[1]["audio_cpp_voice_ref"]


@pytest.mark.parametrize(
    "mutation", ["snapshot_state", "audio_state", "snapshot_hash", "audio_hash"]
)
def test_current_graph_change_denies_cache_reuse_before_any_new_file_reads(
    graph, monkeypatch, mutation
):
    h = graph.h
    runtime = current_runtime(graph)
    runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    with h.database.immediate_session() as session:
        header = session.get_one(Artifact, graph.prepared.snapshot.artifact_id)
        audio = session.get_one(Artifact, graph.prepared.audio.artifact_id)
        if mutation == "snapshot_state":
            header.state = "stale"
        elif mutation == "audio_state":
            audio.state = "deleted"
        elif mutation == "snapshot_hash":
            header.content_hash = "0" * 64
        else:
            audio.content_hash = "0" * 64
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    with pytest.raises(SpeechInputError):
        runtime.prepare(graph.settings(), legacy_prepare=forbidden)


@pytest.mark.parametrize(
    "mutation", ["foreign_root", "wrong_pin_hash", "snapshot_bytes", "wav_bytes"]
)
def test_invalid_root_hash_or_real_bytes_denies_without_legacy(graph, mutation):
    h = graph.h
    settings = graph.settings()
    owner = h.owner
    if mutation == "foreign_root":
        owner = SpeechInputOwner(h.other.workspace_id, h.owner.project_id, h.owner.session_id)
    elif mutation == "wrong_pin_hash":
        settings[PROJECT_SPEECH_INPUT_KEY]["content_hash"] = "0" * 64
    else:
        selected = graph.prepared.snapshot if mutation == "snapshot_bytes" else graph.prepared.audio
        path = h.paths.managed_path(selected.relative_path)
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
    runtime = GenerationSpeechInputs(h.database, h.paths, owner, lambda: None)
    with pytest.raises(SpeechInputError):
        runtime.prepare(settings, legacy_prepare=forbidden)


def test_caller_denial_precedes_graph_and_file_reads(graph, monkeypatch):
    h = graph.h
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    denied = OSError("Current generation proof denied")

    def deny():
        raise denied

    runtime = GenerationSpeechInputs(h.database, h.paths, h.owner, deny)
    monkeypatch.setattr(runtime_module, "load_speech_input", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    with pytest.raises(OSError) as error:
        runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    assert error.value is denied and statements == []


def test_final_proof_includes_new_graph_and_failure_does_not_cache_or_return(graph, monkeypatch):
    h = graph.h
    calls: list[str] = []
    checks: list[str] = []
    original_check = runtime_module.check_speech_input
    original_load = runtime_module.load_speech_input
    loads = 0
    deny_final = True
    denied = PermissionError("Final graph proof denied")

    def check(session, pin, **kwargs):
        assert calls[-1] == "current-proof"
        checks.append(pin["artifact_id"])
        original_check(session, pin, **kwargs)

    def proof():
        calls.append("current-proof")
        with h.database.snapshot_session() as session:
            runtime.check(session)
        if checks and deny_final:
            raise denied

    def load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "check_speech_input", check)
    monkeypatch.setattr(runtime_module, "load_speech_input", load)
    runtime = GenerationSpeechInputs(h.database, h.paths, h.owner, proof)
    with pytest.raises(PermissionError) as error:
        runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    assert error.value is denied and checks == [graph.pin["artifact_id"]] and loads == 1
    # Re-enable the caller proof; a failed first proof must cause a full reload.
    deny_final = False
    runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    assert loads == 2


def test_selected_graphs_are_distinct_and_current_check_precedes_cache_handoff(graph, monkeypatch):
    checks: list[dict[str, Any]] = []
    original_check = runtime_module.check_speech_input
    proof_seen = False

    def check(session, pin, **kwargs):
        assert proof_seen
        checks.append(deepcopy(pin))
        original_check(session, pin, **kwargs)

    def proof():
        nonlocal proof_seen
        proof_seen = True
        with graph.h.database.snapshot_session() as session:
            runtime.check(session)

    monkeypatch.setattr(runtime_module, "check_speech_input", check)
    runtime = GenerationSpeechInputs(graph.h.database, graph.h.paths, graph.h.owner, proof)
    runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    assert checks == [graph.pin]
    checks.clear()
    runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    assert checks == [graph.pin]
    checks.clear()
    with graph.h.database.snapshot_session() as session:
        runtime.check(session)
    assert checks == [graph.pin]


def test_explicit_null_ignores_later_matching_alias_and_strips_inherited_material(
    graph, monkeypatch
):
    h = graph.h
    settings = graph.settings(
        voice="later",
        audio_cpp_voice_ref={"data": "inherited"},
        audio_cpp_voice_ref_hash="inherited",
        audio_cpp_reference_text="inherited",
    )
    settings[PROJECT_SPEECH_INPUT_KEY] = None
    settings[PROJECT_SPEECH_BINDING_KEY]["voice"] = "later"
    with h.database.immediate_session() as session:
        session.get_one(Voice, h.voice_id).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "resource_kind": "linked_reference",
                    "status": "ready",
                    "voice_id": "later",
                }
            }
        }
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    callbacks: list[str] = []
    runtime = GenerationSpeechInputs(
        h.database, h.paths, h.owner, lambda: callbacks.append("proof")
    )
    monkeypatch.setattr(runtime_module, "load_speech_input", forbidden)
    monkeypatch.setattr(runtime_module, "check_speech_input", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    result = runtime.prepare(settings, legacy_prepare=forbidden)
    assert statements == []
    with h.database.snapshot_session() as session:
        statements.clear()  # The caller opening its transaction emits BEGIN.
        runtime.check(session)
    assert callbacks == ["proof"] and statements == []
    assert not any(
        name in result
        for name in ("audio_cpp_voice_ref", "audio_cpp_voice_ref_hash", "audio_cpp_reference_text")
    )
    assert (
        result[PROJECT_SPEECH_INPUT_KEY] is None
        and settings["audio_cpp_reference_text"] == "inherited"
    )


def test_unmarked_legacy_path_passes_original_settings_without_any_extra_work(graph, monkeypatch):
    h = graph.h
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    runtime = GenerationSpeechInputs(h.database, h.paths, h.owner, forbidden)
    assert statements == []
    with h.database.snapshot_session() as session:
        statements.clear()  # Count work by check(), not the caller's BEGIN.
        runtime.check(session)
    monkeypatch.setattr(runtime_module, "check_speech_input", forbidden)
    monkeypatch.setattr(runtime_module, "load_speech_input", forbidden)
    settings = {"service": "legacy", "provider_configs": [{"api_key": "private"}]}
    sentinel = {"legacy": True}

    def legacy(value):
        assert value is settings
        return sentinel

    assert runtime.prepare(settings, legacy_prepare=legacy) is sentinel
    assert statements == []


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_pin",
        "missing_binding",
        "pin_shape",
        "pin_hash",
        "binding_extra",
        "binding_missing",
        "binding_adapter",
        "binding_empty",
        "settings_adapter",
        "settings_model",
        "settings_voice",
    ],
)
def test_malformed_or_mismatched_markers_deny_before_current_proof(graph, invalid):
    settings = graph.settings()
    if invalid == "missing_pin":
        del settings[PROJECT_SPEECH_INPUT_KEY]
    elif invalid == "missing_binding":
        del settings[PROJECT_SPEECH_BINDING_KEY]
    elif invalid == "pin_shape":
        settings[PROJECT_SPEECH_INPUT_KEY]["source_voice_id"] = "not-allowed"
    elif invalid == "pin_hash":
        settings[PROJECT_SPEECH_INPUT_KEY]["content_hash"] = "invalid"
    elif invalid == "binding_extra":
        settings[PROJECT_SPEECH_BINDING_KEY]["api_key"] = "not-allowed"
    elif invalid == "binding_missing":
        del settings[PROJECT_SPEECH_BINDING_KEY]["voice"]
    elif invalid == "binding_adapter":
        settings[PROJECT_SPEECH_BINDING_KEY]["adapter"] = "other"
    elif invalid == "binding_empty":
        settings[PROJECT_SPEECH_BINDING_KEY]["model"] = ""
    elif invalid == "settings_adapter":
        settings["service"] = "other"
    elif invalid == "settings_model":
        settings["model"] = "other"
    else:
        settings["voice"] = "other"
    runtime = GenerationSpeechInputs(graph.h.database, graph.h.paths, graph.h.owner, forbidden)
    with pytest.raises(SpeechInputError):
        runtime.prepare(settings, legacy_prepare=forbidden)


@pytest.mark.parametrize(
    "bad_owner",
    [
        None,
        SpeechInputOwner(""),
        SpeechInputOwner("w", "p"),
        SpeechInputOwner("w", None, "s"),
        SpeechInputOwner("w", "p", " "),
        SpeechInputOwner("w ", "p", "s"),
    ],
)
def test_constructor_rejects_incomplete_owners_without_queries(graph, bad_owner):
    statements: list[str] = []
    event.listen(
        graph.h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with pytest.raises(SpeechInputError):
        GenerationSpeechInputs(graph.h.database, graph.h.paths, cast(Any, bad_owner), lambda: None)
    assert statements == []


def test_constructor_requires_callable_callback(graph):
    with pytest.raises(TypeError):
        GenerationSpeechInputs(graph.h.database, graph.h.paths, graph.h.owner, cast(Any, None))


def test_check_tracks_each_selected_graph_once_and_ignores_unused_retired_graph(graph, monkeypatch):
    h = graph.h

    def make_pin(transcript):
        with h.database.immediate_session() as session:
            session.get_one(VoiceSample, h.sample_id).transcript = transcript
        captured = h.capture()
        return h.publish(captured, h.prepare(captured))

    second_pin = make_pin("Second selected reference")
    unused_pin = make_pin("Unused reference")
    with h.database.immediate_session() as session:
        session.get_one(Artifact, unused_pin["artifact_id"]).state = "stale"
    checked: list[str] = []
    original_check = runtime_module.check_speech_input

    def check(session, pin, **kwargs):
        checked.append(pin["artifact_id"])
        original_check(session, pin, **kwargs)

    monkeypatch.setattr(runtime_module, "check_speech_input", check)
    runtime = current_runtime(graph)
    runtime.prepare(graph.settings(), legacy_prepare=forbidden)
    second = graph.settings()
    second[PROJECT_SPEECH_INPUT_KEY] = second_pin
    runtime.prepare(second, legacy_prepare=forbidden)
    checked.clear()
    with h.database.snapshot_session() as session:
        runtime.check(session)
    assert checked == [graph.pin["artifact_id"], second_pin["artifact_id"]]
    checked.clear()
    runtime.prepare(second, legacy_prepare=forbidden)
    assert checked == [graph.pin["artifact_id"], second_pin["artifact_id"]]
