"""Manager-side validation and projection of the mirrored audio.cpp inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pandrator_manager.audio_cpp_inventory import (
    load_audio_cpp_packages,
    validated_inventory_packages,
)
from pandrator_manager.components.audiocpp import (
    MANUAL_MODEL_IDS,
    MODEL_PACKAGES,
    SUPPORTED_MODEL_IDS,
    AudioCppModelPackage,
)
from pandrator_manager.operations.handlers import FilesystemTaskHandler


def _manifest(source_path: str, path: str, content: bytes = b"gguf") -> dict[str, object]:
    return {
        "source_path": source_path,
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _package(
    package_id: str,
    family: str,
    *,
    source_path: str = "model.gguf",
    path: str = "model.gguf",
    content: bytes = b"gguf",
    files: list[str] | None = None,
    download: dict[str, object] | None = None,
    availability: object = "available",
    package_format: str = "gguf",
    target_directory: str | None = None,
    strip_prefix: str = "",
) -> dict[str, object]:
    return {
        "id": package_id,
        "family": family,
        "label": package_id,
        "format": package_format,
        "precision": "q8_0",
        "files": files or [path],
        "target_directory": target_directory or f"{package_id}-GGUF",
        "strip_prefix": strip_prefix,
        "download": download
        or {
            "kind": "huggingface_snapshot",
            "repo": "owner/repo",
            "revision": "1" * 40,
            "gated": False,
        },
        "availability": availability,
        "manifest": [_manifest(source_path, path, content)],
    }


def _inventory(*packages: dict[str, object]) -> dict[str, object]:
    families = [
        {"id": "normal", "category": "speech", "tasks": ["tts"]},
        {"id": "clone_only", "category": "speech", "tasks": ["clone"]},
        {"id": "moss_voicegen", "category": "speech", "tasks": ["tts"]},
        {"id": "music", "category": "music", "tasks": ["music"]},
        {"id": "minimax_h3", "category": "dialogue", "tasks": ["tts"]},
        {"id": "pocket_tts", "category": "speech", "tasks": ["tts"]},
    ]
    return {"runtime_version": "0.8.1", "source_url": "https://example.test", "families": families, "packages": list(packages)}


def _write_inventory(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "audio_cpp_inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_available_digest_verified_speech_packages_project_and_infer_tasks(tmp_path):
    path = _write_inventory(
        tmp_path,
        _inventory(
            _package("ordinary_chatterbox", "clone_only"),
            _package("moss_voice_design", "moss_voicegen"),
            _package(
                "pocket_tts_german_q8_0",
                "pocket_tts",
                source_path="root/pocket.gguf",
                path="pocket.gguf",
                files=["pocket.gguf"],
                strip_prefix="root",
                download={
                    "kind": "huggingface_snapshot",
                    "repo": "another/repo",
                    "revision": "2" * 40,
                    "gated": False,
                },
                target_directory="PocketTTS-GGUF/german",
            ),
        ),
    )

    rows = validated_inventory_packages(path)
    assert {row.id for row in rows} == {
        "ordinary_chatterbox",
        "moss_voice_design",
        "pocket_tts_german_q8_0",
    }
    projected = {package.id: package for package in load_audio_cpp_packages(path)}
    assert projected["ordinary_chatterbox"].task == "clon"
    assert projected["moss_voice_design"].task == "vdes"
    assert projected["pocket_tts_german_q8_0"].config_path == (
        "models/PocketTTS-GGUF/german"
    )
    assert projected["pocket_tts_german_q8_0"].load_options == {"language": "german"}
    assert projected["pocket_tts_german_q8_0"].repository == "another/repo"
    assert projected["pocket_tts_german_q8_0"].revision == "2" * 40


def test_invalid_paths_digests_non_speech_and_multiple_gguf_are_not_eligible(tmp_path):
    multiple = _package("multiple", "normal", files=["first.gguf", "second.gguf"])
    multiple["manifest"] = [
        _manifest("first.gguf", "first.gguf"),
        _manifest("second.gguf", "second.gguf"),
    ]
    bad_digest = _package("bad_digest", "normal")
    bad_digest["manifest"] = [{**_manifest("model.gguf", "model.gguf"), "sha256": "0" * 63 + "x"}]
    bad_path = _package("bad_path", "normal", source_path="../escape.gguf", path="../escape.gguf")
    non_speech = _package("music_model", "music")
    catalogue_only = _package("dialogue_model", "minimax_h3")

    rows = validated_inventory_packages(
        _write_inventory(tmp_path, _inventory(multiple, bad_digest, bad_path, non_speech, catalogue_only))
    )
    assert rows == ()


def test_manual_packages_remain_first_and_unchanged():
    assert tuple(SUPPORTED_MODEL_IDS[: len(MANUAL_MODEL_IDS)]) == MANUAL_MODEL_IDS
    assert tuple(MODEL_PACKAGES)[: len(MANUAL_MODEL_IDS)] == MANUAL_MODEL_IDS
    assert MODEL_PACKAGES["pocket_tts_english_q8_0"].config_path == (
        "models/PocketTTS-GGUF/english"
    )


def test_spec_pinning_uses_each_package_repository_revision_and_exact_manifest(tmp_path):
    package = AudioCppModelPackage(
        id="fixture_package",
        family="fixture",
        target_directory="Fixture",
        files=("model.gguf", "sidecar.json"),
        sha256=("a" * 64, "b" * 64),
        task="tts",
        download_kind="huggingface_snapshot",
        repository="different/repository",
        revision="3" * 40,
        download_files=("weights/model.gguf", "weights/sidecar.json"),
        strip_prefix="weights",
    )
    specs_root = tmp_path / "model_specs"
    specs_root.mkdir()
    spec_path = specs_root / "fixture.json"
    spec_path.write_text(
        json.dumps({"packages": [{"id": package.id, "download": {"revision": "main"}}]}),
        encoding="utf-8",
    )

    FilesystemTaskHandler._pin_audio_cpp_model_specs(specs_root, [package])

    entry = json.loads(spec_path.read_text(encoding="utf-8"))["packages"][0]
    assert entry["download"] == {
        "kind": "huggingface_snapshot",
        "repo": "different/repository",
        "revision": "3" * 40,
    }
    assert entry["files"] == ["weights/model.gguf", "weights/sidecar.json"]
    assert entry["strip_prefix"] == "weights"
    assert entry["target_directory"] == "Fixture"


def test_manual_package_layout_is_pinned_at_the_downloader_package_level(tmp_path):
    package = MODEL_PACKAGES["pocket_tts_english_q8_0"]
    spec = tmp_path / "pocket_tts.json"
    spec.write_text(json.dumps({"packages": [{"id": package.id, "files": ["stale.gguf"], "strip_prefix":"wrong", "target_directory":"wrong"}]}))
    FilesystemTaskHandler._pin_audio_cpp_model_specs(tmp_path, [package])
    pinned = json.loads(spec.read_text())["packages"][0]
    assert pinned["strip_prefix"] == package.target_directory
    assert pinned["target_directory"] == package.target_directory
    assert pinned["files"] == [f"{package.target_directory}/{path}" for path in package.files]
