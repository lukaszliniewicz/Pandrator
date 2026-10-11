from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pandrator.logic import audio_cpp_catalogue
from scripts.snapshot_audio_cpp_catalogue import (
    MAX_SMALL_FILE_BYTES,
    MetadataClient,
    SnapshotError,
    _cache_key,
    _file_detail,
    build_snapshot,
    flatten_packages,
    load_specs,
    main,
)


@pytest.mark.parametrize("loader_name", ["inventory", "curation"])
def test_catalogue_resources_use_utf8_with_legacy_windows_default(monkeypatch, loader_name):
    original_read = Path.read_text

    def legacy_read(path, encoding=None, errors=None):
        return original_read(path, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", legacy_read)
    loader = getattr(audio_cpp_catalogue, loader_name)
    loader.cache_clear()
    try:
        resource = Path(audio_cpp_catalogue.__file__).with_name(
            f"audio_cpp_{loader_name}.json"
        )
        expected = json.loads(resource.read_bytes().decode("utf-8"))
        assert loader() == expected
    finally:
        loader.cache_clear()


class FixtureClient:
    def __init__(self, metadata: dict[str, Any], files: dict[str, bytes]) -> None:
        self.metadata = metadata
        self.files = files
        self.json_calls = 0
        self.file_calls: list[str] = []

    def get_json(self, url: str) -> dict[str, Any]:
        self.json_calls += 1
        return self.metadata

    def get_small_file(self, url: str) -> bytes:
        self.file_calls.append(url)
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


def test_yue2_main_packages_compose_f16_vae_and_vae_rows_are_components() -> None:
    vae_file = "yue2-vae-f16.gguf"
    specs = [
        {
            "family": "yue2",
            "packages": [
                {
                    "id": package_id,
                    "target_directory": "Yue2-3B-GGUF",
                    "files": files,
                }
                for package_id, files in (
                    ("yue2_main_bf16", ["yue2-3b-bf16.gguf"]),
                    ("yue2_main_q4_0", ["yue2-3b-q4_0.gguf", vae_file]),
                    ("yue2_main_q8_0", ["yue2-3b-q8_0.gguf"]),
                    ("yue2_vae_f16", [vae_file]),
                    ("yue2_vae_f32", ["yue2-vae-f32.gguf"]),
                )
            ],
        }
    ]
    packages = {package["id"]: package for package in flatten_packages(specs)}

    for package_id in ("yue2_main_bf16", "yue2_main_q4_0", "yue2_main_q8_0"):
        assert packages[package_id]["files"].count(vae_file) == 1
        assert packages[package_id]["_local_files"].count(vae_file) == 1
    for package_id in ("yue2_vae_f16", "yue2_vae_f32"):
        assert packages[package_id]["entry_role"] == "component"
        assert packages[package_id]["category"] == "component"
        assert packages[package_id]["tasks"] == []


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
    assert len(client.file_calls) == 1
    assert client.file_calls[0].endswith("/config.json")
    assert not any(url.endswith("/weights.gguf") for url in client.file_calls)


def _git_blob_id(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    ).hexdigest()


def test_large_tokenizer_sidecar_is_hashed_at_immutable_revision() -> None:
    content = b"tokenizer-data\n" * 100_000
    revision = "0123456789abcdef0123456789abcdef01234567"
    client = FixtureClient({}, {"tokenizer.json": content})
    detail, verified = _file_detail(
        "Fixture-GGUF/tokenizer.json",
        "Fixture-GGUF",
        {"size": len(content), "blobId": _git_blob_id(content)},
        client,
        "fixture/repo",
        revision,
        allow_content=True,
    )

    assert len(content) > 1024 * 1024
    assert verified is True
    assert detail["size"] == len(content)
    assert detail["sha256"] == hashlib.sha256(content).hexdigest()
    assert client.file_calls == [
        "https://huggingface.co/fixture/repo/resolve/"
        f"{revision}/Fixture-GGUF/tokenizer.json"
    ]


def test_content_fallback_rejects_declared_size_and_git_blob_conflicts() -> None:
    content = b"exact body"
    client = FixtureClient({}, {"tokenizer.json": content})
    detail, verified = _file_detail(
        "Fixture-GGUF/tokenizer.json",
        "Fixture-GGUF",
        {"size": len(content) + 1},
        client,
        "fixture/repo",
        "0123456789abcdef0123456789abcdef01234567",
        allow_content=True,
    )
    assert verified is False
    assert detail["sha256"] is None

    detail, verified = _file_detail(
        "Fixture-GGUF/tokenizer.json",
        "Fixture-GGUF",
        {"size": len(content), "blobId": "0" * 40},
        client,
        "fixture/repo",
        "0123456789abcdef0123456789abcdef01234567",
        allow_content=True,
    )
    assert verified is False
    assert detail["sha256"] is None


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int | None = None) -> bytes:
        return self.body if limit is None else self.body[:limit]


