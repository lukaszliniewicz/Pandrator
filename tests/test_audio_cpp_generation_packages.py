from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pandrator.logic.audio_cpp_catalogue import speech_model_catalog
from pandrator_manager.audio_cpp_inventory import (
    GENERATION_FAMILIES,
    load_audio_cpp_inventory,
    load_audio_cpp_packages,
    validated_inventory_packages,
)
from pandrator_manager.components.audiocpp import MODEL_PACKAGES, SUPPORTED_MODEL_IDS, server_config

EXPECTED_GENERATION_IDS = {
    "stable_audio": {
        "stable_audio_3_medium_f16",
        "stable_audio_3_medium_q8_0",
        "stable_audio_3_small_music_f16",
        "stable_audio_3_small_music_q8_0",
        "stable_audio_3_small_sfx_f16",
        "stable_audio_3_small_sfx_q8_0",
    },
    "controlfoley": {
        "controlfoley_large_44k_f32",
        "controlfoley_large_44k_q8_0",
    },
    "midashenglm_gen": {
        "midashenglm_gen_f32",
        "midashenglm_gen_q8_0",
    },
    "ace_step": {
        "ace_step_base_bf16",
        "ace_step_base_q8_0",
        "ace_step_turbo_bf16",
        "ace_step_turbo_q8_0",
        "ace_step_xl_sft_bf16",
        "ace_step_xl_sft_q8dit",
        "ace_step_xl_turbo_bf16",
        "ace_step_xl_turbo_q8dit",
    },
    "heartmula": {"heartmula_f16", "heartmula_q8_0"},
    "minimax_music3": {
        "minimax_music3_bf16",
        "minimax_music3_q4_0",
        "minimax_music3_q8_0",
    },
    "yue2": {"yue2_main_bf16", "yue2_main_q4_0", "yue2_main_q8_0"},
    "minimax_h3": {"minimax_h3_q4_k", "minimax_h3_q4_k_int8_dit"},
}

MINIMAX_MUSIC3_COMMON_HASHES = {
    "config.json": "1ffcff67d916d5519d1ef9a36853232ee6bcb7e14a936623c4e85559f5dce106",
    "config/language_model.json": "34ded58fcd5e8181557f1417e76af9f3e95109a5ce113a4771fd16f8625b53c0",
    "config/rvq_depth_decoder.json": "b38c7f72d4b2fa8667442071f9e4a63ca5a7b5d0859e5e64ee7f79c4bc64520c",
    "config/condition_encoder.json": "1cdea3f36506719128ae4a29cdfff74b1134e607eb4a504ce6d810568fac8263",
    "config/transformer.json": "f60856be934127a8b8223510aff2e38a9efd8bbdbc230c3d3339b96e2cb12f00",
    "config/vocoder.json": "4161afd07ba7d5b949e29c8a7a005592259fba73e9110b6ece9dfd38d3489eac",
    "tokenizer/tokenizer.json": "b1537fa9e59a537276ecbc2e12d0438edff635a1f4a4948e679774b5feb3e610",
    "tokenizer/tokenizer_config.json": "1689852cc9c45010de040c8302a8acdc0d2c4c6c740dd7e9dd0a8c704e16eada",
    "condition_encoder.gguf": "ae4be8b6154645b0717d1928f5c195359bdee8fd025099c49954ed302257cd20",
    "vocoder.gguf": "5d40e6d52349d50d2aeaaaf6ba934afbf490e4cbc1ba5bb715c3133ea45cdca4",
}

