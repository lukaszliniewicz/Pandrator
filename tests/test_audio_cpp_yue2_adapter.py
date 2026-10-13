"""No-network contract tests for the pinned YuE2 adapter cache."""

import hashlib
import io
import threading

import pytest

from pandrator.logic import audio_cpp_assets as assets
from pandrator.logic import audio_cpp_yue2_adapter as adapter
from pandrator.logic.cancellable_process import ProcessCancelled


class FakeResponse:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _use_test_payload(monkeypatch, payload: bytes, *, sha256: str | None = None):
    monkeypatch.setattr(adapter, "SIZE_BYTES", len(payload))
    monkeypatch.setattr(adapter, "SHA256", sha256 or hashlib.sha256(payload).hexdigest())


def test_identity_and_source_are_pinned_exactly():
    assert adapter.identity() == {
        "id": "yue2_instrumental_ar_v3abc_bf16",
        "repository": "Mothersuperior/YuE2-instrumental-cot-full-loras",
        "revision": "947f2f4b28978b2b6c3e316e6a87925c76bf3c4b",
        "filename": "ar_lora_inst_v3abc.bf16.safetensors",
        "size_bytes": 139502088,
        "sha256": "e408fd3148b75b1165f7ddbf63db575d83bb6402a0b5f876fcb767dbcb2c5414",
        "license": "CC-BY-NC-4.0",
    }
    assert adapter.SOURCE_URL == (
        "https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras"
    )
    assert adapter._trusted_file_spec()["url"] == (
        "https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras"
        "/resolve/947f2f4b28978b2b6c3e316e6a87925c76bf3c4b/"
        "ar_lora_inst_v3abc.bf16.safetensors"
    )


def test_download_verifies_then_reuses_without_network(tmp_path, monkeypatch):
    payload = b"verified adapter"
    _use_test_payload(monkeypatch, payload)
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    requested = []

    def opener(request, timeout=30):
        requested.append((request.full_url, timeout))
        return FakeResponse(payload)

    path = adapter.ensure_adapter(settings, opener=opener)
    assert path == adapter.cache_path(settings).resolve()
    assert path.read_bytes() == payload
    assert adapter.ensure_adapter(
        settings, opener=lambda *_args, **_kwargs: pytest.fail("cache reuse called network")
    ) == path
    assert requested == [
        (
            "https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras"
            "/resolve/947f2f4b28978b2b6c3e316e6a87925c76bf3c4b/"
            "ar_lora_inst_v3abc.bf16.safetensors",
            30,
        )
    ]


def test_same_size_wrong_digest_leaves_no_installed_or_partial_file(
    tmp_path, monkeypatch
):
    payload = b"same-size wrong digest"
    _use_test_payload(monkeypatch, payload, sha256="0" * 64)
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}

    with pytest.raises(assets.AudioAssetsError, match="SHA-256"):
        adapter.ensure_adapter(settings, opener=lambda *_args, **_kwargs: FakeResponse(payload))

    destination = adapter.cache_path(settings)
    assert not destination.exists()
    assert not list(destination.parent.glob(".yue2_instrumental_ar_v3abc_bf16-download-*"))
    _use_test_payload(monkeypatch, payload)
    assert adapter.ensure_adapter(
        settings, opener=lambda *_args, **_kwargs: FakeResponse(payload)
    ).read_bytes() == payload


def test_cancellation_before_network_does_not_create_cache(tmp_path):
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    event = threading.Event()
    event.set()

    def opener(*_args, **_kwargs):
        pytest.fail("cancelled operation called network")

    with pytest.raises(ProcessCancelled, match="canceled"):
        adapter.ensure_adapter(settings, cancel_event=event, opener=opener)
    assert not (tmp_path / "cache").exists()


def test_cache_path_rejects_symlink_package_and_destination(tmp_path):
    settings = {"audio_cpp_cache_dir": str(tmp_path / "cache")}
    package = adapter.cache_path(settings).parent
    package.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    def opener(*_args, **_kwargs):
        pytest.fail("symlinked path called network")

    package.symlink_to(outside, target_is_directory=True)
    with pytest.raises(assets.AudioAssetsError, match="package cache directory.*symlink"):
        adapter.cache_path(settings)
    with pytest.raises(assets.AudioAssetsError, match="package cache directory.*symlink"):
        adapter.ensure_adapter(settings, opener=opener)

    package.unlink()
    package.mkdir()
    destination = package / adapter.FILENAME
    outside_file = outside / "adapter.safetensors"
    outside_file.write_bytes(b"outside")
    destination.symlink_to(outside_file)
    with pytest.raises(assets.AudioAssetsError, match="cache file.*symlink"):
        adapter.cache_path(settings)
    with pytest.raises(assets.AudioAssetsError, match="cache file.*symlink"):
        adapter.ensure_adapter(settings, opener=opener)


def test_cache_path_is_shared_across_base_model_settings(tmp_path):
    root = tmp_path / "cache"
    first = {
        "audio_cpp_cache_dir": str(root),
        "base_model": "yue2-base-q8_0",
    }
    second = {
        "audio_cpp_cache_dir": str(root),
        "base_model": "yue2-base-bf16",
    }
    expected = root / adapter.ADAPTER_ID / adapter.FILENAME

    assert adapter.cache_path(first) == expected
    assert adapter.cache_path(second) == expected
    assert not root.exists()
