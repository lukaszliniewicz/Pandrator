"""Bounded, identity-checked speech-reference files and private staging."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import uuid
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from pandrator.runtime import DataPaths

from .speech_input_contracts import FileIdentity, SpeechInputError, SpeechInputFile


def _parts(relative_path: str) -> tuple[str, ...]:
    if type(relative_path) is not str or not relative_path or "\x00" in relative_path:
        raise SpeechInputError("Unsafe speech input path.")
    posix, windows = PurePosixPath(relative_path), PureWindowsPath(relative_path)
    if (
        posix.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
        or "\\" in relative_path
        or "." in relative_path.split("/")
    ):
        raise SpeechInputError("Unsafe speech input path.")
    return posix.parts


@contextmanager
def _parent(paths: DataPaths, relative_path: str) -> Iterator[tuple[int | None, Path, str]]:
    parts = _parts(relative_path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    handles: list[int] = []
    path = paths.root
    try:
        if os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd:
            fd = os.open(path, flags)
            handles.append(fd)
            for part in parts[:-1]:
                fd = os.open(part, flags, dir_fd=fd)
                handles.append(fd)
                path = path / part
            yield fd, path, parts[-1]
        else:
            for part in parts[:-1]:
                path = path / part
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise SpeechInputError("Unsafe speech input directory.")
            yield None, path, parts[-1]
    except OSError as error:
        raise SpeechInputError("Speech input file is unavailable.") from error
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _identity(info: os.stat_result) -> FileIdentity:
    if not stat.S_ISREG(info.st_mode):
        raise SpeechInputError("Speech input must be a regular file.")
    return FileIdentity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_identity(paths: DataPaths, relative_path: str) -> FileIdentity:
    with _parent(paths, relative_path) as (fd, parent, name):
        info = (
            os.stat(name, dir_fd=fd, follow_symlinks=False)
            if fd is not None
            else (parent / name).lstat()
        )
        return _identity(info)


def read_verified_file(
    paths: DataPaths,
    relative_path: str,
    *,
    limit: int,
    expected_hash: str | None = None,
    expected_identity: FileIdentity | None = None,
) -> tuple[bytes, str, FileIdentity]:
    """Read an opened regular inode, then prove its path and metadata still agree."""
    with _parent(paths, relative_path) as (parent_fd, parent, name):
        info = (
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else (parent / name).lstat()
        )
        identity = _identity(info)
        if identity.size > limit or (
            expected_identity is not None and identity != expected_identity
        ):
            raise SpeechInputError("Speech input file is oversized or changed.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = (
            os.open(name, flags, dir_fd=parent_fd)
            if parent_fd is not None
            else os.open(parent / name, flags)
        )
        with os.fdopen(fd, "rb") as source:
            if _identity(os.fstat(source.fileno())) != identity:
                raise SpeechInputError("Speech input file changed before reading.")
            data = source.read(limit + 1)
            if len(data) > limit or _identity(os.fstat(source.fileno())) != identity:
                raise SpeechInputError("Speech input file changed while reading.")
        # Rewalk the original path as well as the pinned parent descriptor.
        if file_identity(paths, relative_path) != identity:
            raise SpeechInputError("Speech input path changed while reading.")
    digest = hashlib.sha256(data).hexdigest()
    if expected_hash and digest != expected_hash:
        raise SpeechInputError("Speech input bytes do not match their recorded hash.")
    return data, digest, identity


def validate_wav(data: bytes) -> None:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            if expected <= 0 or len(audio.readframes(audio.getnframes())) != expected:
                raise SpeechInputError("Speech reference WAV is empty or truncated.")
    except (wave.Error, EOFError) as error:
        raise SpeechInputError("Speech reference must be normalized WAV audio.") from error


def create_staging_directory(paths: DataPaths) -> tuple[str, tuple[int, int]]:
    # The already managed artifacts directory must exist; never follow a symlink.
    relative = f"artifacts/speech-input-{uuid.uuid4()}"
    with _parent(paths, relative) as (fd, parent, name):
        if fd is not None:
            os.mkdir(name, mode=0o700, dir_fd=fd)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        else:
            (parent / name).mkdir(mode=0o700)
            info = (parent / name).lstat()
    return relative, (info.st_dev, info.st_ino)


def stage_file(paths: DataPaths, directory: str, data: bytes, suffix: str) -> SpeechInputFile:
    identifier = str(uuid.uuid4())
    relative = f"{directory}/{identifier}{suffix}"
    with _parent(paths, relative) as (parent_fd, parent, name):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = (
            os.open(name, flags, 0o600, dir_fd=parent_fd)
            if parent_fd is not None
            else os.open(parent / name, flags, 0o600)
        )
        info = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                info = os.fstat(output.fileno())
        except BaseException:
            _remove_owned_file(paths, relative, (info.st_dev, info.st_ino))
            raise
    return SpeechInputFile(
        identifier, relative, hashlib.sha256(data).hexdigest(), len(data), _identity(info), True
    )


def _remove_owned_file(paths: DataPaths, relative_path: str, identity: tuple[int, int]) -> None:
    try:
        with _parent(paths, relative_path) as (fd, parent, name):
            info = (
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                if fd is not None
                else (parent / name).lstat()
            )
            if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                if fd is not None:
                    os.unlink(name, dir_fd=fd)
                else:
                    (parent / name).unlink()
    except (SpeechInputError, OSError):
        pass


def cleanup_files(
    paths: DataPaths,
    files: tuple[SpeechInputFile, ...],
    directory: str | None,
    directory_identity: tuple[int, int] | None,
) -> None:
    # A replaced staging directory is not ours, even if it contains a moved inode.
    if directory is not None:
        try:
            with _parent(paths, directory) as (fd, parent, name):
                info = (
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if fd is not None
                    else (parent / name).lstat()
                )
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != directory_identity
                ):
                    return
        except (SpeechInputError, OSError):
            return
    for item in files:
        if item.owned:
            _remove_owned_file(
                paths, item.relative_path, (item.identity.device, item.identity.inode)
            )
    if directory is not None:
        try:
            with _parent(paths, directory) as (fd, parent, name):
                if fd is not None:
                    os.rmdir(name, dir_fd=fd)
                else:
                    (parent / name).rmdir()
        except (SpeechInputError, OSError):
            pass
