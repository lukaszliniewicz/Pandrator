"""The standalone Manager projection must match the app's catalogue authority."""

from __future__ import annotations

import json
from pathlib import Path

from pandrator.logic.audio_cpp_catalogue import (
    catalogue_page,
    inventory,
    package_metadata,
)
from pandrator_manager.components.audiocpp import MODEL_PACKAGES
from pandrator_manager.components.catalog import _audio_cpp_models

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROJECTION_PATH = (
    REPOSITORY_ROOT / "pandrator_manager" / "audio_cpp_model_metadata.json"
)


def _projection() -> dict:
    return json.loads(PROJECTION_PATH.read_text(encoding="utf-8"))


def test_generated_projection_matches_every_canonical_inventory_package():
    projection = _projection()
    package_ids = sorted(package["id"] for package in inventory()["packages"])

    assert projection["schema_version"] == 1
    assert projection["runtime_version"] == inventory()["runtime_version"]
    assert list(projection["models"]) == package_ids
    for model_id in package_ids:
        assert projection["models"][model_id] == package_metadata(model_id)


def test_manager_projects_canonical_metadata_for_every_installable_model():
    projection = _projection()
    models = {model.id: model for model in _audio_cpp_models()}

    assert set(models) == set(MODEL_PACKAGES)
    for model_id, model in models.items():
        canonical = projection["models"][model_id]
        assert model.model_info == canonical
        assert model.capabilities == tuple(canonical["capabilities"])
        assert model.estimated_download_bytes == canonical["estimated_download_bytes"]
        assert model.license_name == canonical["license"]["name"]
        assert model.license_url == canonical["license"]["url"]


def test_runtime_capabilities_distinguish_qwen_variants():
    base = package_metadata("qwen3_tts_1_7b_base_q8_0")
    custom = package_metadata("qwen3_tts_1_7b_customvoice_q8_0")
    design = package_metadata("qwen3_tts_1_7b_voicedesign_q8_0")

    assert base["voice_mode"] == "cloning"
    assert {"voice_cloning"} <= set(base["capabilities"])
    assert not {"prebuilt_voices", "instructions", "emotion_control"} & set(
        base["capabilities"]
    )

    assert custom["voice_mode"] == "prebuilt"
    assert {"prebuilt_voices", "instructions", "emotion_control"} <= set(
        custom["capabilities"]
    )
    assert not {"voice_cloning", "voice_design"} & set(custom["capabilities"])

    assert design["voice_mode"] == "design"
    assert {"voice_design", "instructions", "emotion_control"} <= set(
        design["capabilities"]
    )
    assert not {"voice_cloning", "prebuilt_voices"} & set(design["capabilities"])


def test_chatterbox_turbo_does_not_gain_emotion_or_instruction_controls():
    standard = package_metadata("chatterbox_q8_0")
    turbo = package_metadata("chatterbox_turbo_q8_0")

    assert standard["voice_mode"] == "cloning"
    assert "voice_cloning" in standard["capabilities"]
    assert standard["upstream_features"]["voice_conversion"] is True
    assert "voice_conversion" in standard["capabilities"]
    assert turbo["voice_mode"] == "prebuilt"
    assert {"prebuilt_voices", "vocal_events"} <= set(turbo["capabilities"])
    assert not {
        "voice_cloning",
        "instructions",
        "emotion_control",
    } & set(turbo["capabilities"])
    assert turbo["upstream_features"]["vocal_events"] is True
    assert turbo["pandrator_features"]["emotion_control"] == "none"
    assert turbo["pandrator_features"]["instructions"] == "none"


def test_firered_base_and_instruct_keep_distinct_modes_and_controls():
    base = package_metadata("fireredtts3_base_q8_0")
    instruct = package_metadata("fireredtts3_instruct_q8_0")

    assert base["voice_mode"] == "cloning"
    assert base["capabilities"] == ["multilingual", "voice_cloning"]
    assert instruct["voice_mode"] == "optional_cloning"
    assert {
        "voice_cloning",
        "voice_design",
        "instructions",
        "emotion_control",
    } <= set(instruct["capabilities"])


def test_emotions_alias_matches_canonical_emotion_control_filter():
    canonical = catalogue_page(capability="emotion_control", limit=100)
    alias = catalogue_page(capability="emotions", limit=100)

    assert alias["total"] == canonical["total"]
    assert [item["id"] for item in alias["items"]] == [
        item["id"] for item in canonical["items"]
    ]
