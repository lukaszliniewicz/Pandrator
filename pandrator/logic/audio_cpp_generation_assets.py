"""On-demand verified asset cache for the audio.cpp generation registry."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from . import audio_cpp_assets as assets
from .audio_cpp_generation_models import AssetFile, ModelSpec, get_model

ProgressCallback = Callable[[str, int, int], None]


def package_path(model_id: str, settings: dict) -> Path:
    """Return the selected package's private cache directory."""
    get_model(model_id)
    return assets.cache_root(settings) / model_id


def file_path(model_id: str, relative_path: str, settings: dict) -> Path:
    """Return one pinned package path after checking membership in its manifest."""
    spec = get_model(model_id)
    if relative_path not in {item.path for item in spec.files}:
        raise assets.AudioAssetsError("Requested package path is not in its pinned manifest.")
    package = package_path(model_id, settings)
    if package.is_symlink():
        raise assets.AudioAssetsError("Model package cache directory must not be a symlink.")
    target = package / Path(relative_path)
    try:
        target.resolve(strict=False).relative_to(package.resolve(strict=False))
    except ValueError as error:
        raise assets.AudioAssetsError("Pinned package path escapes its cache directory.") from error
    return target


def model_path(model_id: str, settings: dict) -> Path:
    """Return the direct GGUF file or package directory expected by the CLI."""
    spec = get_model(model_id)
    if spec.is_directory_model:
        package = package_path(model_id, settings)
        if package.is_symlink():
            raise assets.AudioAssetsError("Model package cache directory must not be a symlink.")
        return package
    return file_path(model_id, spec.model_path_asset.path, settings)


def _manager_source(spec: ModelSpec, relative_path: str) -> Path | None:
    """Find one contained Manager file; the shared verifier checks its digest."""
    for models_root in assets._manager_models_dirs():
        try:
            root = models_root.resolve()
            candidate = models_root / spec.target_directory / Path(relative_path)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if (
                not candidate.is_symlink()
                and resolved.is_file()
                and resolved.stat().st_size
                == next(item.size_bytes for item in spec.files if item.path == relative_path)
            ):
                return resolved
        except (OSError, ValueError, StopIteration):
            continue
    return None


def is_cached(model_id: str, settings: dict) -> bool:
    """Read-only size check for the complete selected package; performs no hashing."""
    spec = get_model(model_id)
    try:
        return all(
            (path := file_path(model_id, item.path, settings)).is_file()
            and not path.is_symlink()
            and path.stat().st_size == item.size_bytes
            for item in spec.files
        )
    except (OSError, assets.AudioAssetsError):
        return False


def _file_spec(model: ModelSpec, item: AssetFile) -> dict[str, str | int]:
    return {
        "id": f"{model.id}-{item.path.replace('/', '-')}",
        "label": f"{model.label} ({item.path})",
        "url": (
            f"https://huggingface.co/{model.repository}/resolve/"
            f"{model.revision}/{quote(item.source_path, safe='/')}"
        ),
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
    }


def ensure_model(
    model_id: str,
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
    *,
    opener: Callable = urlopen,
) -> Path:
    """Install only the selected package, verifying every manifest file first."""
    spec = get_model(model_id)
    package = package_path(model_id, settings)
    package.parent.mkdir(parents=True, exist_ok=True)
    total = spec.download_bytes
    completed = 0
    with assets.asset_package_lock(package, model_id, cancel_event):
        for item in spec.files:
            assets.check_cancelled(cancel_event)
            destination = file_path(model_id, item.path, settings)
            pinned_file = _file_spec(spec, item)
            file_progress: ProgressCallback | None = None
            if progress is not None:

                def report_file_progress(
                    stage: str, done: int, _file_total: int, *, base: int = completed
                ) -> None:
                    progress(stage, min(total, base + done), total)

                file_progress = report_file_progress

            assets._ensure_trusted_file_locked(
                pinned_file,
                destination,
                cancel_event,
                file_progress,
                opener=opener,
                manager_source=_manager_source(spec, item.path),
            )
            completed += item.size_bytes

        # The package lock stays held until every file has passed the shared
        # size/SHA verifier, so a caller never sees an installable partial model.
        for item in spec.files:
            if not assets._verified_cached(
                file_path(model_id, item.path, settings),
                _file_spec(spec, item),
                cancel_event,
            ):
                raise assets.AudioAssetsError(
                    f"Installed package {model_id!r} failed its final file verification."
                )
    return model_path(model_id, settings).resolve()


__all__ = [
    "ensure_model",
    "file_path",
    "is_cached",
    "model_path",
    "package_path",
]
