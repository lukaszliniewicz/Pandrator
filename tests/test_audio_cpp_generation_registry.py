"""Pinned generation registry behavior without network access."""

from __future__ import annotations

import copy

import pytest

from pandrator.logic import audio_cpp_generation_models as registry

SUPPORTED_FAMILIES = {
    "stable_audio",
    "controlfoley",
    "midashenglm_gen",
    "ace_step",
    "heartmula",
    "minimax_music3",
    "minimax_h3",
    "yue2",
}
EXPECTED_MODEL_IDS = {
    "stable_audio_3_small_sfx_q8_0",
    "stable_audio_3_small_sfx_f16",
    "stable_audio_3_small_music_f16",
    "stable_audio_3_small_music_q8_0",
    "stable_audio_3_medium_f16",
    "stable_audio_3_medium_q8_0",
    "controlfoley_large_44k_f32",
    "controlfoley_large_44k_q8_0",
    "midashenglm_gen_f32",
    "midashenglm_gen_q8_0",
    "ace_step_base_bf16",
    "ace_step_base_q8_0",
    "ace_step_turbo_bf16",
    "ace_step_turbo_q8_0",
    "ace_step_xl_sft_bf16",
    "ace_step_xl_sft_q8dit",
    "ace_step_xl_turbo_bf16",
    "ace_step_xl_turbo_q8dit",
    "heartmula_f16",
    "heartmula_q8_0",
    "minimax_music3_bf16",
    "minimax_music3_q4_0",
    "minimax_music3_q8_0",
    "minimax_h3_q4_k",
    "minimax_h3_q4_k_int8_dit",
    "yue2_main_bf16",
    "yue2_main_q4_0",
    "yue2_main_q8_0",
}


def test_registry_exposes_only_verified_allowlisted_generation_packages():
    models = registry.all_models()
    assert len(models) == 28
    assert set(registry.model_ids()) == EXPECTED_MODEL_IDS
    assert {model.family for model in models} == SUPPORTED_FAMILIES
    assert models[0].id == registry.DEFAULT_MODEL_ID
    assert all(model.files and model.revision for model in models)
    assert all(len(model.revision) == 40 for model in models)
    assert all(
        len(item.sha256) == 64 and item.size_bytes > 0
        for model in models
        for item in model.files
    )
    assert "stable_audio_3_small_music_q8_0" in registry.model_ids()
    assert "controlfoley_large_44k_q8_0" in registry.model_ids()
    assert not any("safetensors" in model.id for model in models)
    assert not any(model.family in {"liveavatar", "vevo2"} for model in models)


def test_public_descriptors_are_bounded_and_include_variant_capabilities():
    descriptors = registry.model_descriptors()
    assert len(descriptors) == 28
    assert set(descriptors[0]) == {
        "id",
        "label",
        "family",
        "cached",
        "kinds",
        "description",
        "source_url",
        "backends",
        "duration",
        "supports_lyrics",
        "requires_lyrics",
        "seed_maximum",
        "parameters",
        "download_bytes",
        "available",
        "reason",
    }
    for descriptor in descriptors:
        model = registry.get_model(descriptor["id"])
        assert descriptor["backends"] == list(model.backends)
        assert descriptor["available"] is True
        assert descriptor["reason"] is None
        assert descriptor["cached"] is False
        assert descriptor["download_bytes"] > 0
        assert descriptor["duration"]["minimum"] <= descriptor["duration"]["default"]
        assert descriptor["duration"]["default"] <= descriptor["duration"]["maximum"]

    turbo = registry.get_model("ace_step_turbo_q8_0")
    base = registry.get_model("ace_step_base_q8_0")
    assert "guidance_scale" not in turbo.parameter_descriptors()
    assert "guidance_scale" in base.parameter_descriptors()
    assert registry.get_model("minimax_music3_q4_0").requires_lyrics is True
    assert registry.get_model("minimax_music3_q4_0").supports_lyrics is True


def test_verified_yue2_and_h3_manifests_are_complete_and_variant_exact():
    assert registry.unavailable_models() == []
    for model_id in ("yue2_main_bf16", "yue2_main_q4_0", "yue2_main_q8_0"):
        model = registry.get_model(model_id)
        assert model.family == "yue2"
        assert model.is_directory_model
        assert model.kinds == ("music",)
        assert model.supports_lyrics and not model.requires_lyrics
        assert len(model.files) == 6
        assert "yue2-vae-f16.gguf" in {item.path for item in model.files}
        assert model.duration_minimum == 8
        assert model.duration_maximum == 360
        assert model.duration_default == 30
        assert "Maximum length budget" in model.descriptor()["duration"]["description"]

    normal = registry.get_model("minimax_h3_q4_k")
    int8 = registry.get_model("minimax_h3_q4_k_int8_dit")
    assert normal.family == int8.family == "minimax_h3"
    assert not normal.is_directory_model and not int8.is_directory_model
    assert normal.model_path_asset.path == "dit.gguf"
    assert int8.model_path_asset.path == "dit_int8.gguf"
    assert len(normal.files) == len(int8.files) == 12
    assert normal.kinds == int8.kinds == ("sound", "music")
    assert normal.backends == ("cpu", "vulkan", "cuda")
    assert int8.backends == ("cuda",)
    assert "Joint audio/video latents" in normal.descriptor()["notes"]
    assert "experimental" in int8.descriptor()["notes"]
    assert "excludes the EU, UK, US and South Korea" in normal.descriptor()["notes"]
    assert "excludes the EU, UK, US and South Korea" in int8.descriptor()["notes"]
    assert normal.descriptor()["duration"]["description"] == (
        "Approximate duration, rounded up to the native frame grid."
    )


