"""Exact package roles and task variants in the pinned audio.cpp inventory."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from pandrator.logic.audio_cpp_catalogue import (
    catalogue_page,
    inventory,
    package_metadata,
    speech_model_catalog,
)
from pandrator_manager.components.audiocpp import (
    MODEL_PACKAGES,
    SUPPORTED_MODEL_IDS,
    server_config,
)
from scripts.snapshot_audio_cpp_catalogue import flatten_packages

COMPONENT_IDS = {"auk_qwen_bf16", "auk_qwen_q8_0", "auk_vae_f32", "vocos_mel_24khz"}
TASK_VARIANTS = {
    "personaplex_7b_v1_q4_k": ("conversion", ["s2s"], "s2s"),
    "personaplex_7b_v1_q8_0": ("conversion", ["s2s"], "s2s"),
    "dots_tts_edit_bf16": ("tools", ["edit"], "audio_edit"),
    "dots_tts_edit_q8_0": ("tools", ["edit"], "audio_edit"),
}
SPEECH_TASK_VARIANTS = {
    "maya1_orig": ["tts", "design"],
    "maya1_q8_0": ["tts", "design"],
    "irodori_tts_600m_v3_voicedesign_f16": ["tts", "clone", "design"],
    "irodori_tts_600m_v3_voicedesign_q8_0": ["tts", "clone", "design"],
    "irodori_tts_v4_small_f16": ["tts", "clone", "design"],
    "irodori_tts_v4_small_q8_0": ["tts", "clone", "design"],
    "irodori_tts_500m_v3_f16": ["tts", "clone"],
    "irodori_tts_500m_v3_q8_0": ["tts", "clone"],
    "irodori_tts_v4_1_anime_q8_0": ["tts"],
    "moss_voicegen_bf16_codec_f16_decode": ["design"],
    "moss_tts_v15_bf16_codec_f16": ["tts", "clone"],
    "moss_tts_v15_q4_k_codec_f16": ["tts", "clone"],
    "moss_tts_v15_q8_0_codec_f16": ["tts", "clone"],
}


def test_components_keep_pinned_downloads_but_not_model_choices():
    raw = {row["id"]: row for row in inventory()["packages"]}
    ordinary_ids = {row["id"] for row in catalogue_page(limit=100)["items"]}
    # The full default catalogue is paged; search each component as well.
    speech_ids = {row["id"] for row in speech_model_catalog()}
    for package_id in COMPONENT_IDS:
        package = raw[package_id]
        assert package["entry_role"] == "component"
        assert package["category"] == "component"
        assert package["tasks"] == []
        assert package["weight_manifest"]["revision"]
        assert package["weight_manifest"]["files"]
        assert all(file["sha256"] for file in package["weight_manifest"]["files"])
        assert package_id in MODEL_PACKAGES
        assert MODEL_PACKAGES[package_id].role == "component"
        assert package_id not in SUPPORTED_MODEL_IDS
        assert package_id not in ordinary_ids
        assert package_id not in speech_ids
        assert catalogue_page(query=package_id)["total"] == 0
        metadata = package_metadata(package_id)
        assert metadata["entry_role"] == "component"
        assert metadata["voice_mode"] == "none"
        assert metadata["capabilities"] == []
        assert metadata["language_support_by_operation"] == {}
        assert metadata["language_support"]["languages"] == []
        with pytest.raises(ValueError, match="component"):
            server_config("cpu", [package_id])


def test_generator_and_real_task_variants_remain_distinct():
    for package_id in ("auk_base_q8_0", "auk_flash_q8_0"):
        metadata = package_metadata(package_id)
        assert metadata["entry_role"] == "model"
        assert metadata["voice_mode"] != "none"
        assert package_id in SUPPORTED_MODEL_IDS
        assert metadata["package_availability"]["status"] == "manual_setup"
        assert "Qwen2.5-Omni" in metadata["package_availability"]["reason"]
        assert metadata["pandrator_features"]["dependency_binding"] == "manual"
    for package_id, (category, tasks, operation) in TASK_VARIANTS.items():
        metadata = package_metadata(package_id)
        assert metadata["entry_role"] == "model"
        assert metadata["category"] == category
        assert metadata["tasks"] == tasks
        assert metadata["voice_mode"] == "none"
        assert set(metadata["language_support_by_operation"]) == {operation}
    assert package_metadata("qwen3_asr_0_6b_q8_0")["entry_role"] == "model"
    assert package_metadata("vevo2_q8_0")["entry_role"] == "model"


def test_unverified_irodori_variant_remains_visible_without_speech_admission():
    model_id = "irodori_tts_v4_1_anime_q8_0"
    metadata = package_metadata(model_id)
    assert catalogue_page(query=model_id)["total"] == 1
    assert metadata["pandrator_features"]["speech_generation"] == "unverified"
    assert metadata["package_availability"]["status"] == "catalogued_only"
    assert metadata["request_parameters"] == {}
    assert model_id not in {row["id"] for row in speech_model_catalog()}


@pytest.mark.parametrize(
    ("package_id", "voice_mode", "reference_audio", "clone", "design"),
    [
        ("maya1_orig", "design", "not_used", False, True),
        ("maya1_q8_0", "design", "not_used", False, True),
        ("irodori_tts_600m_v3_voicedesign_f16", "optional_cloning", "optional", True, True),
        ("irodori_tts_600m_v3_voicedesign_q8_0", "optional_cloning", "optional", True, True),
        ("irodori_tts_v4_small_f16", "optional_cloning", "optional", True, True),
        ("irodori_tts_v4_small_q8_0", "optional_cloning", "optional", True, True),
        ("irodori_tts_500m_v3_f16", "optional_cloning", "optional", True, False),
        ("irodori_tts_500m_v3_q8_0", "optional_cloning", "optional", True, False),
        ("irodori_tts_v4_1_anime_q8_0", "unknown", "unverified", False, False),
        ("moss_voicegen_bf16_codec_f16_decode", "design", "not_used", False, True),
        ("moss_tts_v15_bf16_codec_f16", "optional_cloning", "optional", True, False),
        ("moss_tts_v15_q4_k_codec_f16", "optional_cloning", "optional", True, False),
        ("moss_tts_v15_q8_0_codec_f16", "optional_cloning", "optional", True, False),
    ],
)
def test_exact_voice_modes(package_id, voice_mode, reference_audio, clone, design):
    metadata = package_metadata(package_id)
    assert metadata["tasks"] == SPEECH_TASK_VARIANTS[package_id]
    assert metadata["voice_mode"] == voice_mode
    assert metadata["reference_audio"] == reference_audio
    assert metadata["upstream_features"]["voice_cloning"] is clone
    assert metadata["upstream_features"]["voice_design"] is design


def test_mirror_and_snapshot_projection_preserve_role_annotations():
    app = Path("pandrator/logic/audio_cpp_inventory.json")
    manager = Path("pandrator_manager/audio_cpp_inventory.json")
    assert app.read_bytes() == manager.read_bytes()
    fixture = {
        "family": "auk",
        "package_defaults": {"download": {"kind": "huggingface_snapshot", "repo": "owner/repo"}},
        "packages": [
            {"id": package_id, "target_directory": package_id, "files": ["weights.gguf"]}
            for package_id in [
                *sorted(COMPONENT_IDS), *sorted(TASK_VARIANTS),
                *(ident for ident in sorted(SPEECH_TASK_VARIANTS) if not ident.startswith("moss_tts_v15_")),
            ]
        ],
    }
    output = {row["id"]: row for row in flatten_packages([fixture])}
    for package_id in COMPONENT_IDS:
        assert {key: output[package_id][key] for key in ("entry_role", "category", "tasks")} == {
            "entry_role": "component", "category": "component", "tasks": [],
        }
    for package_id, (category, tasks, _) in TASK_VARIANTS.items():
        assert output[package_id]["category"] == category
        assert output[package_id]["tasks"] == tasks
        assert output[package_id].get("entry_role", "model") == "model"
    for package_id, tasks in SPEECH_TASK_VARIANTS.items():
        if package_id.startswith("moss_tts_v15_"):
            continue
        assert output[package_id]["tasks"] == tasks


def test_new_package_pickle_round_trip_keeps_role():
    package = MODEL_PACKAGES["auk_qwen_bf16"]
    loaded = pickle.loads(pickle.dumps(package, protocol=4))
    assert loaded == package
    assert loaded.role == "component"
