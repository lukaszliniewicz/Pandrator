"""Opt-in null-input validation and remapping for saved speech copies."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from uuid import UUID

import pytest

from pandrator.web.generation_control_schemas import CastSettings, VoiceBinding
from pandrator.web.generation_controls import iter_cast_voice_bindings
from pandrator.web.project_speech_references import (
    speech_reference_selector,
    speech_reference_selector_hash,
)
from pandrator.web.projects.copy_speech import (
    freeze_cast_reference,
    remap_cast_reference,
    remap_tts_reference,
    validate_speech_copy_graph,
    validate_tts_copy_graph,
)
from pandrator.web.speech_input_contracts import SpeechInputOwner

WORKSPACE_ID = "source-workspace"
PROJECT_ID = "source-project"
SESSION_ID = "source-session"
TARGET_SESSION_ID = "target-session"
MODEL = "qwen3_tts_1_7b_base_q8_0"
TTS_EFFECTIVE = {"service": "audio_cpp", "model": MODEL, "voice": "Narrator"}
CAST_EFFECTIVE = {"service": "audio_cpp", "model": MODEL, "voice": "unused"}


def _voice_binding(voice: str) -> VoiceBinding:
    return VoiceBinding(voice=voice, service="audio_cpp", model=MODEL)


def _reference(
    slot: str,
    binding: VoiceBinding,
    *,
    pin: tuple[str, str] | None = None,
) -> dict:
    selector = speech_reference_selector(binding.model_dump(mode="json"))
    return {
        "session_id": SESSION_ID,
        "slot_key": slot,
        "selector_json": selector,
        "selector_hash": speech_reference_selector_hash(selector),
        "binding_json": {
            "adapter": "audio_cpp",
            "model": MODEL,
            "voice": binding.voice,
            "display_name": binding.voice,
        },
        "input_artifact_id": None if pin is None else pin[0],
        "input_content_hash": None if pin is None else pin[1],
        "revision": 7,
    }


def _tts_graph(*, include_reference: bool = True) -> dict:
    narrator = VoiceBinding(voice="Narrator", service="audio_cpp", model=MODEL)
    rows = {
        "projects": [{"id": PROJECT_ID, "workspace_id": WORKSPACE_ID}],
        "sessions": [{"id": SESSION_ID, "project_id": PROJECT_ID}],
        "session_settings": [
            {"session_id": SESSION_ID, "section": "tts", "value_json": TTS_EFFECTIVE}
        ],
        "project_speech_references": [],
        "artifacts": [],
        "artifact_edges": [],
        "knowledge_ledgers": [],
    }
    if include_reference:
        rows["project_speech_references"].append(_reference("tts", narrator))
    return rows


def _cast_graph() -> dict:
    cast = CastSettings(
        narrator=_voice_binding("Cast narrator"),
        categories={"male": _voice_binding("Male category")},
        characters={"character-1": _voice_binding("Character voice")},
        source_speakers={"Speaker 1": _voice_binding("Speaker voice")},
    )
    cast_bindings = dict(iter_cast_voice_bindings(cast))
    ledger = {
        "id": "controls-ledger",
        "session_id": SESSION_ID,
        "kind": "generation_controls",
        "source_language": "auto",
        "target_language": "",
        "payload_json": {
            "characters": [{"id": "character-1", "display_name": "A character"}],
            "cast": cast.model_dump(mode="json"),
        },
    }
    rows = {
        "projects": [{"id": PROJECT_ID, "workspace_id": WORKSPACE_ID}],
        "sessions": [{"id": SESSION_ID, "project_id": PROJECT_ID}],
        "session_settings": [
            {"session_id": SESSION_ID, "section": "tts", "value_json": CAST_EFFECTIVE}
        ],
        "knowledge_ledgers": [ledger],
        "project_speech_references": [],
        "artifacts": [],
        "artifact_edges": [],
    }

    for index, (slot, binding) in enumerate(cast_bindings.items()):
        if slot in {"cast:narrator", 'cast:["categories","male"]'}:
            pin = None
        else:
            header_id = str(UUID(int=100 + index * 2))
            content_hash = sha256(header_id.encode()).hexdigest()
            audio_id = str(UUID(int=101 + index * 2))
            rows["artifacts"].extend(
                [
                    {
                        "id": header_id,
                        "workspace_id": WORKSPACE_ID,
                        "project_id": PROJECT_ID,
                        "session_id": SESSION_ID,
                        "state": "current",
                        "kind": "speech_voice_snapshot",
                        "role": "speech_input",
                        "mime_type": "application/json",
                        "size_bytes": 10,
                        "content_hash": content_hash,
                    },
                    {
                        "id": audio_id,
                        "workspace_id": WORKSPACE_ID,
                        "project_id": PROJECT_ID,
                        "session_id": SESSION_ID,
                        "state": "current",
                        "kind": "audio",
                        "role": "speech_reference_audio",
                        "mime_type": "audio/wav",
                        "size_bytes": 10,
                        "content_hash": sha256(audio_id.encode()).hexdigest(),
                    },
                ]
            )
            rows["artifact_edges"].append(
                {
                    "parent_artifact_id": audio_id,
                    "child_artifact_id": header_id,
                    "relation": "speech_reference",
                }
            )
            pin = (header_id, content_hash)
        rows["project_speech_references"].append(_reference(slot, binding, pin=pin))
    return rows


def test_null_narrator_is_opt_in_and_remaps_with_only_its_owner_identity():
    absent = _tts_graph(include_reference=False)
    validate_tts_copy_graph(absent, allow_null_inputs=True)
    assert absent["project_speech_references"] == []

    rows = _tts_graph()
    reference = rows["project_speech_references"][0]
    before = deepcopy(rows)

    validate_tts_copy_graph(rows, allow_null_inputs=True)
    validate_speech_copy_graph(rows, allow_null_inputs=True)
    remapped = remap_tts_reference(
        reference, {SESSION_ID: TARGET_SESSION_ID}, allow_null_inputs=True
    )

    assert reference["input_artifact_id"] is None
    assert reference["input_content_hash"] is None
    assert remapped == {
        **reference,
        "session_id": TARGET_SESSION_ID,
        "revision": 1,
    }
    assert rows == before


def test_complete_cast_graph_accepts_mixed_pins_and_preserves_semantic_slots():
    rows = _cast_graph()
    before = deepcopy(rows)
    validate_speech_copy_graph(rows, allow_null_inputs=True)

    for index, reference in enumerate(rows["project_speech_references"]):
        pin_id = reference["input_artifact_id"]
        identities = {SESSION_ID: TARGET_SESSION_ID}
        if pin_id is not None:
            identities[pin_id] = str(UUID(int=1000 + index))
        remapped = remap_cast_reference(
            reference, identities, allow_null_inputs=True
        )
        assert remapped["session_id"] == TARGET_SESSION_ID
        assert remapped["revision"] == 1
        assert remapped["slot_key"] == reference["slot_key"]
        assert remapped["selector_json"] == reference["selector_json"]
        assert remapped["selector_hash"] == reference["selector_hash"]
        assert remapped["binding_json"] == reference["binding_json"]
        if pin_id is None:
            assert remapped["input_artifact_id"] is None
            assert remapped["input_content_hash"] is None
        else:
            assert remapped["input_artifact_id"] == identities[pin_id]
            assert remapped["input_content_hash"] == reference["input_content_hash"]

    assert rows == before


def test_default_false_still_rejects_null_tts_and_cast_pins():
    tts_rows = _tts_graph()
    tts_reference = tts_rows["project_speech_references"][0]
    with pytest.raises(ValueError):
        validate_tts_copy_graph(tts_rows)
    with pytest.raises(ValueError):
        remap_tts_reference(tts_reference, {SESSION_ID: TARGET_SESSION_ID})

    cast_rows = _cast_graph()
    cast_reference = cast_rows["project_speech_references"][0]
    with pytest.raises(ValueError):
        validate_speech_copy_graph(cast_rows)
    with pytest.raises(ValueError):
        remap_cast_reference(cast_reference, {SESSION_ID: TARGET_SESSION_ID})
    with pytest.raises(ValueError):
        freeze_cast_reference(
            cast_reference,
            CAST_EFFECTIVE,
            binding=_voice_binding("Cast narrator"),
            owner=SpeechInputOwner(WORKSPACE_ID, PROJECT_ID, SESSION_ID),
            artifacts={},
            edges=[],
        )


@pytest.mark.parametrize(
    "defect",
    [
        "selector_hash",
        "binding",
        "model",
        "owner",
        "unknown_slot",
        "missing_cast_reference",
        "partial_artifact_pin",
        "partial_hash_pin",
    ],
)
def test_allowed_null_inputs_keep_existing_graph_rejections(defect: str):
    rows = _cast_graph()
    reference = rows["project_speech_references"][0]
    if defect == "selector_hash":
        reference["selector_hash"] = "f" * 64
    elif defect == "binding":
        reference["binding_json"]["voice"] = "different voice"
    elif defect == "model":
        rows["session_settings"][0]["value_json"]["model"] = "different-model"
    elif defect == "owner":
        rows["projects"][0]["workspace_id"] = ""
    elif defect == "unknown_slot":
        reference["slot_key"] = "segment:00000000-0000-0000-0000-000000000000"
    elif defect == "missing_cast_reference":
        rows["project_speech_references"].pop()
    elif defect == "partial_artifact_pin":
        reference["input_artifact_id"] = "orphan-header"
    else:
        reference["input_content_hash"] = "a" * 64

    with pytest.raises(ValueError):
        validate_speech_copy_graph(rows, allow_null_inputs=True)


def test_null_cast_remap_still_requires_owner_mapping_and_rejects_segment_slots():
    reference = _cast_graph()["project_speech_references"][0]
    with pytest.raises(ValueError):
        remap_cast_reference(reference, {}, allow_null_inputs=True)

    segment_reference = {
        **reference,
        "slot_key": "segment:00000000-0000-0000-0000-000000000000",
    }
    with pytest.raises(ValueError):
        remap_cast_reference(
            segment_reference,
            {SESSION_ID: TARGET_SESSION_ID},
            allow_null_inputs=True,
        )
