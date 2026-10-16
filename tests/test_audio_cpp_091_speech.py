"""Offline speech contracts for the refreshed audio.cpp 0.9.2 catalogue."""

from __future__ import annotations

import pytest

from pandrator.logic.audio_cpp_catalogue import (
    family_metadata,
    inventory,
    package_metadata,
)
from pandrator.logic.audio_cpp_parameters import request_parameters_for_family
from pandrator.logic.tts_handler import _build_audio_cpp_audio_payload
from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_PRESETS
from pandrator_manager.audio_cpp_inventory import load_audio_cpp_packages
from pandrator_manager.audio_cpp_packages import AudioCppModelPackage


def _payload(model: str, **settings: object) -> dict:
    return _build_audio_cpp_audio_payload(
        "The quick brown fox.", {"model": model, **settings}, {}
    )


def test_refreshed_inventory_counts_and_new_speech_families_are_projected():
    source = inventory()
    assert source["runtime_version"] == "0.9.2"
    assert len(source["families"]) == 122
    assert len(source["packages"]) == 380

    families = {row["id"] for row in source["families"]}
    assert {"kugelaudio", "kitten_tts2", "lfm2_audio"} <= families
    for model_id in (
        "kitten_tts2_q8_0",
        "kugelaudio_0_open_q8_0",
        "lfm2_audio_1_5b_f16",
        "lfm2_audio_1_5b_jp_f16",
    ):
        metadata = package_metadata(model_id)
        assert metadata["verified_runtime"] == "0.9.2"
        assert metadata["pandrator_features"]["acoustic_verification"] == "not_tested"


def test_kugel_is_prebuilt_and_emits_a_named_preset_without_clone_fields():
    metadata = package_metadata("kugelaudio_0_open_q8_0")
    assert metadata["voice_mode"] == "prebuilt"
    assert metadata["reference_audio"] == "not_used"
    assert metadata["reference_text"] == "not_used"
    assert metadata["upstream_features"]["voice_cloning"] is False

    payload = _payload(
        "kugelaudio_0_open_q8_0",
        language="en",
        speaker="english_female",
        audio_cpp_voice_ref={"sample_id": "local-sample"},
        audio_cpp_reference_text="A reviewed transcript.",
    )
    assert payload["voice"] == "english_female"
    assert payload["language"] == "en"
    assert "voice_ref" not in payload
    assert "reference_text" not in payload


def test_kitten_presets_and_linked_clones_have_distinct_payloads():
    model = "kitten_tts2_q8_0"
    preset = _payload(
        model,
        speaker="Bella",
        audio_cpp_model_settings={model: {"preset": "expressive"}},
    )
    assert preset["voice"] == "Bella"
    assert preset["options"]["preset"] == "expressive"
    assert "voice_ref" not in preset

    linked_ref = {"type": "path", "path": "/tmp/reviewed-reference.wav"}
    clone = _payload(
        model,
        speaker="Bella",
        audio_cpp_voice_ref=linked_ref,
        audio_cpp_reference_text="The matching reviewed transcript.",
    )
    assert clone["reference_text"] == "The matching reviewed transcript."
    assert clone["voice_ref"] == linked_ref
    assert "voice" not in clone
    assert "cached_voice_id" not in clone["voice_ref"]

    with pytest.raises(
        ValueError,
        match="^KittenTTS2 voice cloning requires a matching reference transcript\\.$",
    ):
        _payload(
            model,
            audio_cpp_voice_ref=linked_ref,
            audio_cpp_reference_text=" \t ",
        )


_LFM_ENGLISH_MODELS = (
    "lfm2_audio_1_5b_f16",
    "lfm2_audio_1_5b_q4_0",
    "lfm2_audio_1_5b_q8_0",
)
_LFM_JAPANESE_MODELS = (
    "lfm2_audio_1_5b_jp_f16",
    "lfm2_audio_1_5b_jp_f32",
    "lfm2_audio_1_5b_jp_q4_0",
    "lfm2_audio_1_5b_jp_q8_0",
)
_LFM_ENGLISH_VOICES = ("us_male", "us_female", "uk_male", "uk_female")