MINIMAX_MUSIC3_GGUF_HASHES = {
    "minimax_music3_bf16": {
        "language_model_bf16.gguf": "61692fa58e77f83dd5014d6bb70a6ebbba95e79b3c3619c495b2c4949c0aa6f2",
        "rvq_depth_decoder_bf16.gguf": "986ca48662fcf64dd249ee8bc07d479724d195fa6cf1ace72b582140f1d4db7b",
        "transformer_bf16.gguf": "bd6f9cd595684f782650a5bed3e3f3715b949c7d78762dab036ff7b4e8ac4576",
    },
    "minimax_music3_q4_0": {
        "language_model_q4_0.gguf": "6f621dd636320403c03e9f755b3e2047f5754d055e0fcc6c0c444ae52ffbfa90",
        "rvq_depth_decoder_q8_0.gguf": "d5b7495fb1a7845abfeab13df829510de442fd4ea0fd55467fe9895fe8ec5db6",
        "transformer_q4_0.gguf": "18d3b2461a2988f59e197ccf05dc3931663099290b42443baa2a30e4fd15e506",
    },
    "minimax_music3_q8_0": {
        "language_model_q8_0.gguf": "cf8a9b01bb0ea2e930f1df023e524d1ec2de595553775bcd3f9c5e5187419e72",
        "rvq_depth_decoder_q8_0.gguf": "d5b7495fb1a7845abfeab13df829510de442fd4ea0fd55467fe9895fe8ec5db6",
        "transformer_q8_0.gguf": "40ed8a14884e9ac1b40625edd0dc14138c3d3d62803e2b5903f008dec60e61ac",
    },
}


def test_generation_allowlist_projects_all_current_pinned_packages_as_offline_gen():
    packages = load_audio_cpp_packages()
    by_family = {
        family: {
            package.id: package
            for package in packages
            if package.family == family and package.role == "model"
        }
        for family in GENERATION_FAMILIES
    }

    assert {family: set(rows) for family, rows in by_family.items()} == EXPECTED_GENERATION_IDS
    generation = [package for rows in by_family.values() for package in rows.values()]
    assert len(generation) == 28
    assert all(package.task == "gen" for package in generation)
    assert all(package.mode == "offline" and package.role == "model" for package in generation)
    assert all(len(package.sha256) == len(package.files) == len(package.download_files) for package in generation)
    assert all(len(package.revision) == 40 and package.repository for package in generation)
    assert not any("liveavatar" in package.id.casefold() for package in generation)


def test_yue2_vae_components_are_installable_but_never_server_models():
    component_ids = {"yue2_vae_f16", "yue2_vae_f32"}
    packages = {package.id: package for package in load_audio_cpp_packages()}

    assert component_ids <= packages.keys()
    assert all(packages[package_id].role == "component" for package_id in component_ids)
    assert component_ids.isdisjoint(SUPPORTED_MODEL_IDS)
    assert component_ids <= MODEL_PACKAGES.keys()
    for package_id in component_ids:
        with pytest.raises(ValueError, match="is a component"):
            server_config("cpu", [package_id])


def test_non_generation_catalogue_packages_remain_unmanaged():
    excluded_ids = {
        "vevo2_f16",
        "vevo2_orig",
        "vevo2_q8_0",
        "liveavatar_nvfp4_lora",
    }
    assert excluded_ids.isdisjoint(MODEL_PACKAGES)