@pytest.mark.parametrize(
    ("model_id", "mutate"),
    (
        (
            "yue2_main_q8_0",
            lambda row: row["weight_manifest"]["files"].pop(),
        ),
        (
            "yue2_main_q8_0",
            lambda row: row["availability"].update(verified=False),
        ),
        (
            "minimax_h3_q4_k_int8_dit",
            lambda row: row["weight_manifest"]["files"][-3].update(path="dit.gguf"),
        ),
    ),
)
def test_new_family_rows_reject_partial_unverified_or_wrong_variant_layout(model_id, mutate):
    inventory = registry._load_inventory()
    row = next(item for item in inventory["packages"] if item["id"] == model_id)
    mutate(row)
    with pytest.raises(ValueError):
        registry.build_registry(inventory)


def test_minimax_music3_variant_manifests_pin_every_directory_component():
    for model_id in (
        "minimax_music3_bf16",
        "minimax_music3_q4_0",
        "minimax_music3_q8_0",
    ):
        model = registry.get_model(model_id)
        paths = {asset.path for asset in model.files}
        assert model.is_directory_model
        assert len(paths) == 13
        assert {"config.json", "tokenizer/tokenizer.json"} <= paths
        assert all(asset.sha256 and asset.size_bytes > 0 for asset in model.files)
        assert any(path.startswith("language_model_") for path in paths)
        assert any(path.startswith("rvq_depth_decoder_") for path in paths)
        assert any(path.startswith("transformer_") for path in paths)
    q4_paths = {asset.path for asset in registry.get_model("minimax_music3_q4_0").files}
    assert "rvq_depth_decoder_q8_0.gguf" in q4_paths


def _valid_inventory_row(model_id: str = "stable_audio_3_small_music_q8_0"):
    inventory = registry._load_inventory()
    return copy.deepcopy(next(row for row in inventory["packages"] if row["id"] == model_id))


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda row: row["weight_manifest"].update(revision="main"), "immutable 40-hex"),
        (
            lambda row: row["weight_manifest"]["files"][0].update(sha256=None),
            "SHA-256",
        ),
        (
            lambda row: row["weight_manifest"]["files"][0].update(path="../escape.gguf"),
            "safe relative POSIX path",
        ),
        (lambda row: row["download"].update(gated=True), "Gated packages"),
        (
            lambda row: row["availability"].update(missing_files=["required.json"]),
            "reports missing files",
        ),
        (
            lambda row: row["weight_manifest"].update(repository="attacker/repo"),
            "outside the generation source allowlist",
        ),
        (lambda row: row["availability"].update(verified=False), "not verified"),
    ),
)
def test_manifest_rejects_unpinned_or_unsafe_eligible_rows(mutate, message):
    row = _valid_inventory_row()
    mutate(row)
    with pytest.raises(ValueError, match=message):
        registry.validate_weight_manifest(row)


def test_manifest_uses_weight_revision_not_mutable_download_revision():
    row = _valid_inventory_row()
    row["download"]["revision"] = "main"
    _repository, revision, _files = registry.validate_weight_manifest(row)
    assert revision == row["weight_manifest"]["revision"]


def test_yue2_vae_is_never_a_standalone_generation_model():
    assert "yue2_vae_f16" not in registry.model_ids()
    assert "yue2_vae_f32" not in registry.model_ids()


@pytest.mark.parametrize("model_id", ("yue2_main_q4_0", "minimax_h3_q4_k"))
def test_manifest_rejects_swapped_remote_files_even_with_same_path_sets(model_id):
    inventory = registry._load_inventory()
    row = next(item for item in inventory["packages"] if item["id"] == model_id)
    first, second = row["weight_manifest"]["files"][:2]
    first["source_path"], second["source_path"] = second["source_path"], first["source_path"]
    with pytest.raises(ValueError, match="exact.*layout"):
        registry.build_registry(inventory)


@pytest.mark.parametrize(
    "path",
    ("../escape.gguf", "one/../escape.gguf", "folder:stream/model.gguf", "bad\x01name"),
)
def test_manifest_paths_reject_escape_ads_and_control_characters(path):
    with pytest.raises(ValueError, match="safe relative POSIX path"):
        registry._safe_relative_path(path, "test.path")
