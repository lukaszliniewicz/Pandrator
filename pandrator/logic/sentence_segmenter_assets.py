"""Local-cache discovery and authorized acquisition for wtpsplit-lite assets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .model_acquisition import require_model_acquisition

__all__ = [
    "WTPSPLIT_MODEL",
    "SentenceSegmenterAssets",
    "resolve_sentence_segmenter_assets",
]

WTPSPLIT_MODEL = "sat-12l-sm"
_MODEL_REPOSITORY = "segment-any-text/sat-12l-sm"
_TOKENIZER_REPOSITORY = "facebookAI/xlm-roberta-base"
_MODEL_FILENAME = "model_optimized.onnx"
_CONFIG_FILENAME = "config.json"
_TOKENIZER_FILENAME = "tokenizer.json"
_INCOMPLETE_CACHE_MESSAGE = "The sentence segmentation model cache is incomplete."


def try_to_load_from_cache(*, repo_id: str, filename: str) -> object:
    from huggingface_hub import _CACHED_NO_EXIST
    from huggingface_hub import try_to_load_from_cache as load_from_cache

    cached = load_from_cache(repo_id=repo_id, filename=filename)
    return None if cached is _CACHED_NO_EXIST else cached


def hf_hub_download(*, repo_id: str, filename: str, revision: str | None = None) -> str:
    from huggingface_hub import hf_hub_download as download

    return download(repo_id=repo_id, filename=filename, revision=revision)


@dataclass(frozen=True, slots=True)
class SentenceSegmenterAssets:
    model_directory: Path
    tokenizer_path: Path


def _validated_file(value: object, filename: str) -> Path:
    if not isinstance(value, (str, PathLike)):
        raise RuntimeError(_INCOMPLETE_CACHE_MESSAGE)
    try:
        path = Path(value)
    except (OSError, TypeError, ValueError):
        raise RuntimeError(_INCOMPLETE_CACHE_MESSAGE) from None
    try:
        valid = (
            path.is_absolute()
            and path.name == filename
            and path.is_file()
            and path.stat().st_size > 0
        )
    except OSError:
        valid = False
    if not valid:
        raise RuntimeError(_INCOMPLETE_CACHE_MESSAGE)
    return path


def _cached_file(repository: str, filename: str) -> Path | None:
    cached = try_to_load_from_cache(repo_id=repository, filename=filename)
    if cached is None:
        return None
    return _validated_file(cached, filename)


def _model_snapshot(onnx_path: Path) -> tuple[Path, str]:
    snapshot_directory = onnx_path.parent
    commit = snapshot_directory.name
    if (
        snapshot_directory.parent.name != "snapshots"
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None
    ):
        raise RuntimeError(_INCOMPLETE_CACHE_MESSAGE)
    return snapshot_directory, commit


def resolve_sentence_segmenter_assets() -> SentenceSegmenterAssets:
    """Return a complete local bundle, acquiring only after per-file approval."""
    onnx_path = _cached_file(_MODEL_REPOSITORY, _MODEL_FILENAME)
    if onnx_path is None:
        require_model_acquisition()
        onnx_path = _validated_file(
            hf_hub_download(
                repo_id=_MODEL_REPOSITORY,
                filename=_MODEL_FILENAME,
            ),
            _MODEL_FILENAME,
        )

    model_directory, commit = _model_snapshot(onnx_path)
    config_path = model_directory / _CONFIG_FILENAME
    if config_path.exists():
        config_path = _validated_file(config_path, _CONFIG_FILENAME)
    else:
        require_model_acquisition()
        downloaded_config = _validated_file(
            hf_hub_download(
                repo_id=_MODEL_REPOSITORY,
                filename=_CONFIG_FILENAME,
                revision=commit,
            ),
            _CONFIG_FILENAME,
        )
        if downloaded_config.parent != model_directory:
            raise RuntimeError(_INCOMPLETE_CACHE_MESSAGE)
        config_path = downloaded_config

    tokenizer_path = _cached_file(_TOKENIZER_REPOSITORY, _TOKENIZER_FILENAME)
    if tokenizer_path is None:
        require_model_acquisition()
        tokenizer_path = _validated_file(
            hf_hub_download(
                repo_id=_TOKENIZER_REPOSITORY,
                filename=_TOKENIZER_FILENAME,
            ),
            _TOKENIZER_FILENAME,
        )

    return SentenceSegmenterAssets(
        model_directory=model_directory,
        tokenizer_path=tokenizer_path,
    )
