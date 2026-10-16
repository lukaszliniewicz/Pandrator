"""New speech packages and Pandrator-owned segmentation contracts."""

import pytest

from pandrator.logic.audio_cpp_catalogue import package_metadata
from pandrator.logic.audio_cpp_inventory_data import family_metadata
from pandrator.logic.audio_cpp_parameters import request_parameters_for_model
from pandrator.logic.audio_cpp_segmentation import (
    SINGLE_SEGMENT_SESSION_CHARACTERS,
    single_segment_options,
)
from pandrator.logic.audiobook_chunking import audiobook_chunk_budget
from pandrator.logic.text_preprocessor import _limit_segment_words
from pandrator.logic.tts_generation_limits import generation_limit_metadata
from pandrator.logic.tts_handler import _build_audio_cpp_audio_payload
from pandrator.logic.tts_provider_profiles import (
    AUDIO_CPP_MODEL_DEFAULT_VOICES,
    AUDIO_CPP_MODEL_PRESETS,
)
from pandrator_manager.components.audiocpp import MODEL_PACKAGES, server_config


@pytest.mark.parametrize(("language", "voice", "native"), [
    ("en", "bf_emma", "en-gb"), ("en_GB", "bf_emma", "en-gb"),
    ("en", "af_heart", "en-us"), ("fr-FR", "ff_siwis", "fr"),
    ("en", "bf_emma(0.6)+bm_george(0.4)", "en-gb"),
])
def test_kokoro_keeps_the_selected_voice_phonemizer_language(language, voice, native):
    payload = _build_audio_cpp_audio_payload("A short passage.", {
        "model": "kokoro_82m_q8_0", "language": language, "voice": voice,
    }, {})
    assert payload["voice"] == voice
    assert payload["language"] == native


@pytest.mark.parametrize(("language", "voice", "native"), [
    ("en-GB", "bf_emma", "en-gb"), ("fr", "ff_siwis", "fr"),
])
def test_kokoro_uses_a_language_matching_default_when_no_voice_is_selected(language, voice, native):
    payload = _build_audio_cpp_audio_payload("A short passage.", {
        "model": "kokoro_82m_q8_0", "language": language,
    }, {})
    assert payload["voice"] == voice
    assert payload["language"] == native


def test_kokoro_preserves_explicit_voice_and_rejects_cross_language_mismatch():
    model = "kokoro_82m_q8_0"
    payload = _build_audio_cpp_audio_payload("A short passage.", {
        "model": model, "language": "en", "voice": "bf_emma",
    }, {"default_voices": {model: "af_heart"}})
    assert payload["voice"] == "bf_emma"
    assert payload["language"] == "en-gb"
    with pytest.raises(ValueError, match="Kokoro voice matching"):
        _build_audio_cpp_audio_payload("Bonjour.", {"model": model, "voice": "af_heart", "language": "fr"}, {})


@pytest.mark.parametrize("precision", ["q8_0", "bf16"])
def test_voxtral_presets_contract_and_language_metadata(precision):
    model = f"voxtral_4b_tts_2603_{precision}"
    metadata = package_metadata(model)
    assert metadata["voice_mode"] == "prebuilt"
    assert metadata["license"]["commercial_use"] == "noncommercial"
    assert set(metadata["supported_languages"]) == {
        "en", "fr", "es", "de", "it", "pt", "nl", "ar", "hi"
    }
    assert len(AUDIO_CPP_MODEL_PRESETS[model]) == 20
    assert AUDIO_CPP_MODEL_DEFAULT_VOICES[model] == "casual_male"
    payload = _build_audio_cpp_audio_payload(
        "Bonjour à tous.",
        {"model": model, "speaker": "fr_female", "language": "fr",
         "generation_prompt": "Whisper.", "speed": 1,
         "audio_cpp_voice_ref": {"type": "base64", "data": "data:audio/wav;base64,ZmFrZQ=="},
         "audio_cpp_reference_text": "A stale transcript."},
        {},
    )
    assert payload["voice"] == "fr_female"
    assert not {"language", "instructions", "voice_ref", "reference_text", "speed"} & payload.keys()
    assert not {"instruction", "instruct", "reference_text", "speed"} & payload["options"].keys()
    assert "80 ms" in metadata["request_parameters"]["max_tokens"]["description"]
    assert MODEL_PACKAGES[model].family == "voxtral_tts"


