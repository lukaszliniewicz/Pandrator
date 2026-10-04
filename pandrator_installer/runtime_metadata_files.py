"""Snapshot comparison and a shared guard for participating metadata writers."""

import errno
import json
import math
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_METADATA_GATE = threading.Lock()


@dataclass(frozen=True, slots=True)
class RuntimeMetadataSnapshot:
    path: Path
    payload: object
    data: bytes
    version: tuple[int, int, int, int, int]
    parse_error: ValueError | None = None


def _version(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_runtime_metadata(path: str | os.PathLike[str]) -> RuntimeMetadataSnapshot | None:
    """Capture stable readable bytes; retain malformed JSON as a payload of None."""
    metadata_path = Path(path)
    try:
        with metadata_path.open("rb") as handle:
            before = _version(os.fstat(handle.fileno()))
            data = handle.read()
            after = _version(os.fstat(handle.fileno()))
    except OSError:
        return None
    if before != after:
        return None
    parse_error: ValueError | None = None
    try:
        payload: object = json.loads(data.decode("utf-8"))
    except ValueError as error:
        payload = None
        parse_error = error
    return RuntimeMetadataSnapshot(metadata_path, payload, data, after, parse_error)


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def runtime_metadata_guard(
    root: str | os.PathLike[str], *, timeout: float = 10.0
) -> Iterator[None]:
    """Serialize participating writers with one deadline for thread and OS waits."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Runtime metadata guard timeout must be finite and nonnegative.")
    deadline = time.monotonic() + timeout
    if not _METADATA_GATE.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("Timed out waiting for the runtime metadata guard.")
    try:
        with (Path(root) / ".runtime-metadata.guard").open("a+b") as handle:
            while True:
                try:
                    _try_lock(handle.fileno())
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "Timed out waiting for the runtime metadata guard."
                        ) from error
                    time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                _unlock(handle.fileno())
    finally:
        _METADATA_GATE.release()


def runtime_metadata_matches(snapshot: RuntimeMetadataSnapshot) -> bool:
    """Compare identity and bytes; this does not exclude uncooperative writers."""
    current = read_runtime_metadata(snapshot.path)
    return (
        current is not None
        and current.version == snapshot.version
        and current.data == snapshot.data
    )


def discard_runtime_metadata(snapshot: RuntimeMetadataSnapshot) -> bool:
    """Discard a matching snapshot under the participating-writer guard.

    Earlier uncooperative replacements are detected. An uncooperative writer
    can still replace the file between the final comparison and unlink.
    """
    with runtime_metadata_guard(snapshot.path.parent):
        if not runtime_metadata_matches(snapshot):
            return False
        try:
            snapshot.path.unlink()
        except FileNotFoundError:
            return False
        return True