@pytest.mark.parametrize("model", _LFM_ENGLISH_MODELS)
@pytest.mark.parametrize("voice", _LFM_ENGLISH_VOICES)
def test_lfm_english_checkpoints_keep_top_level_language_and_voice(model, voice):
    assert package_metadata(model)["supported_languages"] == ["en"]
    assert AUDIO_CPP_MODEL_PRESETS[model] == list(_LFM_ENGLISH_VOICES)
    payload = _payload(model, language="en", speaker=voice)
    assert payload["language"] == "en"
    assert payload["voice"] == voice


@pytest.mark.parametrize("model", _LFM_JAPANESE_MODELS)
def test_lfm_japanese_checkpoints_use_their_builtin_voice(model):
    assert package_metadata(model)["supported_languages"] == ["ja"]
    assert AUDIO_CPP_MODEL_PRESETS[model] == []
    payload = _payload(model, language="ja")
    assert payload["language"] == "ja"
    assert "voice" not in payload

    for field in ("speaker", "voice"):
        with pytest.raises(
            ValueError,
            match="^The Japanese LFM checkpoint uses its built-in voice; clear the selected voice\\.$",
        ):
            _payload(model, language="ja", **{field: "narrator"})

    with pytest.raises(ValueError, match="does not support language"):
        _payload(model, language="en")


def test_lfm_english_checkpoint_rejects_japanese_preflight_language():
    with pytest.raises(ValueError, match="does not support language"):
        _payload("lfm2_audio_1_5b_f16", language="ja")


def test_native_parameter_descriptors_retain_documented_controls():
    kugel_spec = family_metadata("kugelaudio")
    kugel_native_names = {
        item["name"] for item in kugel_spec["options"]["request"]
    }
    kugel_parameters = request_parameters_for_family("kugelaudio")
    assert "text_chunk_mode" in kugel_native_names
    assert kugel_parameters["text_chunk_mode"]["type"] == "string"
    assert kugel_parameters["text_chunk_mode"]["enum"] == [
        "default",
        "tag_aware",
        "japanese",
        "endline",
    ]

    kitten_spec = family_metadata("kitten_tts2")
    kitten_native_names = {
        item["name"] for item in kitten_spec["options"]["request"]
    }
    kitten_parameters = request_parameters_for_family("kitten_tts2")
    assert "preset" in kitten_native_names
    assert kitten_parameters["preset"]["type"] == "string"
    assert kitten_parameters["preset"]["enum"] == ["stable", "expressive"]

    supertonic_parameters = request_parameters_for_family("supertonic")
    assert supertonic_parameters["seed"]["default"] == 1234
    assert supertonic_parameters["num_inference_steps"]["default"] == 8


def test_lfm_manager_projection_selects_the_backbone_and_keeps_sibling_files():
    inventory_lfm_ids = {
        row["id"]
        for row in inventory()["packages"]
        if row["family"] == "lfm2_audio"
    }
    assert inventory_lfm_ids == {*_LFM_ENGLISH_MODELS, *_LFM_JAPANESE_MODELS}

    packages = {package.id: package for package in load_audio_cpp_packages()}
    assert inventory_lfm_ids <= packages.keys()
    for model in inventory_lfm_ids:
        package = packages[model]
        assert package.config_path.endswith(package.files[0])
        assert package.files[0].casefold().endswith(".gguf")
        assert len(package.files) > 1
        assert package.precision


def test_lfm_package_constructor_preserves_selected_quantization_with_siblings():
    selected_backbone = "LFM2.5-Audio-1.5B-Q4_0.gguf"
    package = AudioCppModelPackage(
        id="lfm2_audio_1_5b_q4_0_fixture",
        family="lfm2_audio",
        target_directory="LFM2.5-Audio-1.5B-GGUF",
        files=(
            selected_backbone,
            "mmproj-LFM2.5-Audio-1.5B-Q4_0.gguf",
            "vocoder-LFM2.5-Audio-1.5B-Q4_0.gguf",
            "tokenizer-LFM2.5-Audio-1.5B-Q4_0.gguf",
        ),
        sha256=("0" * 64,) * 4,
        task="tts",
        precision="q4_0",
    )
    assert package.config_path == (
        "models/LFM2.5-Audio-1.5B-GGUF/LFM2.5-Audio-1.5B-Q4_0.gguf"
    )
    assert package.precision == "q4_0"
    assert package.files[0] == selected_backbone
    assert len(package.files) == 4