@pytest.mark.parametrize("precision", ["q8_0", "bf16"])
def test_french_pocket_has_cosette_french_load_options_and_author_temperature(precision):
    model = f"pocket_tts_french_{precision}"
    assert package_metadata(model)["supported_languages"] == ["fr"]
    assert AUDIO_CPP_MODEL_PRESETS[model] == ["cosette"]
    assert AUDIO_CPP_MODEL_DEFAULT_VOICES[model] == "cosette"
    package = MODEL_PACKAGES[model]
    assert "embeddings/cosette.safetensors" in package.files
    entry = server_config("cpu", [model])["models"][0]
    assert entry["load_options"] == entry["session_options"] == {"language": "french"}
    descriptors = request_parameters_for_model("pocket_tts", model)
    assert descriptors["temperature"]["default"] == 0.3
    payload = _build_audio_cpp_audio_payload("Bonjour.", {"model": model}, {})
    assert payload["options"]["temperature"] == 0.3
    custom = _build_audio_cpp_audio_payload(
        "Bonjour.", {"model": model, "audio_cpp_model_settings": {model: {"temperature": 0.5}}}, {}
    )
    assert custom["options"]["temperature"] == 0.5
    scalar = _build_audio_cpp_audio_payload(
        "Bonjour.", {"model": model, "audio_cpp_temperature": 0.45}, {}
    )
    assert scalar["temperature"] == 0.45
    assert "temperature" not in scalar["options"]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("voxtral_4b_tts_2603_q8_0", 1152),
        ("pocket_tts_french_q8_0", 180),
        ("chatterbox_q8_0", 180),
        ("chatterbox_turbo_q8_0", 180),
        ("moss_tts_nano_100m_q8_0", 230),
        ("moss_tts_local_v1_5_q8_0", 1843),
        ("cosyvoice3_q8_0", 540),
        ("higgs_audio_tts_4b_q8_0", 921),
    ],
)
def test_model_budget_has_headroom_and_matches_catalogue(model, expected):
    settings = {"service": "audio_cpp", "model": model}
    budget = audiobook_chunk_budget({}, settings)
    assert budget["target_chars"] == expected
    metadata = generation_limit_metadata(
        service="audio_cpp", model=model, family=package_metadata(model)["family"]
    )
    assert metadata["default_segment_characters"] == expected
    assert metadata["segmentation_owner"] == "pandrator"
    manual = audiobook_chunk_budget(
        {"audiobook_chunking": "manual", "max_sentence_length": 1234}, settings
    )
    assert manual["target_chars"] == 1234
    assert "target_words" not in manual


def test_configured_chunk_hint_reduces_automatic_policy_but_empty_map_is_authoritative():
    model = "voxtral_4b_tts_2603_q8_0"
    base = {"service": "audio_cpp", "model": model, "audio_cpp_options": {"text_chunk_size": 100}}
    assert audiobook_chunk_budget({}, base)["target_chars"] == 90
    base["audio_cpp_model_settings"] = {model: {}}
    assert audiobook_chunk_budget({}, base)["target_chars"] == 1152


@pytest.mark.parametrize("family", [
    "chatterbox", "chatterbox_turbo", "pocket_tts", "confucius4_tts",
    "irodori_tts", "voxtral_tts", "omnivoice", "lfm2_audio",
])
def test_native_request_keeps_the_complete_segment(family):
    text = "First sentence. Second sentence. " * 25
    has_mode = any(
        item["name"] == "text_chunk_mode"
        for item in family_metadata(family)["options"]["request"]
    )
    supplied = {"text_chunk_size": 100}
    if has_mode:
        supplied["text_chunk_mode"] = "endline"
    result = single_segment_options(family, text, supplied)
    assert result["text_chunk_size"] >= len(text)
    if has_mode:
        assert result["text_chunk_mode"] == "tag_aware"
        assert supplied["text_chunk_mode"] == "endline"
    assert supplied["text_chunk_size"] == 100


