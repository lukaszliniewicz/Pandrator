"""Nonblocking native locks for session upload filesystem activity."""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pandrator.runtime import DataPaths


class UploadBusy(ValueError):
    """Another process or request is already operating on this upload owner."""


def _lock_path(paths: DataPaths, session_id: str | None, upload_id: str | None) -> Path:
    for value in (session_id, upload_id):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("Upload activity owner is not a safe identifier.")
    identifier = session_id if session_id is not None else upload_id
    if identifier is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
        raise ValueError("Upload activity owner is not a safe identifier.")
    directory = paths.temporary / "upload-locks"
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("Upload activity lock root is not a canonical absolute path.")
    prefix = "session" if session_id is not None else "upload"
    return directory / f"{prefix}-{identifier}.lock"


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd:
        descriptors = []
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            parent = os.open(path.anchor, directory_flags)
            descriptors.append(parent)
            for part in path.parent.parts[1:]:
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
                if not stat.S_ISDIR(os.stat(part, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise ValueError("Upload activity lock ancestor is a symlink or non-directory.")
                parent = os.open(part, directory_flags, dir_fd=parent)
                descriptors.append(parent)
            try:
                mode = os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode
            except FileNotFoundError:
                mode = None
            if mode is not None and not stat.S_ISREG(mode):
                raise ValueError("Upload activity lock is a symlink or nonregular file.")
            return os.open(path.name, flags, 0o600, dir_fd=parent)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    directory = path.parent
    current = Path(directory.anchor)
    for part in directory.parts[1:]:
        current /= part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("Upload activity lock ancestor is a symlink or non-directory.")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        raise ValueError("Upload activity lock is a symlink or nonregular file.")
    return os.open(path, flags, 0o600)


@contextmanager
def upload_activity(
    paths: DataPaths, *, session_id: str | None = None, upload_id: str | None = None
) -> Iterator[None]:
    """Keep lockfiles permanently; replacing an inode would defeat exclusion."""
    path = _lock_path(paths, session_id, upload_id)
    descriptor = _open_lock(path)
    acquired = False
    try:
        opened = os.fstat(descriptor)
        visible = path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(visible.st_mode)
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)):
            raise ValueError("Upload activity lock path changed or is nonregular.")
        try:
            if sys.platform == "win32":
                import msvcrt

                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise UploadBusy("Upload filesystem activity is already in progress.") from error
            raise
        acquired = True
        visible = path.lstat()
        if (not stat.S_ISREG(visible.st_mode)
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)):
            raise ValueError("Upload activity lock path changed while acquiring it.")
        yield
    finally:
        try:
            if acquired:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