def test_generation_options_and_unambiguous_config_paths():
    expected_ace_paths = {
        "ace_step_base_bf16": "acestep-v15-base",
        "ace_step_base_q8_0": "acestep-v15-base",
        "ace_step_turbo_bf16": "acestep-v15-turbo",
        "ace_step_turbo_q8_0": "acestep-v15-turbo",
        "ace_step_xl_sft_bf16": "acestep-v15-xl-sft",
        "ace_step_xl_sft_q8dit": "acestep-v15-xl-sft",
        "ace_step_xl_turbo_bf16": "acestep-v15-xl-turbo",
        "ace_step_xl_turbo_q8dit": "acestep-v15-xl-turbo",
    }
    for package_id, model_path in expected_ace_paths.items():
        package = MODEL_PACKAGES[package_id]
        assert package.load_options == {
            "ace_step.dit_model_path": model_path
        }
        assert package.session_options is None

    for package_id in EXPECTED_GENERATION_IDS["minimax_music3"]:
        package = MODEL_PACKAGES[package_id]
        language_model, rvq_depth_decoder, transformer = {
            "minimax_music3_bf16": (
                "language_model_bf16.gguf",
                "rvq_depth_decoder_bf16.gguf",
                "transformer_bf16.gguf",
            ),
            "minimax_music3_q4_0": (
                "language_model_q4_0.gguf",
                "rvq_depth_decoder_q8_0.gguf",
                "transformer_q4_0.gguf",
            ),
            "minimax_music3_q8_0": (
                "language_model_q8_0.gguf",
                "rvq_depth_decoder_q8_0.gguf",
                "transformer_q8_0.gguf",
            ),
        }[package_id]
        assert package.config_path == "models/MiniMax-Music3-GGUF"
        assert package.load_options is None
        assert package.session_options == {
            "language_model_gguf": language_model,
            "rvq_depth_decoder_gguf": rvq_depth_decoder,
            "flow_transformer_gguf": transformer,
        }

    for package_id in EXPECTED_GENERATION_IDS["yue2"]:
        package = MODEL_PACKAGES[package_id]
        variant = package_id.removeprefix("yue2_main_")
        assert package.config_path == "models/Yue2-3B-GGUF"
        assert package.load_options is None
        assert package.session_options == {
            "model_gguf": f"yue2-3b-{variant}.gguf",
            "vae_gguf": "yue2-vae-f16.gguf",
        }

    expected_h3_config_files = {
        "minimax_h3_q4_k": "dit.gguf",
        "minimax_h3_q4_k_int8_dit": "dit_int8.gguf",
    }
    for package_id, config_file in expected_h3_config_files.items():
        package = MODEL_PACKAGES[package_id]
        assert package.config_path == f"models/MiniMax-H3-Q4-GGUF/{config_file}"
        assert package.load_options is None
        assert package.session_options is None

    for family in ("stable_audio", "controlfoley", "midashenglm_gen", "heartmula"):
        assert all(
            MODEL_PACKAGES[package_id].load_options is None
            and MODEL_PACKAGES[package_id].session_options is None
            for package_id in EXPECTED_GENERATION_IDS[family]
        )
    for family in GENERATION_FAMILIES - {"minimax_music3", "yue2", "minimax_h3"}:
        for package_id in EXPECTED_GENERATION_IDS[family]:
            package = MODEL_PACKAGES[package_id]
            gguf_files = [path for path in package.files if path.casefold().endswith(".gguf")]
            assert len(gguf_files) == 1
            assert package.config_path == f"models/{package.target_directory}/{gguf_files[0]}"


@pytest.mark.parametrize(
    ("package_id", "gguf_hashes"),
    MINIMAX_MUSIC3_GGUF_HASHES.items(),
)
def test_minimax_music3_retains_exact_thirteen_file_manifest_and_pins(package_id, gguf_hashes):
    package = MODEL_PACKAGES[package_id]
    expected_hashes = {**MINIMAX_MUSIC3_COMMON_HASHES, **gguf_hashes}

    assert package.repository == "audio-cpp/MiniMax-Music3-GGUF"
    assert package.revision == "46fe77f8bb395ccdbe797d784225f7b84866e7ce"
    assert len(package.files) == len(package.download_files) == len(package.sha256) == 13
    assert set(package.files) == set(expected_hashes)
    assert dict(zip(package.files, package.sha256, strict=True)) == expected_hashes
    assert set(package.download_files) == set(expected_hashes)