def test_voxtral_word_recommendation_is_applied_without_losing_text_or_paragraph_end():
    source = " ".join(["a"] * 300)
    segment = {"original_sentence": source, "paragraph": "yes", "chapter": "no"}
    parts = _limit_segment_words(segment, 240, "en")
    assert len(parts) > 1
    assert all(len(part["original_sentence"].split()) <= 240 for part in parts)
    assert " ".join(part["original_sentence"] for part in parts) == source
    assert parts[-1]["paragraph"] == "yes"
    assert all(part["paragraph"] == "no" for part in parts[:-1])
    budget = audiobook_chunk_budget({}, {"service": "audio_cpp", "model": "voxtral_4b_tts_2603_q8_0"})
    assert budget["target_words"] == 240


def test_voxtral_output_frames_are_not_reported_as_text_or_input_limits():
    result = generation_limit_metadata(
        service="audio_cpp", model="voxtral_4b_tts_2603_q8_0", family="voxtral_tts"
    )
    assert result["runtime_output_budget"] == {
        "value": 2048, "unit": "audio_frames", "frame_duration_ms": 80
    }
    assert result["runtime_chunk_characters"] == 300
    assert not {"input_characters", "input_tokens"} & result.keys()


def test_voxtral_rejects_unsupported_speed_and_legacy_instruction_options():
    model = "voxtral_4b_tts_2603_q8_0"
    with pytest.raises(ValueError, match="Voxtral speed is unsupported"):
        _build_audio_cpp_audio_payload("Hello.", {"model": model, "speed": 1.2}, {})
    with pytest.raises(ValueError, match="Unknown.*instruction"):
        _build_audio_cpp_audio_payload(
            "Hello.", {"model": model, "audio_cpp_options": {"instruction": "Whisper."}}, {}
        )


@pytest.mark.parametrize("family,model", [
    ("f5_tts", "habibi_unified"), ("outetts", "outetts_1_0_1b_q8_0"),
])
def test_models_without_native_split_override_remain_available_with_honest_metadata(family, model):
    text = "One sentence. Another sentence. " * 12
    supplied = {"temperature": 0.7}
    result = single_segment_options(family, text, supplied)
    assert supplied == {"temperature": 0.7}
    assert result["temperature"] == 0.7
    if family == "outetts":
        assert result["text_chunk_size"] >= len(text)
    else:
        assert result == supplied
    metadata = package_metadata(model)
    assert metadata["package_availability"]["status"] == "installable"
    assert any("Native sub-splits are not reported" in note
               for note in metadata["pandrator_features"]["capability_notes"])
    assert generation_limit_metadata(service="audio_cpp", model=model, family=family)[
        "single_segment_supported"
    ] is False


def test_soprano_splitter_is_disabled_at_the_managed_session_boundary():
    config = server_config("cpu", ["soprano_1_1_80m_q8_0"])
    assert config["models"][0]["session_options"] == {
        "text_chunk_size": SINGLE_SEGMENT_SESSION_CHARACTERS
    }
    assert narration_default("soprano_1_1_80m_q8_0") == 180
    assert single_segment_options("soprano_tts", "Two sentences. Still one segment.", {}) == {}


def narration_default(model):
    return audiobook_chunk_budget({}, {"service": "audio_cpp", "model": model})["target_chars"]


def test_dramabox_threshold_bypasses_native_long_form_without_changing_duration():
    options = {"duration_sec": 80, "duration_scale": 1.2, "audio_chunk_threshold_sec": 30}
    actual = single_segment_options("dramabox", "One sentence. " * 100, options)
    assert actual["audio_chunk_threshold_sec"] >= 80
    assert actual["audio_chunk_threshold_sec"] > 30
    assert actual["duration_sec"] == options["duration_sec"]
    assert actual["duration_scale"] == options["duration_scale"]
    assert options["audio_chunk_threshold_sec"] == 30


def test_korean_chatterbox_uses_its_shorter_output_budget():
    settings = {"service": "audio_cpp", "model": "chatterbox_q8_0"}
    assert audiobook_chunk_budget({}, settings, "ko")["target_chars"] == 63
    assert audiobook_chunk_budget({}, settings, "en")["target_chars"] == 180
    limits = generation_limit_metadata(
        service="audio_cpp", model="chatterbox_q8_0", family="chatterbox"
    )
    assert limits["runtime_output_budget"] == {
        "value": 384, "unit": "speech_tokens", "multilingual_token_duration_ms": 40,
    }
