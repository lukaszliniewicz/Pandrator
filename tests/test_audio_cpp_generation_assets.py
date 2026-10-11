"""Generation package cache tests with synthetic local-only manifests."""

from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import replace

import pytest

from pandrator.logic import audio_cpp_assets as shared_assets
from pandrator.logic import audio_cpp_generation_assets as generation_assets
from pandrator.logic import audio_cpp_generation_models as registry
from pandrator.logic.audio_cpp_assets import AudioAssetsError


class FakeResponse:
    def __init__(self, payload: bytes):
        self.stream = io.BytesIO(payload)

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _small_spec(model_id: str, file_payloads: dict[str, bytes]):
    original = registry.get_model(model_id)
    files = tuple(
        replace(
            next(asset for asset in original.files if asset.path == path),
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        for path, payload in file_payloads.items()
    )
    return replace(original, files=files), file_payloads


def _patch_spec(monkeypatch, spec):
    monkeypatch.setattr(
        generation_assets,
        "get_model",
        lambda model_id: spec if model_id == spec.id else registry.get_model(model_id),
    )


def test_cache_paths_are_exact_and_model_paths_match_every_package_layout(tmp_path):
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    for model in registry.all_models():
        package = generation_assets.package_path(model.id, settings)
        assert package == tmp_path / "cache" / model.id
        expected = package if model.is_directory_model else package / model.model_path_asset.path
        assert generation_assets.model_path(model.id, settings) == expected
        for item in model.files:
            assert generation_assets.file_path(model.id, item.path, settings) == package / item.path
    with pytest.raises(AudioAssetsError, match="not in its pinned manifest"):
        generation_assets.file_path(
            "minimax_music3_q8_0", "unlisted.gguf", settings
        )


def test_package_and_nested_paths_cannot_escape_through_symlinks(tmp_path):
    model_id = "minimax_music3_q8_0"
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    package = generation_assets.package_path(model_id, settings)
    package.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    package.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AudioAssetsError, match="must not be a symlink"):
        generation_assets.file_path(model_id, "config.json", settings)
    with pytest.raises(AudioAssetsError, match="must not be a symlink"):
        generation_assets.model_path(model_id, settings)

    package.unlink()
    package.mkdir()
    nested = package / "tokenizer"
    nested.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AudioAssetsError, match="escapes its cache directory"):
        generation_assets.file_path(model_id, "tokenizer/tokenizer.json", settings)


def test_ensure_downloads_and_verifies_only_the_selected_direct_model(tmp_path, monkeypatch):
    model_id = "stable_audio_3_small_music_q8_0"
    asset_path = registry.get_model(model_id).model_path_asset.path
    spec, payloads = _small_spec(model_id, {asset_path: b"synthetic stable weights"})
    _patch_spec(monkeypatch, spec)
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    requested = []

    def opener(request, timeout=30):
        assert timeout == 30
        requested.append(request.full_url)
        return FakeResponse(next(iter(payloads.values())))

    installed = generation_assets.ensure_model(model_id, settings, opener=opener)
    assert installed == generation_assets.model_path(model_id, settings).resolve()
    assert installed.read_bytes() == b"synthetic stable weights"
    assert generation_assets.is_cached(model_id, settings)
    assert requested == [
        f"https://huggingface.co/{spec.repository}/resolve/{spec.revision}/"
        f"{spec.files[0].source_path}"
    ]
    assert not generation_assets.package_path(
        "stable_audio_3_small_sfx_q8_0", settings
    ).exists()
    assert generation_assets.ensure_model(
        model_id,
        settings,
        opener=lambda *_args, **_kwargs: pytest.fail("verified cache should be reused"),
    ) == installed