def test_yue2_and_minimax_h3_retain_exact_pinned_manifest_layouts():
    packages = {package.id: package for package in load_audio_cpp_packages()}
    yue2_fixed_files = {
        "sidecars/yue2-model-config.json",
        "sidecars/yue2-generation-config.json",
        "sidecars/yue2-qwen.tiktoken",
        "sidecars/yue2-vae-config.json",
        "yue2-vae-f16.gguf",
    }
    h3_fixed_files = {
        "configuration.json",
        "FL2VA/processor/chat_template.json",
        "FL2VA/processor/merges.txt",
        "FL2VA/processor/preprocessor_config.json",
        "FL2VA/processor/tokenizer.json",
        "FL2VA/processor/tokenizer_config.json",
        "FL2VA/processor/video_preprocessor_config.json",
        "FL2VA/processor/vocab.json",
        "text_encoder_q4_k.gguf",
        "audio_vae_folded_f16.gguf",
        "video_vae.gguf",
    }
    expected = {
        **{
            package_id: (
                "cca74d8980c00f30ab8d52248a367c01ad633ec2",
                yue2_fixed_files | {f"yue2-3b-{package_id.removeprefix('yue2_main_')}.gguf"},
            )
            for package_id in EXPECTED_GENERATION_IDS["yue2"]
        },
        "minimax_h3_q4_k": (
            "e36610ac69b5262e914a52635324050bee8f1ad2",
            h3_fixed_files | {"dit.gguf"},
        ),
        "minimax_h3_q4_k_int8_dit": (
            "e36610ac69b5262e914a52635324050bee8f1ad2",
            h3_fixed_files | {"dit_int8.gguf"},
        ),
    }

    for package_id, (revision, expected_files) in expected.items():
        package = packages[package_id]
        assert package.revision == revision
        assert set(package.files) == expected_files
        assert len(package.files) == len(package.download_files) == len(package.sha256)
        assert all(len(digest) == 64 for digest in package.sha256)


def test_server_config_keeps_sfx_generation_and_mixed_speech_models():
    generation_id = "stable_audio_3_small_sfx_q8_0"
    sfx_only = server_config("cpu", [generation_id])
    assert len(sfx_only["models"]) == 1
    assert sfx_only["models"][0]["id"] == generation_id
    assert sfx_only["models"][0]["task"] == "gen"
    assert sfx_only["models"][0]["mode"] == "offline"
    assert sfx_only["ui_management"] is False
    assert sfx_only["lazy_load"] is True
    assert sfx_only["max_loaded_models"] == 1

    mixed = server_config("cpu", ["qwen3_tts_1_7b_base_q8_0", generation_id])
    assert [model["id"] for model in mixed["models"]] == [
        "qwen3_tts_1_7b_base_q8_0",
        generation_id,
    ]
    assert [model["task"] for model in mixed["models"]] == ["tts", "gen"]


def test_generation_packages_are_not_offered_by_the_speech_catalogue():
    speech_ids = {entry["id"] for entry in speech_model_catalog()}
    generation_ids = set().union(*EXPECTED_GENERATION_IDS.values())

    assert generation_ids.isdisjoint(speech_ids)


@pytest.mark.parametrize("backend", ["cpu", "vulkan", "cuda"])
def test_minimax_h3_q4_k_supports_all_manager_backends(backend):
    config = server_config(backend, ["minimax_h3_q4_k"])
    assert config["backend"] == backend
    assert config["models"][0]["id"] == "minimax_h3_q4_k"


@pytest.mark.parametrize("backend", ["cpu", "vulkan"])
def test_minimax_h3_int8_dit_requires_cuda(backend):
    with pytest.raises(ValueError, match="requires the CUDA backend"):
        server_config(backend, ["minimax_h3_q4_k_int8_dit"])


@pytest.mark.parametrize("backend", ["auto", "metal"])
def test_minimax_h3_int8_dit_rejects_unsupported_backends(backend):
    with pytest.raises(ValueError, match="does not support backend"):
        server_config(backend, ["minimax_h3_q4_k_int8_dit"])


def test_minimax_h3_int8_dit_server_config_allows_cuda():
    config = server_config("cuda", ["minimax_h3_q4_k_int8_dit"])
    assert config["backend"] == "cuda"
    assert config["models"][0]["id"] == "minimax_h3_q4_k_int8_dit"


def _minimax_inventory(package_id: str = "minimax_music3_q4_0") -> dict[str, object]:
    return _inventory_for_package(package_id)


