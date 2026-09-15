"""Reuse immutable, digest-verified model payloads between rollback-safe slots."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Callable

from .audiocpp import AudioCppModelPackage


def reuse_verified_package(
    active_slot: Path | None,
    models_root: Path,
    package: AudioCppModelPackage,
    digest: Callable[[Path], str],
    check_cancelled: Callable[[], None],
) -> str | None:
    """Link complete verified packages, never expose shared files to the installer.

    Metadata stays private to each slot. A partial or corrupt package is not
    reused at all: the installer gets an empty destination and cannot truncate
    a payload still used by the active/rollback slot. Cross-device filesystems
    fall back to a private copy, still avoiding a network download.
    """
    if active_slot is None:
        return None
    source_root = active_slot / "models"
    sources = package.required_paths(source_root)
    for source, expected in zip(sources, package.sha256, strict=True):
        check_cancelled()
        try:
            source.resolve().relative_to(active_slot.resolve())
            if not source.is_file() or digest(source) != expected:
                return None
        except (OSError, ValueError):
            return None

    mode = "hardlink"
    for source, target in zip(sources, package.required_paths(models_root), strict=True):
        check_cancelled()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source.resolve(), target)
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES,
                                   errno.ENOTSUP, errno.EMLINK}:
                raise
            # Do not overwrite an existing path, especially a shared inode.
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            mode = "copy"
    return mode
