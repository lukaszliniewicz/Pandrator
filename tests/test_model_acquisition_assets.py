"""Local model acquisition checks use tiny synthetic pins and fake openers."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest

from pandrator.logic import audio_cpp_assets
from pandrator.logic.dubbing import crispasr_qwen_assets, qwen_alignment
from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    model_acquisition_scope,
)


def _pinned_spec(payload: bytes) -> dict[str, Any]:
    return {
        "id": "synthetic-probe",
        "label": "Synthetic probe",
        "family": "test",
        "cli_task": "sep",
        "cli_family": "test",
        "directory": "Synthetic",
        "filename": "probe.gguf",
        "kind": "gguf",
        "repo": "example/model",
        "revision": "0" * 40,
        "url": "https://huggingface.co/example/model/resolve/" + "0" * 40 + "/probe.gguf",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "utility": None,
        "notes": "synthetic test pin",
    }


def _unexpected_opener(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("network opener ran without an acquisition grant")


def _authorizer_state() -> tuple[list[bool], list[None], Any]:
    allowed = [True]
    calls: list[None] = []

    def authorize() -> None:
        calls.append(None)
        if not allowed[0]:
            raise PermissionError("authorization was revoked")

    return allowed, calls, authorize


def test_audio_cpp_denies_corrupt_miss_but_allows_verified_cache_and_manager_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"tiny synthetic audio.cpp GGUF"
    spec = _pinned_spec(payload)
    denied_path = tmp_path / "denied" / "probe.gguf"
    denied_path.parent.mkdir()
    denied_path.write_bytes(b"preserve this corrupt file")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            audio_cpp_assets.shutil,
            "disk_usage",
            lambda _path: pytest.fail("disk-space work ran before authorization"),
        )
        with pytest.raises(ModelAcquisitionDenied):
            audio_cpp_assets.ensure_trusted_file(spec, denied_path, opener=_unexpected_opener)
        with pytest.raises(ModelAcquisitionDenied):
            audio_cpp_assets.ensure_trusted_file(
                spec, tmp_path / "missing" / "probe.gguf", opener=_unexpected_opener
            )
    assert denied_path.read_bytes() == b"preserve this corrupt file"

    manager_source = tmp_path / "manager" / "probe.gguf"
    manager_source.parent.mkdir()
    manager_source.write_bytes(payload)
    reused_path = audio_cpp_assets.ensure_trusted_file(
        spec,
        tmp_path / "reused" / "probe.gguf",
        manager_source=manager_source,
        opener=_unexpected_opener,
    )
    assert reused_path.read_bytes() == payload

    allowed, authorization_calls, authorize = _authorizer_state()
    downloaded_path = tmp_path / "downloaded" / "probe.gguf"
    opens: list[None] = []

    def opener(*_args: object, **_kwargs: object) -> io.BytesIO:
        opens.append(None)
        return io.BytesIO(payload)

    with model_acquisition_scope(authorize):
        assert (
            audio_cpp_assets.ensure_trusted_file(spec, downloaded_path, opener=opener).read_bytes()
            == payload
        )
    assert len(opens) == 1
    assert len(authorization_calls) == 1

    # A verified cache hit does not consult the authorizer or opener.
    assert (
        audio_cpp_assets.ensure_trusted_file(
            spec, downloaded_path, opener=_unexpected_opener
        ).read_bytes()
        == payload
    )
    assert len(authorization_calls) == 1

    allowed[0] = False
    with model_acquisition_scope(authorize), pytest.raises(ModelAcquisitionDenied):
        audio_cpp_assets.ensure_trusted_file(
            spec, tmp_path / "revoked" / "probe.gguf", opener=_unexpected_opener
        )
    assert len(authorization_calls) == 2
    assert len(opens) == 1


def test_qwen_alignment_denies_corrupt_miss_and_allows_local_gguf_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"GGUF synthetic aligner"
    monkeypatch.setattr(qwen_alignment, "MODEL_SIZE", len(payload))
    monkeypatch.setattr(qwen_alignment, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(qwen_alignment, "MODEL_URL", "https://huggingface.co/example/aligner")
    qwen_alignment._VERIFIED.clear()

    denied_settings = {"qwen_aligner_cache_dir": str(tmp_path / "denied")}
    denied_path = qwen_alignment.cache_path(denied_settings)
    denied_path.parent.mkdir(parents=True)
    denied_path.write_bytes(b"preserve this corrupt file")
    with monkeypatch.context() as patcher:
        patcher.setattr(
            qwen_alignment.shutil,
            "disk_usage",
            lambda _path: pytest.fail("disk-space work ran before authorization"),
        )
        with pytest.raises(ModelAcquisitionDenied):
            qwen_alignment.ensure_model(denied_settings, opener=_unexpected_opener)
        with pytest.raises(ModelAcquisitionDenied):
            qwen_alignment.ensure_model(
                {"qwen_aligner_cache_dir": str(tmp_path / "missing")},
                opener=_unexpected_opener,
            )
    assert denied_path.read_bytes() == b"preserve this corrupt file"

    local_model = tmp_path / "custom.gguf"
    local_model.write_bytes(payload)
    assert (
        qwen_alignment.ensure_model(
            {"qwen_aligner_model_path": str(local_model)}, opener=_unexpected_opener
        )
        == local_model.resolve()
    )

    allowed, authorization_calls, authorize = _authorizer_state()
    opens: list[None] = []

    def opener(*_args: object, **_kwargs: object) -> io.BytesIO:
        opens.append(None)
        return io.BytesIO(payload)

    settings = {"qwen_aligner_cache_dir": str(tmp_path / "downloaded")}
    with model_acquisition_scope(authorize):
        downloaded_path = qwen_alignment.ensure_model(settings, opener=opener)
    assert downloaded_path.read_bytes() == payload
    assert len(opens) == 1
    assert len(authorization_calls) == 1

    assert qwen_alignment.ensure_model(settings, opener=_unexpected_opener) == downloaded_path
    assert len(authorization_calls) == 1

    allowed[0] = False
    with model_acquisition_scope(authorize), pytest.raises(ModelAcquisitionDenied):
        qwen_alignment.ensure_model(
            {"qwen_aligner_cache_dir": str(tmp_path / "revoked")},
            opener=_unexpected_opener,
        )
    assert len(authorization_calls) == 2
    assert len(opens) == 1


def test_crispasr_asset_denies_corrupt_miss_and_reuses_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"tiny pinned CrispASR GGUF"
    asset = crispasr_qwen_assets.PinnedAsset(
        "example/model",
        "abc123",
        "model.gguf",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setitem(crispasr_qwen_assets.ASSETS, "synthetic", asset)

    denied_settings = {"crispasr_cache_dir": str(tmp_path / "denied")}
    denied_path = crispasr_qwen_assets.cache_path(asset, denied_settings)
    denied_path.parent.mkdir(parents=True)
    denied_path.write_bytes(b"preserve this corrupt file")
    with pytest.raises(ModelAcquisitionDenied):
        crispasr_qwen_assets.ensure_asset("synthetic", denied_settings, opener=_unexpected_opener)
    with pytest.raises(ModelAcquisitionDenied):
        crispasr_qwen_assets.ensure_asset(
            "synthetic",
            {"crispasr_cache_dir": str(tmp_path / "missing")},
            opener=_unexpected_opener,
        )
    assert denied_path.read_bytes() == b"preserve this corrupt file"

    allowed, authorization_calls, authorize = _authorizer_state()
    opens: list[None] = []

    def opener(*_args: object, **_kwargs: object) -> io.BytesIO:
        opens.append(None)
        return io.BytesIO(payload)

    settings = {"crispasr_cache_dir": str(tmp_path / "downloaded")}
    with model_acquisition_scope(authorize):
        downloaded_path = crispasr_qwen_assets.ensure_asset("synthetic", settings, opener=opener)
    assert downloaded_path.read_bytes() == payload
    assert len(opens) == 1
    assert len(authorization_calls) == 1

    assert (
        crispasr_qwen_assets.ensure_asset("synthetic", settings, opener=_unexpected_opener)
        == downloaded_path
    )
    assert len(authorization_calls) == 1

    allowed[0] = False
    with model_acquisition_scope(authorize), pytest.raises(ModelAcquisitionDenied):
        crispasr_qwen_assets.ensure_asset(
            "synthetic",
            {"crispasr_cache_dir": str(tmp_path / "revoked")},
            opener=_unexpected_opener,
        )
    assert len(authorization_calls) == 2
    assert len(opens) == 1
