"""Drama 3 lowering preserves authoring text and native request boundaries."""

import re

import pytest

from pandrator.logic.audio_cpp_catalogue import package_metadata
from pandrator.logic.audiobook_chunking import audiobook_chunk_budget
from pandrator.logic.speech_performance import compile_performance
from pandrator.logic.tts_generation_limits import generation_limit_metadata
from pandrator.logic.tts_handler import _build_audio_cpp_audio_payload
from pandrator.logic.tts_provider_profiles import (
    AUDIO_CPP_MODEL_DEFAULT_VOICES,
    AUDIO_CPP_MODEL_PRESETS,
)
from pandrator.web.generation_rendering import build_render_parts
from pandrator_manager.components.audiocpp import MODEL_PACKAGES

MODEL = "fish_drama3_small_iq3_xxs"
DRAMA_MODELS = (
    MODEL, "fish_drama3_q2_experts_q8_backbone", "fish_drama3_q3_experts_q8_backbone",
    "fish_drama3_q4_experts_q8_backbone", "fish_drama3_q4_backbone",
)


def settings(**extra):
    return {"service": "audio_cpp", "model": MODEL, **extra}


@pytest.mark.parametrize("model", DRAMA_MODELS)
def test_drama_packages_have_a_separate_expressive_contract(model):
    metadata = package_metadata(model)
    assert metadata["voice_mode"] == "optional_cloning"
    assert metadata["reference_audio"] == "optional"
    assert metadata["license"]["commercial_use"] == "conditional"
    assert metadata["upstream_features"]["multi_speaker"] is True
    assert metadata["pandrator_features"]["native_multi_speaker"] == "not_implemented"
    assert metadata["expressive_capabilities"]["dialect"] == "fish_drama3"
    assert metadata["expressive_capabilities"]["native_instruction_scope"] == ["request", "span"]
    assert metadata["expressive_capabilities"]["semantic_context"] == "none"
    assert metadata["package_availability"]["status"] == "installable"
    assert MODEL_PACKAGES[model].family == "fish_drama3"
    assert len(MODEL_PACKAGES[model].revision) == 40


def test_legacy_pssml_uses_paired_phrase_tags_and_drama_event_spelling():
    text = "Keep this secret."
    compiled = compile_performance(text, settings(
        generation_prompt="calm",
        performance_allow_vocalizations=True,
        _performance={
            "decision": "steer",
            "spans": [{"anchor": {"quote": "this"}, "delivery": {"instruction": "whisper"}}],
            "events": [{"kind": "laugh"}, {"kind": "sigh", "position": "after"}],
        },
    ))
    assert compiled.transcript == text
    assert compiled.input == "[calm][laughs]Keep <whisper>this</whisper> secret.[sighs]"
    assert compiled.instructions == ""
    assert compiled.request_options["text_chunk_mode"] == "tag_aware"


def test_xml_workflow_scopes_effective_directions_without_extra_voice_parts():
    text = "Keep this secret."
    parts = build_render_parts(text, settings(), speech_xml=(
        '<segment id="s"><ins>calm</ins>Keep '
        '<span><ins>whisper</ins>this</span> secret.</segment>'
    ), segment_id="s")
    assert len(parts) == 1
    compiled = compile_performance(parts[0]["text"], parts[0]["settings"])
    assert compiled.transcript == text
    assert compiled.input == "<calm>Keep </calm><calm; whisper>this</calm; whisper><calm> secret.</calm>"
    assert re.sub(r"</?[^>]+>", "", compiled.input) == text
    assert compiled.instructions == ""


def test_adjacent_phrase_tags_close_before_the_next_opens():
    compiled = compile_performance("OneTwo", settings(_performance={
        "decision": "steer", "spans": [
            {"anchor": {"quote": "One"}, "delivery": {"instruction": "slow"}},
            {"anchor": {"quote": "Two"}, "delivery": {"instruction": "whisper"}},
        ],
    }))
    assert compiled.input == "<slow>One</slow><whisper>Two</whisper>"


def test_payload_keeps_cloning_separate_from_delivery_and_uses_native_sampling():
    reference = {"type": "base64", "data": "data:audio/wav;base64,ZmFrZQ=="}
    result = _build_audio_cpp_audio_payload("Hello.", settings(
        generation_prompt="warmly", language="en", speed=1,
        voice="local-sample", audio_cpp_voice_ref=reference,
        audio_cpp_reference_text="Reference text.",
        audio_cpp_model_settings={MODEL: {"top_k": 4097, "text_chunk_mode": "default"}},
    ), {})
    assert result["input"] == "[warmly]Hello."
    assert result["voice_ref"] == reference
    assert result["reference_text"] == "Reference text."
    assert not {"voice", "instructions", "language", "speed"} & result.keys()
    assert result["options"]["top_k"] == 4097
    assert result["options"]["text_chunk_mode"] == "tag_aware"
    assert result["options"]["text_chunk_size"] >= len(result["input"])
    assert not {"instruct", "instruction", "speed"} & result["options"].keys()


