"""Pinned YuE2 instrumental LoRA adapter for audio.cpp generation."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from urllib.request import urlopen

from . import audio_cpp_assets as assets

ADAPTER_ID = "yue2_instrumental_ar_v3abc_bf16"
REPOSITORY = "Mothersuperior/YuE2-instrumental-cot-full-loras"
REVISION = "947f2f4b28978b2b6c3e316e6a87925c76bf3c4b"
FILENAME = "ar_lora_inst_v3abc.bf16.safetensors"
SIZE_BYTES = 139502088
SHA256 = "e408fd3148b75b1165f7ddbf63db575d83bb6402a0b5f876fcb767dbcb2c5414"
LICENSE = "CC-BY-NC-4.0"
SOURCE_URL = f"https://huggingface.co/{REPOSITORY}"


def identity() -> dict[str, str | int]:
    """Return the immutable identity used to describe this adapter."""
    return {
        "id": ADAPTER_ID,
        "repository": REPOSITORY,
        "revision": REVISION,
        "filename": FILENAME,
        "size_bytes": SIZE_BYTES,
        "sha256": SHA256,
        "license": LICENSE,
    }


def _trusted_file_spec() -> dict[str, str | int]:
    """Assemble the fixed source and digest spec consumed by shared assets."""
    return {
        "id": ADAPTER_ID,
        "label": "YuE2 instrumental AR LoRA adapter",
        "url": f"{SOURCE_URL}/resolve/{REVISION}/{FILENAME}",
        "size_bytes": SIZE_BYTES,
        "sha256": SHA256,
    }


def cache_path(settings: dict) -> Path:
    """Return this adapter's shared cache path without creating anything."""
    package = assets.cache_root(settings) / ADAPTER_ID
    if package.is_symlink():
        raise assets.AudioAssetsError("Adapter package cache directory must not be a symlink.")

    destination = package / FILENAME
    if destination.is_symlink():
        raise assets.AudioAssetsError("Adapter cache file must not be a symlink.")
    try:
        destination.resolve(strict=False).relative_to(package.resolve(strict=False))
    except ValueError as error:
        raise assets.AudioAssetsError(
            "Adapter cache file escapes its package directory."
        ) from error
    return destination


def ensure_adapter(
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: assets.ProgressCallback | None = None,
    *,
    opener: Callable = urlopen,
) -> Path:
    """Download or reuse the pinned adapter in its shared verified cache."""
    assets.check_cancelled(cancel_event)
    destination = cache_path(settings)
    package = destination.parent
    with assets.asset_package_lock(package, ADAPTER_ID, cancel_event):
        # Recheck after acquiring the package lock in case another process
        # changed the package or destination while this call was waiting.
        destination = cache_path(settings)
        return assets._ensure_trusted_file_locked(
            _trusted_file_spec(),
            destination,
            cancel_event,
            progress,
            opener=opener,
            manager_source=None,
        )
