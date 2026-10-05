"""Strict XTTS model identifiers and exclusive directory publication."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import unicodedata
from pathlib import Path, PureWindowsPath

_XTTS_MODEL_DISCOVERY_IGNORE_PARTS = {".downloads", "__pycache__"}
_XTTS_WINDOWS_RESERVED_PARTS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_XTTS_INVALID_MODEL_ID_CHARS = set('<>:"|?*\\')


def validate_training_model_name(model_name: str) -> tuple[str, ...]:
    """Validate a relative nested identifier without touching the filesystem."""
    candidate = str(model_name or "")
    if not candidate or candidate != candidate.strip():
        raise ValueError(
            "Model name must be a non-empty relative identifier without surrounding spaces."
        )
    if any(unicodedata.category(character).startswith("C") for character in candidate):
        raise ValueError("Model name must not contain control characters.")
    if "\\" in candidate or candidate.startswith("/") or PureWindowsPath(candidate).is_absolute():
        raise ValueError("Model name must be a relative slash-separated path.")
    parts = candidate.split("/")
    if any(not part for part in parts):
        raise ValueError("Model name must not contain empty path parts.")
    for part in parts:
        windows_basename = part.split(".", maxsplit=1)[0].casefold()
        if (
            part in {".", ".."}
            or part.startswith(".")
            or part != part.rstrip(". ")
            or part in _XTTS_MODEL_DISCOVERY_IGNORE_PARTS
            or windows_basename in _XTTS_WINDOWS_RESERVED_PARTS
            or any(character in _XTTS_INVALID_MODEL_ID_CHARS for character in part)
        ):
            raise ValueError("Model name contains a reserved, hidden, or unsafe path part.")

    return tuple(parts)


def resolve_training_model_target(model_name: str, models_dir: Path | str) -> tuple[Path, Path]:
    parts = validate_training_model_name(model_name)
    root = Path(models_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    target = root.joinpath(*parts)
    try:
        target.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("Model name must resolve inside the XTTS model root.") from error
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Model name must not pass through a symlink.")
    return root, target


def promote_training_model_directory(staging: Path, target: Path) -> None:
    """Rename a complete directory while refusing every existing destination."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.rename(staging, target)
        return
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Exclusive model directory promotion is unavailable.")
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Exclusive model directory promotion is unavailable.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