def _inventory_for_package(package_id: str) -> dict[str, object]:
    payload = load_audio_cpp_inventory()
    package = copy.deepcopy(next(row for row in payload["packages"] if row["id"] == package_id))
    family = copy.deepcopy(next(row for row in payload["families"] if row["id"] == package["family"]))
    return {
        "runtime_version": payload.get("runtime_version"),
        "source_url": payload.get("source_url"),
        "families": [family],
        "packages": [package],
    }


def _replace_manifest_path(package: dict[str, object], old: str, new: str) -> None:
    weight_manifest = package["weight_manifest"]
    strip_prefix = package.get("strip_prefix", "")
    for entry in weight_manifest["files"]:
        if entry["path"] == old:
            entry["path"] = new
            entry["source_path"] = f"{strip_prefix}/{new}" if strip_prefix else new
            break
    package["files"] = [entry["path"] for entry in weight_manifest["files"]]


def _remove_manifest_path(package: dict[str, object], path: str) -> None:
    weight_manifest = package["weight_manifest"]
    weight_manifest["files"] = [entry for entry in weight_manifest["files"] if entry["path"] != path]
    package["files"] = [entry["path"] for entry in weight_manifest["files"]]


def _add_manifest_path(package: dict[str, object], path: str) -> None:
    strip_prefix = package.get("strip_prefix", "")
    source_path = f"{strip_prefix}/{path}" if strip_prefix else path
    weight_manifest = package["weight_manifest"]
    weight_manifest["files"].append(
        {"path": path, "source_path": source_path, "sha256": "a" * 64, "size": 1}
    )
    package["files"] = [entry["path"] for entry in weight_manifest["files"]]


def _write_inventory(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "audio_cpp_inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_gguf",
        "missing_gguf",
        "wrong_decoder_variant",
        "missing_config",
        "unknown_variant_id",
        "missing_sha256",
        "null_sha256",
        "unpinned_revision",
        "gated",
        "unverified_availability",
        "missing_availability_file",
        "unsupported_download",
        "string_availability",
        "source_escape",
    ],
)
def test_minimax_music3_rejects_incomplete_or_unpinned_layouts(tmp_path, mutation):
    payload = _minimax_inventory()
    package = payload["packages"][0]
    if mutation == "extra_gguf":
        _add_manifest_path(package, "extra.gguf")
    elif mutation == "missing_gguf":
        _remove_manifest_path(package, "vocoder.gguf")
    elif mutation == "wrong_decoder_variant":
        _replace_manifest_path(package, "rvq_depth_decoder_q8_0.gguf", "rvq_depth_decoder_bf16.gguf")
    elif mutation == "missing_config":
        _remove_manifest_path(package, "config/condition_encoder.json")
    elif mutation == "unknown_variant_id":
        package["id"] = "minimax_music3_future"
    elif mutation == "missing_sha256":
        del package["weight_manifest"]["files"][0]["sha256"]
    elif mutation == "null_sha256":
        package["weight_manifest"]["files"][0]["sha256"] = None
    elif mutation == "unpinned_revision":
        package["weight_manifest"]["revision"] = "main"
    elif mutation == "gated":
        package["download"]["gated"] = True
    elif mutation == "unverified_availability":
        package["availability"]["verified"] = False
    elif mutation == "missing_availability_file":
        package["availability"]["missing_files"] = ["vocoder.gguf"]
    elif mutation == "unsupported_download":
        package["availability"]["unsupported_download"] = True
    elif mutation == "string_availability":
        package["availability"] = "available"
    elif mutation == "source_escape":
        _replace_manifest_path(package, "config.json", "../config.json")

    path = _write_inventory(tmp_path, payload)
    assert validated_inventory_packages(path) == ()


