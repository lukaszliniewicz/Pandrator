"""Pinned, verified Qwen GGUF acquisition for the CrispASR cache."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..cancellable_process import ProcessCancelled


@dataclass(frozen=True)
class PinnedAsset:
    repository: str
    revision: str
    filename: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return (
            f"https://huggingface.co/{self.repository}/resolve/{self.revision}/"
            f"{quote(self.filename)}"
        )


ASSETS = {
    "qwen3_asr_0_6b": PinnedAsset(
        "cstr/qwen3-asr-0.6b-GGUF",
        "f5814fb07a955e84b4474133002cd2bbc747c4b9",
        "qwen3-asr-0.6b-q8_0.gguf",
        1006809760,
        "f547589d5ca582e093b2d3312ad9ff13b609b43d413f972c0e92b823dde70a00",
    ),
    "qwen3_asr_1_7b": PinnedAsset(
        "cstr/qwen3-asr-1.7b-GGUF",
        "dc60551b654abd7ba71ca0c19c3f6ba7e85c9242",
        "qwen3-asr-1.7b-q8_0.gguf",
        2506723200,
        "9851ab996591a2d0cb0efb216002764b509c86bd40c95e613d7b65b8e69c8a6e",
    ),
    "qwen3_forced_aligner": PinnedAsset(
        "cstr/qwen3-forced-aligner-0.6b-GGUF",
        "d75b1dba5954f9ce25a7432ae38dc24813dbbdac",
        "qwen3-forced-aligner-0.6b-q8_0.gguf",
        985594624,
        "539df5dd0fe1721e378ac13bfac9a26b1260dafb62d892c518c1f21244762636",
    ),
}


def cache_path(asset: PinnedAsset, settings: dict[str, Any]) -> Path:
    configured = str(
        settings.get("crispasr_cache_dir") or os.environ.get("CRISPASR_CACHE_DIR") or ""
    ).strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache" / "crispasr"
    return root / asset.filename


def _check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Qwen3 ASR model acquisition was canceled.")


def _valid(path: Path, asset: PinnedAsset, event: threading.Event | None) -> bool:
    if not path.is_file() or path.stat().st_size != asset.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _check_cancelled(event)
            digest.update(chunk)
    return digest.hexdigest() == asset.sha256


def cached_path(
    key: str, settings: dict[str, Any], cancel_event: threading.Event | None = None
) -> Path | None:
    asset = ASSETS[key]
    path = cache_path(asset, settings)
    return path if _valid(path, asset, cancel_event) else None


def ensure_asset(
    key: str,
    settings: dict[str, Any],
    cancel_event: threading.Event | None = None,
    progress: Callable[..., None] | None = None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    """Stream to a sibling temporary file and publish only a verified GGUF."""

    asset = ASSETS[key]
    _check_cancelled(cancel_event)
    existing = cached_path(key, settings, cancel_event)
    if existing is not None:
        return existing
    target = cache_path(asset, settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    headers = {"Accept": "application/octet-stream", "User-Agent": "Pandrator/Qwen3-ASR"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = Request(asset.url, headers=headers)
        with opener(request, timeout=60) as response, tempfile.NamedTemporaryFile(
            mode="wb", prefix=f"{asset.filename}.part-", dir=target.parent, delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            digest = hashlib.sha256()
            downloaded = 0
            while True:
                _check_cancelled(cancel_event)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > asset.size:
                    raise ValueError(f"Qwen asset {key} exceeded its pinned size.")
                stream.write(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress("download", downloaded, asset.size)
        _check_cancelled(cancel_event)
        if downloaded != asset.size or digest.hexdigest() != asset.sha256:
            raise ValueError(f"Qwen asset {key} failed pinned size/SHA-256 verification.")
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
