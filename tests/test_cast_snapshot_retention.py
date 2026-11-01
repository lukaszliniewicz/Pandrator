"""Frozen cast v2 keys retain exact, session-owned speech references."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event

from pandrator.web import generation_cast_runtime as runtime
from pandrator.web import generation_voice_references, project_speech_references, speech_inputs
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied, WorkspaceNotFound
from pandrator.web.generation_control_schemas import VoiceBinding
from pandrator.web.generation_rendering import _voice_key
from pandrator.web.project_speech_references import put_project_speech_reference
from pandrator.web.projects.identity import session_project
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    SpeechInputError,
    canonical_json,
)
from tests.test_performance_plans import case as case

MODEL = "qwen3_tts_1_7b_base_q8_0"


def _settings() -> dict[str, Any]:
    return {
        "service": "audio_cpp",
        "model": MODEL,
        "xtts_model": MODEL,
        "voice_mode_version": 1,
        "casting_enabled": True,
        "performance_enabled": False,
        "tts_context_mode": "off",
    }


def _binding() -> dict[str, Any]:
    return {"voice_id": str(uuid4())}


def _normalized(binding: Mapping[str, Any]) -> dict[str, Any]:
    return VoiceBinding.model_validate(dict(binding)).model_dump(mode="json")


def _retained_resolution(
    binding: Mapping[str, Any],
    pin: dict[str, Any] | None,
    alias: str = "Saved native voice",
) -> dict[str, Any]:
    normalized = _normalized(binding)
    return {
        **normalized,
        "voice": alias,
        PROJECT_SPEECH_INPUT_KEY: pin,
        PROJECT_SPEECH_BINDING_KEY: {
            "adapter": "audio_cpp",
            "model": MODEL,
            "voice": alias,
            "display_name": alias,
        },
        PROJECT_SPEECH_SELECTOR_KEY: runtime._binding_key(normalized),
    }


def _add_pin(
    case: dict[str, Any], label: str, *, content_hash: str | None = None
) -> dict[str, Any]:
    database = case["services"]["database"]
    with database.session() as session:
        project = session_project(session, case["session_id"])
        digest = content_hash or hashlib.sha256(label.encode()).hexdigest()
        audio_id = str(uuid4())
        snapshot_id = str(uuid4())
        session.add_all(
            [
                m.Artifact(
                    id=audio_id,
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    session_id=case["session_id"],
                    kind="audio",
                    role="speech_reference_audio",
                    relative_path=f"synthetic/{label}/audio.wav",
                    mime_type="audio/wav",
                    size_bytes=16,
                    content_hash=hashlib.sha256(f"{label} audio".encode()).hexdigest(),
                    state="current",
                ),
                m.Artifact(
                    id=snapshot_id,
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    session_id=case["session_id"],
                    kind="speech_voice_snapshot",
                    role="speech_input",
                    relative_path=f"synthetic/{label}/snapshot.json",
                    mime_type="application/json",
                    size_bytes=32,
                    content_hash=digest,
                    state="current",
                ),
            ]
        )
        session.flush()
        session.add(
            m.ArtifactEdge(
                parent_artifact_id=audio_id,
                child_artifact_id=snapshot_id,
                relation="speech_reference",
            )
        )
        session.flush()
    return {"version": 1, "artifact_id": snapshot_id, "content_hash": digest}


def _save_reference(
    case: dict[str, Any],
    slot: str,
    binding: Mapping[str, Any],
    pin: dict[str, Any] | None,
    alias: str = "Saved native voice",
) -> None:
    database = case["services"]["database"]
    with database.session() as session:
        put_project_speech_reference(
            session,
            case["session_id"],
            slot,
            selector=dict(binding),
            binding={
                "adapter": "audio_cpp",
                "model": MODEL,
                "voice": alias,
                "display_name": alias,
            },
            pin=pin,
            expected_revision=0,
            authorization_guard=lambda _session: None,
        )


def _sql_log(statements: list[str]):
    def record(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(str(statement).casefold())

    return record


def test_v1_v2_resolution_keys_are_exact_and_copies_are_defensive() -> None:
    binding = {"voice": "Character voice"}
    cast_slot = "cast:narrator"
    segment_slot = f"segment:{uuid4()}"
    v1_key = runtime._binding_key(binding)
    assert runtime.cast_resolution_key(binding, cast_slot, schema_version=1) == v1_key
    assert runtime.cast_resolution_key(binding, segment_slot, schema_version=1) == v1_key
    cast_key = runtime.cast_resolution_key(binding, cast_slot, schema_version=2)
    segment_key = runtime.cast_resolution_key(binding, segment_slot, schema_version=2)
    none_key = runtime.cast_resolution_key(binding, None, schema_version=2)
    assert len({cast_key, segment_key, none_key}) == 3
    assert runtime.cast_snapshot_version({}) == 1
    assert runtime.cast_snapshot_version({"schema_version": 2}) == 2
    invalid_versions: tuple[object, ...] = (True, 0, 3, "2")
    for invalid in invalid_versions:
        with pytest.raises(ValueError, match="snapshot"):
            runtime.cast_snapshot_version({"schema_version": invalid})
        with pytest.raises(ValueError, match="snapshot"):
            runtime.cast_resolution_key(binding, schema_version=cast(int, invalid))

    nested = {"voice": "Saved", "metadata": {"values": ["original"]}}
    frozen = {
        "schema_version": 2,
        "resolved_bindings": {cast_key: nested, v1_key: {"voice": "legacy"}},
        "segments": {
            "one": {"resolved_bindings": {cast_key: {"voice": "entry"}}}
        },
    }
    selected = runtime.lookup_cast_resolution(frozen, binding, cast_slot)
    assert selected is not None
    assert selected == nested
    assert selected is not nested
    selected["metadata"]["values"].append("changed")
    assert nested["metadata"]["values"] == ["original"]
    assert runtime.lookup_cast_resolution(frozen, binding, segment_slot) is None
    assert runtime.lookup_cast_resolution(frozen, binding, None) is None

    entry_snapshot = runtime.segment_cast_snapshot(frozen, "one")
    assert entry_snapshot is not frozen
    assert entry_snapshot["segments"] is frozen["segments"]
    assert entry_snapshot["resolved_bindings"] == {cast_key: {"voice": "entry"}}
    assert entry_snapshot["resolved_bindings"] is not frozen["segments"]["one"]["resolved_bindings"]
    assert runtime.lookup_cast_resolution(
        {"resolved_bindings": {v1_key: {"voice": "legacy"}}}, binding, segment_slot
    ) == {"voice": "legacy"}


def test_apply_retained_cast_clears_narrator_identity_and_keeps_pin_identity() -> None:
    binding = _binding()
    normalized = _normalized(binding)
    shared_hash = hashlib.sha256(b"same retained bytes").hexdigest()
    first_pin = {"version": 1, "artifact_id": str(uuid4()), "content_hash": shared_hash}
    second_pin = {"version": 1, "artifact_id": str(uuid4()), "content_hash": shared_hash}
    first = _retained_resolution(binding, first_pin)
    second = _retained_resolution(binding, second_pin)
    key = runtime._binding_key(normalized)
    settings = {
        **_settings(),
        "voice": "Narrator",
        "speaker": "Narrator",
        "voice_id": "narrator-id",
        "voice_description": "Narrator description",
        "audio_cpp_voice_ref": "old waveform",
        "audio_cpp_voice_ref_hash": "old hash",
        "audio_cpp_reference_text": "old text",
    }

    applied = runtime.apply_resolved_binding(
        binding, settings, {key: first}
    )
    same_content = runtime.apply_resolved_binding(
        binding, settings, {key: second}
    )
    assert applied["voice"] == applied["speaker"] == "Saved native voice"
    assert applied["_cast_voice_id"] == binding["voice_id"]
    assert applied["model"] == applied["xtts_model"] == MODEL
    for field in (
        "voice_id",
        "voice_description",
        "audio_cpp_voice_ref",
        "audio_cpp_voice_ref_hash",
        "audio_cpp_reference_text",
    ):
        assert field not in applied
    assert applied[PROJECT_SPEECH_INPUT_KEY] == first_pin
    assert runtime._retained_binding_markers(applied)
    assert _voice_key(applied, binding) == _voice_key(same_content, binding)

    different_pin = {
        **first_pin,
        "content_hash": hashlib.sha256(b"different retained bytes").hexdigest(),
    }
    different = runtime.apply_resolved_binding(
        binding,
        settings,
        {key: _retained_resolution(binding, different_pin)},
    )
    assert _voice_key(applied, binding) != _voice_key(different, binding)
    unretained = runtime.apply_resolved_binding(
        binding,
        settings,
        {key: {**normalized, "voice": "Saved native voice"}},
    )
    assert PROJECT_SPEECH_INPUT_KEY not in unretained
    assert _voice_key(applied, binding) != _voice_key(unretained, binding)

    malformed = dict(first)
    malformed.pop(PROJECT_SPEECH_SELECTOR_KEY)
    with pytest.raises(ValueError, match="Incomplete"):
        runtime.apply_resolved_binding(binding, settings, {key: malformed})
    wrong_selector = {**first, PROJECT_SPEECH_SELECTOR_KEY: "0" * 64}
    with pytest.raises(ValueError, match="selector"):
        runtime.apply_resolved_binding(binding, settings, {key: wrong_selector})
    wrong_model = {
        **first,
        PROJECT_SPEECH_BINDING_KEY: {
            **first[PROJECT_SPEECH_BINDING_KEY],
            "model": "different-model",
        },
    }
    with pytest.raises(ValueError, match="model"):
        runtime.apply_resolved_binding(binding, settings, {key: wrong_model})


def test_freeze_v2_looks_up_each_segment_slot_without_source_library(case, monkeypatch):
    binding = _binding()
    ids = case["segment_ids"]
    shared_hash = hashlib.sha256(b"same retained content").hexdigest()
    pins = [
        _add_pin(case, "first", content_hash=shared_hash),
        _add_pin(case, "second"),
        _add_pin(case, "third", content_hash=shared_hash),
    ]
    for segment_id, pin in zip(ids, pins, strict=True):
        _save_reference(case, f"segment:{segment_id}", binding, pin)
    database = case["services"]["database"]
    with database.session() as session:
        first = session.get(m.GenerationSegment, ids[0])
        assert first is not None
        first.text = "First. Second."
        first.speech_plan_json = {
            "speech_xml": f'<segment id="{ids[0]}">First. <span>Second.</span></segment>'
        }

    monkeypatch.setattr(
        runtime,
        "segment_voice_binding",
        lambda _segment, _snapshot=None: dict(binding),
    )
    monkeypatch.setattr(
        generation_voice_references,
        "voice_reference_index",
        lambda *_args, **_kwargs: pytest.fail("retained pins must not index the source library"),
    )
    statements: list[str] = []
    listener = _sql_log(statements)
    event.listen(database.engine, "before_cursor_execute", listener)
    try:
        snapshot = {"tts": _settings()}
        with database.session() as session:
            with patch.object(
                runtime,
                "resolve_project_cast_binding",
                wraps=runtime.resolve_project_cast_binding,
            ) as saved_lookup:
                runtime.freeze_cast_snapshot(
                    session, case["revision_id"], snapshot, snapshot["tts"]
                )
            assert saved_lookup.call_count == 3
    finally:
        event.remove(database.engine, "before_cursor_execute", listener)

    assert snapshot["generation_control_snapshot"]["schema_version"] == 2
    assert not any(re.search(r"\b(?:voices|voice_samples)\b", sql) for sql in statements)
    frozen = snapshot["generation_control_snapshot"]
    parts = [
        runtime.segment_render_parts(
            snapshot["tts"], snapshot, segment_id, text
        )[0]
        for segment_id, text in zip(ids, ("First. Second.", "Yet this defeat would be temporary.", "Membership doubled soon afterwards."), strict=True)
    ]
    assert [part["settings"][PROJECT_SPEECH_INPUT_KEY] for part in parts] == pins
    keys = [
        _voice_key(part["settings"], binding)
        for part in parts
    ]
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]
    assert len(frozen["resolved_bindings"]) == 3


def test_preview_uses_saved_segment_pin_before_managed_live_resolution(case, monkeypatch):
    binding = _binding()
    segment_id = case["segment_ids"][0]
    pin = _add_pin(case, "preview")
    _save_reference(case, f"segment:{segment_id}", binding, pin)
    monkeypatch.setattr(
        runtime,
        "segment_voice_binding",
        lambda _segment: dict(binding),
    )
    monkeypatch.setattr(
        generation_voice_references,
        "voice_reference_index",
        lambda *_args, **_kwargs: pytest.fail("preview must not index the source library"),
    )
    database = case["services"]["database"]
    statements: list[str] = []
    listener = _sql_log(statements)
    event.listen(database.engine, "before_cursor_execute", listener)
    try:
        with database.session() as session:
            with patch.object(
                runtime,
                "resolve_binding",
                wraps=runtime.resolve_binding,
            ) as resolve:
                parts = runtime.preview_render_parts(
                    session,
                    case["session_id"],
                    "Preview the saved voice.",
                    segment_id,
                    _settings(),
                    None,
                )
            assert resolve.call_count == 1
            assert all(not call.args[1].get("voice_id") for call in resolve.call_args_list)
    finally:
        event.remove(database.engine, "before_cursor_execute", listener)

    assert parts[0]["settings"][PROJECT_SPEECH_INPUT_KEY] == pin
    assert not any(re.search(r"\b(?:voices|voice_samples)\b", sql) for sql in statements)


def test_saved_cast_resolver_skips_absent_slot_and_noncloning_modes(monkeypatch):
    monkeypatch.setattr(
        project_speech_references,
        "get_project_speech_reference",
        lambda *_args, **_kwargs: pytest.fail("inapplicable choices must skip the store"),
    )
    assert runtime.resolve_project_cast_binding(
        None, "session", _binding(), _settings(), None
    ) is None


@pytest.mark.parametrize("denial", [AccessDenied, WorkspaceNotFound])
@pytest.mark.parametrize("managed", [False, True])
def test_live_reference_index_denial_only_omits_pure_alias_fingerprint(
    case, monkeypatch, denial: type[Exception], managed: bool
):
    segment_id = case["segment_ids"][0]
    binding = {"voice_id": str(uuid4())} if managed else {"voice": "Native alias"}
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, segment_id)
        assert segment is not None
        segment.voice = "Native alias"
    monkeypatch.setattr(
        runtime,
        "segment_voice_binding",
        lambda _segment, _snapshot=None: dict(binding),
    )

    def deny_index(*_args, **_kwargs):
        raise denial()

    monkeypatch.setattr(generation_voice_references, "voice_reference_index", deny_index)
    if managed:
        normalized = _normalized(binding)
        monkeypatch.setattr(
            runtime,
            "resolve_binding",
            lambda *_args, **_kwargs: {**normalized, "voice": "Native alias"},
        )

    snapshot = {"tts": _settings()}
    database = case["services"]["database"]
    if managed:
        with database.session() as session, pytest.raises(denial):
            runtime.freeze_cast_snapshot(
                session, case["revision_id"], snapshot, snapshot["tts"]
            )
        return

    with database.session() as session:
        runtime.freeze_cast_snapshot(
            session, case["revision_id"], snapshot, snapshot["tts"]
        )
    key = runtime.cast_resolution_key(
        binding, f"segment:{segment_id}", schema_version=2
    )
    assert "_recast_reference_fingerprint" not in snapshot[
        "generation_control_snapshot"
    ]["resolved_bindings"][key]

    monkeypatch.setattr(
        runtime,
        "resolve_capabilities",
        lambda *_args, **_kwargs: {"model": MODEL, "voice_mode": "prebuilt"},
    )
    assert runtime.resolve_project_cast_binding(
        None, "session", _binding(), _settings(), f"segment:{uuid4()}"
    ) is None

    monkeypatch.setattr(
        runtime,
        "resolve_capabilities",
        lambda *_args, **_kwargs: {"model": MODEL, "voice_mode": "cloning"},
    )
    assert runtime.resolve_project_cast_binding(
        None,
        "session",
        {"voice": "Native", "service": "gemini"},
        _settings(),
        f"segment:{uuid4()}",
    ) is None


def test_scoped_retained_cast_versions_keep_entry_maps_and_drop_private_carry(case, monkeypatch):
    binding = _binding()
    cast_slot = "cast:" + canonical_json(["characters", "c-one"])
    key = runtime.cast_resolution_key(binding, cast_slot, schema_version=2)
    segment_ids = case["segment_ids"][:2]
    pins = [_add_pin(case, "scoped-first"), _add_pin(case, "scoped-second")]
    controls_by_segment: dict[str, dict[str, Any]] = {}
    for segment_id, pin in zip(segment_ids, pins, strict=True):
        controls_by_segment[segment_id] = {
            "characters": [{"id": "c-one", "display_name": "Character One"}],
            "cast": {"characters": {"c-one": dict(binding)}},
            "_retained_cast_resolutions": {
                key: _retained_resolution(binding, pin, alias="Historical voice")
            },
        }
    database = case["services"]["database"]
    with database.session() as session:
        for index, segment_id in enumerate(segment_ids):
            segment = session.get(m.GenerationSegment, segment_id)
            assert segment is not None
            segment.text = f"Scoped line {index + 1}."
            segment.speech_plan_json = {
                "speech_xml": (
                    f'<segment id="{segment_id}">'
                    f'<speaker ref="c-one">Scoped line {index + 1}.</speaker>'
                    "</segment>"
                )
            }

    monkeypatch.setattr(
        runtime,
        "resolve_project_cast_binding",
        lambda *_args, **_kwargs: pytest.fail("validated carried choices must not fall back"),
    )
    monkeypatch.setattr(
        generation_voice_references,
        "voice_reference_index",
        lambda *_args, **_kwargs: pytest.fail("retained carry must not index the source library"),
    )
    snapshot = {"tts": _settings()}
    with database.session() as session:
        runtime.freeze_cast_snapshot(
            session,
            case["revision_id"],
            snapshot,
            snapshot["tts"],
            controls_by_segment=controls_by_segment,
        )

    frozen = snapshot["generation_control_snapshot"]
    assert frozen["resolved_bindings"][key][PROJECT_SPEECH_INPUT_KEY] == pins[0]
    for segment_id, pin in zip(segment_ids, pins, strict=True):
        entry = frozen["segments"][segment_id]
        assert "_retained_cast_resolutions" not in entry["controls"]
        assert len(entry["resolved_bindings"]) == 1
        selected = runtime.segment_cast_snapshot(frozen, segment_id)
        assert selected["resolved_bindings"][key][PROJECT_SPEECH_INPUT_KEY] == pin
    parts = [
        runtime.segment_render_parts(
            snapshot["tts"],
            snapshot,
            segment_id,
            f"Scoped line {index + 1}.",
        )[0]
        for index, segment_id in enumerate(segment_ids)
    ]
    assert [part["settings"][PROJECT_SPEECH_INPUT_KEY] for part in parts] == pins


def test_current_saved_cast_slot_and_graph_are_reused_across_segments(case, monkeypatch):
    binding = _binding()
    cast_slot = "cast:" + canonical_json(["characters", "c-one"])
    pin = _add_pin(case, "shared-cast-slot")
    _save_reference(case, cast_slot, binding, pin)
    segment_ids = case["segment_ids"][:2]
    controls = {
        "characters": [{"id": "c-one", "display_name": "Character One"}],
        "cast": {"characters": {"c-one": dict(binding)}},
    }
    controls_by_segment = {segment_id: dict(controls) for segment_id in segment_ids}
    database = case["services"]["database"]
    with database.session() as session:
        for index, segment_id in enumerate(segment_ids):
            segment = session.get(m.GenerationSegment, segment_id)
            assert segment is not None
            segment.text = f"Shared cast line {index + 1}."
            segment.speech_plan_json = {
                "speech_xml": (
                    f'<segment id="{segment_id}">'
                    f'<speaker ref="c-one">Shared cast line {index + 1}.</speaker>'
                    "</segment>"
                )
            }
    monkeypatch.setattr(
        generation_voice_references,
        "voice_reference_index",
        lambda *_args, **_kwargs: pytest.fail("saved cast selection must not index the source library"),
    )
    snapshot = {"tts": _settings()}
    with database.session() as session:
        with (
            patch.object(
                runtime,
                "resolve_project_cast_binding",
                wraps=runtime.resolve_project_cast_binding,
            ) as saved_lookup,
            patch.object(
                speech_inputs,
                "check_speech_input",
                wraps=speech_inputs.check_speech_input,
            ) as graph_check,
        ):
            runtime.freeze_cast_snapshot(
                session,
                case["revision_id"],
                snapshot,
                snapshot["tts"],
                controls_by_segment=controls_by_segment,
            )
        assert saved_lookup.call_count == 1
        assert graph_check.call_count == 1
    frozen = snapshot["generation_control_snapshot"]
    assert len(frozen["resolved_bindings"]) == 1
    assert all("resolved_bindings" not in frozen["segments"][segment_id] for segment_id in segment_ids)


def test_retained_validator_rejects_noncloning_or_changed_public_identity(monkeypatch):
    binding = _binding()
    resolution = _retained_resolution(binding, None)
    settings = _settings()

    changed_identity = {**resolution, "voice_id": str(uuid4())}
    with pytest.raises(ValueError, match="source selector"):
        runtime.validate_retained_cast_resolution(
            None, "session", binding, settings, changed_identity
        )

    for mode in ("prebuilt", "design"):
        monkeypatch.setattr(
            runtime,
            "resolve_capabilities",
            lambda *_args, _mode=mode, **_kwargs: {"model": MODEL, "voice_mode": _mode},
        )
        with pytest.raises(ValueError, match="incompatible"):
            runtime.validate_retained_cast_resolution(
                None, "session", binding, settings, resolution
            )

    described = {**binding, "voice_description": "A designed voice."}
    described_resolution = _retained_resolution(described, None)
    with pytest.raises(ValueError, match="incompatible"):
        runtime.validate_retained_cast_resolution(
            None, "session", described, settings, described_resolution
        )


def test_retained_validator_rejects_retired_pin_graph(case):
    binding = _binding()
    retired_pin = {
        "version": 1,
        "artifact_id": str(uuid4()),
        "content_hash": hashlib.sha256(b"retired").hexdigest(),
    }
    resolution = _retained_resolution(binding, retired_pin)
    with case["services"]["database"].session() as session:
        with pytest.raises(SpeechInputError):
            runtime.validate_retained_cast_resolution(
                session,
                case["session_id"],
                binding,
                _settings(),
                resolution,
            )
