from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.snapshot_audio_cpp_catalogue import (
    SnapshotError,
    build_snapshot,
    flatten_packages,
    load_specs,
    main,
)


class FixtureClient:
    def __init__(self, metadata: dict, files: dict[str, bytes]) -> None:
        self.metadata = metadata
        self.files = files
        self.json_calls = 0
        self.file_calls = 0

    def get_json(self, _url: str) -> dict:
        self.json_calls += 1
        return self.metadata

    def get_small_file(self, url: str) -> bytes:
        self.file_calls += 1
        return self.files[url.rsplit("/", 1)[-1]]


def _write_spec(root: Path, name: str, spec: dict) -> None:
    (root / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _fixture_specs(tmp_path: Path) -> Path:
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(
        specs,
        "fixture",
        {
            "family": "fixture",
            "display_name": "Fixture",
            "description": "Fixture family",
            "category": "tts",
            "status": "supported",
            "tasks": ["tts"],
            "modes": ["offline"],
            "languages": ["en"],
            "capabilities": {"tts": []},
            "options": {"session": [{"name": "weight_type"}]},
            "ui": {"docs": ["docs/tts.md"]},
            "package_defaults": {
                "download": {
                    "kind": "huggingface_snapshot",
                    "repo": "fixture/repo",
                    "revision": "main",
                    "gated": False,
                }
            },
            "packages": [
                {
                    "id": "fixture_q8",
                    "display_name": "Fixture Q8",
                    "format": "gguf",
                    "precision": "q8_0",
                    "default": True,
                    "target_directory": "Fixture-GGUF",
                    "strip_prefix": "Fixture-GGUF",
                    "files": ["Fixture-GGUF/weights.gguf", "Fixture-GGUF/config.json"],
                },
                {
                    "id": "fixture_missing",
                    "display_name": "Fixture missing",
                    "format": "gguf",
                    "precision": "f16",
                    "target_directory": "Fixture-GGUF",
                    "strip_prefix": "Fixture-GGUF",
                    "files": ["Fixture-GGUF/missing.gguf"],
                },
                {
                    "id": "fixture_gated",
                    "display_name": "Fixture gated",
                    "format": "gguf",
                    "precision": "f16",
                    "target_directory": "Fixture-GGUF",
                    "strip_prefix": "Fixture-GGUF",
                    "files": ["Fixture-GGUF/weights.gguf"],
                    "download": {"gated": True},
                },
                {
                    "id": "fixture_unsupported",
                    "display_name": "Fixture unsupported",
                    "format": "native",
                    "precision": "native",
                    "target_directory": "Fixture-native",
                    "files": ["weights.bin"],
                    "download": {"kind": "unsupported", "reason": "local only"},
                },
            ],
        },
    )
    return specs


def test_flatten_merges_defaults_and_keeps_exact_source_files(tmp_path: Path) -> None:
    specs_dir = _fixture_specs(tmp_path)
    packages = flatten_packages(load_specs(specs_dir))
    package = next(item for item in packages if item["id"] == "fixture_q8")
    assert package["download"] == {
        "kind": "huggingface_snapshot",
        "repo": "fixture/repo",
        "revision": "main",
        "gated": False,
    }
    assert package["files"] == ["Fixture-GGUF/weights.gguf", "Fixture-GGUF/config.json"]
    assert package["_local_files"] == ["weights.gguf", "config.json"]


def test_unsafe_filename_family_and_package_paths_are_rejected(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "bad", {"family": "bad/id", "packages": []})
    with pytest.raises(SnapshotError):
        load_specs(specs)
    (specs / "bad.json").unlink()

    _write_spec(
        specs,
        "safe",
        {
            "family": "safe",
            "packages": [
                {
                    "id": "bad/id",
                    "target_directory": "models",
                    "files": ["weights.gguf"],
                }
            ],
        },
    )
    with pytest.raises(SnapshotError):
        flatten_packages(load_specs(specs))

    _write_spec(
        specs,
        "glob",
        {
            "family": "glob",
            "packages": [
                {"id": "glob_pkg", "target_directory": "models", "files": ["*.gguf"]}
            ],
        },
    )
    with pytest.raises(SnapshotError):
        flatten_packages(load_specs(specs))


def test_manifest_resolution_preserves_missing_and_gated_entries(tmp_path: Path) -> None:
    specs_dir = _fixture_specs(tmp_path)
    content = b"{}\n"
    metadata = {
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "gated": False,
        "cardData": {"license": "other"},
        "siblings": [
            {
                "rfilename": "Fixture-GGUF/weights.gguf",
                "size": 8,
                "lfs": {"sha256": "a" * 64, "size": 8},
            },
            {"rfilename": "Fixture-GGUF/config.json", "size": len(content)},
        ],
    }
    client = FixtureClient(metadata, {"config.json": content})
    snapshot = build_snapshot(specs_dir, client=client)
    packages = {item["id"]: item for item in snapshot["packages"]}
    assert packages["fixture_q8"]["availability"]["verified"] is True
    assert packages["fixture_q8"]["weight_manifest"]["revision"] == metadata["sha"]
    assert packages["fixture_q8"]["weight_manifest"]["files"][1]["path"] == "config.json"
    assert packages["fixture_missing"]["availability"]["verified"] is False
    assert packages["fixture_missing"]["availability"]["missing_files"] == [
        "Fixture-GGUF/missing.gguf"
    ]
    assert packages["fixture_gated"]["availability"]["gated"] is True
    assert "weight_manifest" not in packages["fixture_gated"]
    assert packages["fixture_unsupported"]["availability"]["unsupported_download"] is True
    assert client.json_calls == 1
    assert client.file_calls == 1


def test_real_source_has_expected_inventory_counts() -> None:
    source = Path(
        "/home/lliniewicz/Pandrator/services/audio_cpp/versions/"
        "audio-cpp-0.8.1-vulkan-a2c9e5de-0b60-4349-99e7-d2fbf7831108/model_specs"
    )
    if not source.is_dir():
        pytest.skip("installed audio.cpp source specs are unavailable")
    specs = load_specs(source)
    assert len(specs) == 86
    assert len(flatten_packages(specs)) == 256


def test_cli_writes_byte_identical_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs_dir = _fixture_specs(tmp_path)
    monkeypatch.setattr(
        "scripts.snapshot_audio_cpp_catalogue.MetadataClient",
        lambda *_args, **_kwargs: FixtureClient(
            {
                "sha": "0123456789abcdef0123456789abcdef01234567",
                "gated": False,
                "siblings": [],
            },
            {},
        ),
    )
    output = tmp_path / "pandrator" / "logic" / "audio_cpp_inventory.json"
    mirror = tmp_path / "pandrator_manager" / "audio_cpp_inventory.json"
    status = main(
        [
            "--specs-dir",
            str(specs_dir),
            "--output",
            str(output),
            "--mirror",
            str(mirror),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert status == 0
    assert output.read_bytes() == mirror.read_bytes()