@pytest.mark.parametrize(
    ("package_id", "mutation"),
    [
        ("yue2_main_bf16", "missing_vae"),
        ("yue2_main_q4_0", "missing_sidecar"),
        ("yue2_main_q8_0", "wrong_variant"),
        ("yue2_main_bf16", "extra_gguf"),
        ("yue2_main_q4_0", "missing_sha256"),
        ("yue2_main_q8_0", "null_sha256"),
        ("yue2_main_bf16", "unverified_files"),
        ("yue2_main_q4_0", "unpinned_revision"),
        ("yue2_main_q8_0", "source_escape"),
        ("yue2_main_bf16", "unknown_variant"),
    ],
)
def test_yue2_rejects_incomplete_or_unpinned_layouts(tmp_path, package_id, mutation):
    payload = _inventory_for_package(package_id)
    package = payload["packages"][0]
    if mutation == "missing_vae":
        _remove_manifest_path(package, "yue2-vae-f16.gguf")
    elif mutation == "missing_sidecar":
        _remove_manifest_path(package, "sidecars/yue2-qwen.tiktoken")
    elif mutation == "wrong_variant":
        _replace_manifest_path(package, "yue2-3b-q8_0.gguf", "yue2-3b-q4_0.gguf")
    elif mutation == "extra_gguf":
        _add_manifest_path(package, "extra.gguf")
    elif mutation == "missing_sha256":
        del package["weight_manifest"]["files"][0]["sha256"]
    elif mutation == "null_sha256":
        package["weight_manifest"]["files"][0]["sha256"] = None
    elif mutation == "unverified_files":
        package["availability"]["unverified_files"] = ["sidecars/yue2-qwen.tiktoken"]
    elif mutation == "unpinned_revision":
        package["weight_manifest"]["revision"] = "main"
    elif mutation == "source_escape":
        _replace_manifest_path(package, "sidecars/yue2-model-config.json", "../model-config.json")
    elif mutation == "unknown_variant":
        package["id"] = "yue2_main_future"

    path = _write_inventory(tmp_path, payload)
    assert validated_inventory_packages(path) == ()


@pytest.mark.parametrize(
    ("package_id", "mutation"),
    [
        ("minimax_h3_q4_k", "missing_processor"),
        ("minimax_h3_q4_k", "wrong_variant"),
        ("minimax_h3_q4_k_int8_dit", "extra_gguf"),
        ("minimax_h3_q4_k", "missing_sha256"),
        ("minimax_h3_q4_k_int8_dit", "null_sha256"),
        ("minimax_h3_q4_k", "unverified_files"),
        ("minimax_h3_q4_k_int8_dit", "unpinned_revision"),
        ("minimax_h3_q4_k", "source_escape"),
        ("minimax_h3_q4_k_int8_dit", "missing_dit"),
        ("minimax_h3_q4_k", "unknown_variant"),
    ],
)
def test_minimax_h3_rejects_incomplete_or_unpinned_layouts(tmp_path, package_id, mutation):
    payload = _inventory_for_package(package_id)
    package = payload["packages"][0]
    if mutation == "missing_processor":
        _remove_manifest_path(package, "FL2VA/processor/tokenizer.json")
    elif mutation == "wrong_variant":
        _replace_manifest_path(package, "dit.gguf", "dit_int8.gguf")
    elif mutation == "extra_gguf":
        _add_manifest_path(package, "extra.gguf")
    elif mutation == "missing_sha256":
        del package["weight_manifest"]["files"][0]["sha256"]
    elif mutation == "null_sha256":
        package["weight_manifest"]["files"][0]["sha256"] = None
    elif mutation == "unverified_files":
        package["availability"]["unverified_files"] = ["video_vae.gguf"]
    elif mutation == "unpinned_revision":
        package["weight_manifest"]["revision"] = "main"
    elif mutation == "source_escape":
        _replace_manifest_path(package, "configuration.json", "../configuration.json")
    elif mutation == "missing_dit":
        _remove_manifest_path(package, "dit_int8.gguf")
    elif mutation == "unknown_variant":
        package["id"] = "minimax_h3_q4_k_future"

    path = _write_inventory(tmp_path, payload)
    assert validated_inventory_packages(path) == ()