@pytest.mark.parametrize("model_id", ("minimax_music3_q4_0", "yue2_main_q8_0"))
def test_directory_model_waits_for_every_manifest_file_before_returning(
    tmp_path, monkeypatch, model_id
):
    manifest = registry.get_model(model_id)
    selected_paths = tuple(asset.path for asset in manifest.files)
    payloads = {path: f"file:{path}".encode() for path in selected_paths}
    spec, payloads = _small_spec(model_id, payloads)
    _patch_spec(monkeypatch, spec)
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    requested = []

    def opener(request, timeout=30):
        url = request.full_url
        requested.append(url)
        item = next(
            asset
            for asset in spec.files
            if url.endswith("/" + asset.source_path)
        )
        return FakeResponse(payloads[item.path])

    installed = generation_assets.ensure_model(model_id, settings, opener=opener)
    assert installed == generation_assets.package_path(model_id, settings).resolve()
    assert installed.is_dir()
    assert len(requested) == len(selected_paths)
    assert generation_assets.is_cached(model_id, settings)
    assert all((installed / path).read_bytes() == payloads[path] for path in selected_paths)
    assert set(selected_paths) == {asset.path for asset in manifest.files}


def test_h3_selects_exact_dit_but_verifies_every_package_file(tmp_path, monkeypatch):
    model_id = "minimax_h3_q4_k_int8_dit"
    manifest = registry.get_model(model_id)
    selected_paths = tuple(asset.path for asset in manifest.files)
    payloads = {path: f"verified:{path}".encode() for path in selected_paths}
    spec, payloads = _small_spec(model_id, payloads)
    _patch_spec(monkeypatch, spec)
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    requested = []

    def opener(request, timeout=30):
        requested.append(request.full_url)
        item = next(asset for asset in spec.files if request.full_url.endswith("/" + asset.source_path))
        return FakeResponse(payloads[item.path])

    installed = generation_assets.ensure_model(model_id, settings, opener=opener)
    assert installed == (
        generation_assets.package_path(model_id, settings) / "dit_int8.gguf"
    ).resolve()
    assert installed.is_file()
    assert installed.read_bytes() == payloads["dit_int8.gguf"]
    assert len(requested) == len(selected_paths) == 12
    assert generation_assets.is_cached(model_id, settings)


def test_corrupt_file_is_not_installed_and_retries_do_not_leave_partial_files(
    tmp_path, monkeypatch
):
    model_id = "heartmula_q8_0"
    path = registry.get_model(model_id).model_path_asset.path
    spec, _payloads = _small_spec(model_id, {path: b"expected bytes"})
    _patch_spec(monkeypatch, spec)
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    with pytest.raises(AudioAssetsError, match="SHA-256"):
        generation_assets.ensure_model(
        model_id, settings, opener=lambda *_a, **_k: FakeResponse(b"bad payload!!!")
        )
    destination = generation_assets.file_path(model_id, path, settings)
    assert not destination.exists()
    assert not list(destination.parent.glob(".*-download-*"))
    assert not generation_assets.is_cached(model_id, settings)


def test_cancelled_package_operation_does_not_open_download(tmp_path, monkeypatch):
    model_id = "controlfoley_large_44k_q8_0"
    path = registry.get_model(model_id).model_path_asset.path
    spec, _payloads = _small_spec(model_id, {path: b"weights"})
    _patch_spec(monkeypatch, spec)
    cancelled = threading.Event()
    cancelled.set()
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    with pytest.raises(Exception, match="canceled"):
        generation_assets.ensure_model(
            model_id,
            settings,
            cancel_event=cancelled,
            opener=lambda *_a, **_k: pytest.fail("cancelled request must not download"),
        )
    assert not generation_assets.package_path(model_id, settings).exists()


def test_verified_manager_files_are_reused_without_downloading(tmp_path, monkeypatch):
    model_id = "ace_step_turbo_q8_0"
    path = registry.get_model(model_id).model_path_asset.path
    payload = b"manager-owned pinned model"
    spec, _payloads = _small_spec(model_id, {path: payload})
    _patch_spec(monkeypatch, spec)
    manager_root = tmp_path / "manager-models"
    source = manager_root / spec.target_directory / path
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    monkeypatch.setattr(shared_assets, "_manager_models_dirs", lambda: [manager_root])
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}

    installed = generation_assets.ensure_model(
        model_id,
        settings,
        opener=lambda *_a, **_k: pytest.fail("verified Manager copy should be reused"),
    )
    assert installed.read_bytes() == payload
    assert installed.stat().st_ino == source.stat().st_ino or installed.read_bytes() == source.read_bytes()
