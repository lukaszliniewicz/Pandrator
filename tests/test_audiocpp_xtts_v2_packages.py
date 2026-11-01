"""Reviewed XTTS v2 install layouts without depending on the mirrored catalogue."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pandrator_manager.audio_cpp_inventory import (
    InventoryError,
    _parse_package,
    load_audio_cpp_packages,
    validated_inventory_packages,
)

VARIANTS = {
    "xtts_v2_gguf_q8_0": "xtts-v2-gpt-q8_0.gguf",
    "xtts_v2_gguf_q4_k_m": "xtts-v2-gpt-q4_k_m.gguf",
    "xtts_v2_gguf": "xtts-v2-gpt.gguf",
}
TARGET_DIRECTORY = "XTTS-v2-GGUF"
REVISION = "1" * 40
FAMILIES = {
    "xtts_v2": {"id": "xtts_v2", "tasks": ["tts", "clone"]},
    "ordinary": {"id": "ordinary", "tasks": ["tts"]},
}


def _set_files(package: dict[str, Any], files: list[str]) -> None:
    package["files"] = list(files)
    package["weight_manifest"]["files"] = [
        {
            "source_path": f"{TARGET_DIRECTORY}/{path}",
            "path": path,
            "sha256": hashlib.sha256(path.encode()).hexdigest(),
            "size": len(path),
        }
        for path in files
    ]


def _package(package_id: str = "xtts_v2_gguf_q8_0") -> dict[str, Any]:
    package: dict[str, Any] = {
        "id": package_id,
        "family": "xtts_v2",
        "format": "gguf",
        "target_directory": TARGET_DIRECTORY,
        "strip_prefix": TARGET_DIRECTORY,
        "download": {
            "kind": "huggingface_snapshot",
            "repo": "fixture/xtts",
            "revision": "main",
            "gated": False,
        },
        "availability": {"verified": True, "missing_files": []},
        "weight_manifest": {
            "repository": "fixture/xtts",
            "revision": REVISION,
        },
    }
    _set_files(package, [VARIANTS[package_id], "xtts-v2-vocoder-f16.gguf", "mel_norms.bin"])
    return package


def _write_inventory(tmp_path: Path, package: dict[str, Any]) -> Path:
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps({"families": list(FAMILIES.values()), "packages": [package]}),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("package_id", VARIANTS)
@pytest.mark.parametrize("source_files", [False, True])
def test_reviewed_xtts_layouts_project_pinned_siblings_and_directory_config(
    tmp_path: Path, package_id: str, source_files: bool
) -> None:
    row = _package(package_id)
    manifest = row["weight_manifest"]["files"]
    if source_files:
        row["files"] = [item["source_path"] for item in manifest]
    path = _write_inventory(tmp_path, row)

    validated, = validated_inventory_packages(path)
    package, = load_audio_cpp_packages(path)

    expected_files = (VARIANTS[package_id], "xtts-v2-vocoder-f16.gguf", "mel_norms.bin")
    assert validated.files == package.files == expected_files
    assert package.id == package_id
    assert package.family == "xtts_v2"
    assert package.config_path == f"models/{TARGET_DIRECTORY}"
    assert package.required_paths(tmp_path) == tuple(
        tmp_path / TARGET_DIRECTORY / file for file in expected_files
    )
    assert package.download_files == tuple(item["source_path"] for item in manifest)
    assert package.sha256 == tuple(item["sha256"] for item in manifest)
    assert package.repository == "fixture/xtts"
    assert package.revision == REVISION
    assert package.download_kind == "huggingface_snapshot"
    assert package.strip_prefix == TARGET_DIRECTORY
    assert package.task == "tts"
    assert package.load_options is None
    assert package.session_options is None


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_id", "unknown_single_gguf", "missing_norms", "missing_vocoder",
        "wrong_vocoder", "wrong_gpt_variant", "extra_gguf", "extra_sidecar",
        "misnamed_norms", "nested_gpt", "duplicate_norms",
    ],
)
def test_xtts_requires_an_exact_reviewed_id_and_local_file_set(
    tmp_path: Path, mutation: str
) -> None:
    row = _package()
    files = list(row["files"])
    if mutation.startswith("unknown"):
        row["id"] = "xtts_v2_unreviewed"
        if mutation == "unknown_single_gguf":
            files = files[:1]
    elif mutation == "missing_norms":
        files.remove("mel_norms.bin")
    elif mutation == "missing_vocoder":
        files.remove("xtts-v2-vocoder-f16.gguf")
    elif mutation == "wrong_vocoder":
        files[1] = "xtts-v2-vocoder-q8_0.gguf"
    elif mutation == "wrong_gpt_variant":
        files[0] = "xtts-v2-gpt-q4_k_m.gguf"
    elif mutation == "extra_gguf":
        files.append("extra.gguf")
    elif mutation == "extra_sidecar":
        files.append("config.json")
    elif mutation == "misnamed_norms":
        files[2] = "mel-norms.bin"
    elif mutation == "nested_gpt":
        files[0] = f"nested/{files[0]}"
    elif mutation == "duplicate_norms":
        files.append("mel_norms.bin")
    _set_files(row, files)

    with pytest.raises(InventoryError):
        _parse_package(row, FAMILIES)
    assert load_audio_cpp_packages(_write_inventory(tmp_path, row)) == ()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_hash", "invalid_hash", "unpinned_revision", "invalid_repository",
        "gated", "unavailable", "missing_download_file", "unsupported_download",
        "unsafe_source", "unsafe_target", "source_mismatch", "files_mismatch",
    ],
)
def test_xtts_exception_preserves_source_hash_path_and_availability_gates(
    tmp_path: Path, mutation: str
) -> None:
    row = _package()
    item = row["weight_manifest"]["files"][0]
    if mutation == "missing_hash":
        del item["sha256"]
    elif mutation == "invalid_hash":
        item["sha256"] = "x" * 64
    elif mutation == "unpinned_revision":
        row["weight_manifest"]["revision"] = "main"
    elif mutation == "invalid_repository":
        row["weight_manifest"]["repository"] = "../fixture/xtts"
    elif mutation == "gated":
        row["download"]["gated"] = True
    elif mutation == "unavailable":
        row["availability"] = "unavailable"
    elif mutation == "missing_download_file":
        row["availability"]["missing_files"] = ["mel_norms.bin"]
    elif mutation == "unsupported_download":
        row["availability"]["unsupported_download"] = True
    elif mutation == "unsafe_source":
        item["source_path"] = "../xtts-v2-gpt-q8_0.gguf"
    elif mutation == "unsafe_target":
        row["target_directory"] = "../XTTS-v2-GGUF"
    elif mutation == "source_mismatch":
        item["source_path"] = f"{TARGET_DIRECTORY}/wrong.gguf"
    elif mutation == "files_mismatch":
        row["files"] = ["wrong.gguf"]

    with pytest.raises(InventoryError):
        _parse_package(row, FAMILIES)
    assert load_audio_cpp_packages(_write_inventory(tmp_path, row)) == ()


@pytest.mark.parametrize("reviewed_id", [False, True])
@pytest.mark.parametrize("multiple_gguf", [False, True])
def test_xtts_exception_does_not_change_other_family_gguf_rules(
    tmp_path: Path, reviewed_id: bool, multiple_gguf: bool
) -> None:
    row = _package()
    row["family"] = "ordinary"
    if not reviewed_id:
        row["id"] = "ordinary_model"
    if not multiple_gguf:
        _set_files(row, ["ordinary.gguf"])

    packages = load_audio_cpp_packages(_write_inventory(tmp_path, row))

    if multiple_gguf:
        assert packages == ()
    else:
        package, = packages
        assert package.config_path == f"models/{TARGET_DIRECTORY}/ordinary.gguf"