def test_small_file_client_enforces_network_and_cached_upper_bounds(tmp_path: Path) -> None:
    at_limit = b"x" * MAX_SMALL_FILE_BYTES
    client = MetadataClient(
        tmp_path / "network-ok",
        opener=lambda *_args, **_kwargs: _Response(at_limit),
        retries=0,
    )
    assert len(client.get_small_file("https://huggingface.co/fixture/at-limit")) == len(
        at_limit
    )

    too_large = b"x" * (MAX_SMALL_FILE_BYTES + 1)
    client = MetadataClient(
        tmp_path / "network-over",
        opener=lambda *_args, **_kwargs: _Response(too_large),
        retries=0,
    )
    with pytest.raises(SnapshotError, match="size limit"):
        client.get_small_file("https://huggingface.co/fixture/over-limit")

    cache_dir = tmp_path / "cached-over"
    cache_dir.mkdir()
    url = "https://huggingface.co/fixture/cached-over-limit"
    (cache_dir / _cache_key(url, ".bin")).write_bytes(too_large)
    client = MetadataClient(
        cache_dir,
        opener=lambda *_args, **_kwargs: pytest.fail("oversized cache must not refetch"),
        retries=0,
    )
    with pytest.raises(SnapshotError, match="size limit"):
        client.get_small_file(url)


@pytest.mark.parametrize(
    "extension",
    [".gguf", ".safetensors", ".pt", ".pth", ".bin", ".npz", ".npy"],
)
def test_weight_file_extensions_never_use_content_fallback(extension: str) -> None:
    source_path = f"Fixture-GGUF/weights{extension}"
    client = FixtureClient({}, {})
    detail, verified = _file_detail(
        source_path,
        "Fixture-GGUF",
        {"size": 16},
        client,
        "fixture/repo",
        "0123456789abcdef0123456789abcdef01234567",
        allow_content=True,
    )

    assert verified is False
    assert detail["sha256"] is None
    assert client.file_calls == []


def test_missing_and_present_unverified_files_are_reported_separately(
    tmp_path: Path,
) -> None:
    specs_dir = _fixture_specs(tmp_path)
    metadata = {
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "gated": False,
        "siblings": [
            {
                "rfilename": "Fixture-GGUF/weights.gguf",
                "size": 8,
                "lfs": {"sha256": "a" * 64, "size": 8},
            },
            {"rfilename": "Fixture-GGUF/config.json", "size": 2},
        ],
    }
    client = FixtureClient(metadata, {"config.json": b"bad"})
    snapshot = build_snapshot(specs_dir, client=client)
    package = next(item for item in snapshot["packages"] if item["id"] == "fixture_q8")

    assert package["availability"]["missing_files"] == []
    assert package["availability"]["unverified_files"] == ["Fixture-GGUF/config.json"]
    assert package["availability"]["verified"] is False
    assert client.file_calls == [
        "https://huggingface.co/fixture/repo/resolve/"
        "0123456789abcdef0123456789abcdef01234567/Fixture-GGUF/config.json"
    ]

    package = next(item for item in snapshot["packages"] if item["id"] == "fixture_missing")
    assert package["availability"]["missing_files"] == ["Fixture-GGUF/missing.gguf"]
    assert package["availability"]["unverified_files"] == []


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