@pytest.mark.parametrize("field,value", [
    ("references", "[]"), ("multi_reference_cond", "[]"),
    ("previous_takes", "[]"), ("previous_takes_file", "private.json"),
    ("encode_reference_only", True),
])
def test_legacy_options_cannot_bypass_unimplemented_native_workflows(field, value):
    with pytest.raises(ValueError, match="Unknown audio.cpp option"):
        _build_audio_cpp_audio_payload("Hello.", settings(audio_cpp_options={field: value}), {})


@pytest.mark.parametrize("direction", ["[laughs]", "<whisper>", "<|speaker:1|>", "calm\x00"])
def test_directions_cannot_inject_native_syntax(direction):
    with pytest.raises(ValueError):
        compile_performance("Hello.", settings(generation_prompt=direction))


def test_disabling_performance_preserves_plain_text_and_fingerprints_differ():
    text = "Hello."
    directed = settings(_performance={"decision": "steer", "delivery": {"instruction": "calm"}})
    enabled = compile_performance(text, directed)
    disabled = compile_performance(text, {**directed, "performance_enabled": False})
    assert disabled.transcript == disabled.input == text
    assert disabled.fingerprint != enabled.fingerprint


@pytest.mark.parametrize("model", ["plapre_3_bf16", "plapre_3_f32"])
def test_plapre_presets_and_reference_path_are_distinct(model):
    metadata = package_metadata(model)
    assert metadata["supported_languages"] == ["da"]
    assert metadata["voice_mode"] == "hybrid"
    assert metadata["license"]["commercial_use"] == "permitted_with_attribution"
    assert AUDIO_CPP_MODEL_PRESETS[model] == ["tor", "ida", "liv", "ask", "kaj"]
    assert AUDIO_CPP_MODEL_DEFAULT_VOICES[model] == "tor"
    assert audiobook_chunk_budget({}, {"service": "audio_cpp", "model": model})["target_chars"] == 270
    plain = _build_audio_cpp_audio_payload("Godmorgen.", {
        "model": model, "language": "da", "generation_prompt": "whisper", "speed": 1,
        "voice": AUDIO_CPP_MODEL_DEFAULT_VOICES[model],
    }, {})
    assert plain["input"] == "Godmorgen."
    assert plain["voice"] == "tor"
    assert not {"instructions", "language", "speed"} & plain.keys()
    reference = {"type": "base64", "data": "data:audio/wav;base64,ZmFrZQ=="}
    cloned = _build_audio_cpp_audio_payload("Godmorgen.", {
        "model": model, "audio_cpp_voice_ref": reference,
        "audio_cpp_reference_text": "Godmorgen til dig.",
    }, {})
    assert cloned["voice_ref"] == reference
    assert cloned["reference_text"] == "Godmorgen til dig."
    assert "voice" not in cloned


@pytest.mark.parametrize("model", ["kitten_tts2_bf16", "kitten_tts2_q8_v2", "kitten_tts2_q4"])
def test_updated_kitten_variants_keep_presets_and_conditional_license(model):
    assert MODEL_PACKAGES[model].family == "kitten_tts2"
    assert package_metadata(model)["license"]["commercial_use"] == "conditional"
    assert len(AUDIO_CPP_MODEL_PRESETS[model]) == 48


@pytest.mark.parametrize("model", [
    *DRAMA_MODELS, "kitten_tts2_bf16", "kitten_tts2_q8_v2", "kitten_tts2_q4",
])
def test_longer_narration_policy_matches_catalogue_across_packages(model):
    family = package_metadata(model)["family"]
    policy = audiobook_chunk_budget({}, {"service": "audio_cpp", "model": model})
    limits = generation_limit_metadata(service="audio_cpp", model=model, family=family)
    assert policy["target_chars"] == limits["default_segment_characters"] == 540
    assert limits["runtime_chunk_characters"] == (128 if family == "fish_drama3" else 380)
    assert "input_characters" not in limits


def test_kitten_still_rejects_requests_above_its_enforced_chunk_ceiling():
    settings = {"model": "kitten_tts2_q8_v2"}
    payload = _build_audio_cpp_audio_payload("a" * 1000, settings, {})
    assert payload["options"]["text_chunk_size"] == 1000
    with pytest.raises(ValueError, match="1000-character chunk limit"):
        _build_audio_cpp_audio_payload("a" * 1001, settings, {})
