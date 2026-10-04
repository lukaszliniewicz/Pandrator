"""Shared runtime admission and exclusive installation lifecycle guard."""

import errno
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .file_locking import try_lifecycle_file_lock, unlock_lifecycle_file

LIFECYCLE_GUARD_NAME = ".lifecycle-operation.guard"


class LifecycleBusy(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _HeldLifecycleGuard:
    descriptor: int
    shared: bool


class _ThreadLifecycleGuards(threading.local):
    def __init__(self) -> None:
        self.held: dict[Path, _HeldLifecycleGuard] = {}


_THREAD_GUARDS = _ThreadLifecycleGuards()


@contextmanager
def installation_lifecycle_guard(root: str | os.PathLike[str], *, shared: bool) -> Iterator[None]:
    resolved_root = Path(root).expanduser().resolve()
    held = _THREAD_GUARDS.held.get(resolved_root)
    if held is not None:
        if held.shared != shared:
            raise LifecycleBusy(
                "The installation is busy. Stop its runtime or wait for the current lifecycle operation to finish."
            )
        yield
        return

    resolved_root.mkdir(parents=True, exist_ok=True)
    with (resolved_root / LIFECYCLE_GUARD_NAME).open("a+b") as handle:
        descriptor = handle.fileno()
        try:
            try_lifecycle_file_lock(descriptor, shared=shared)
        except OSError as error:
            if (os.name == "nt" and error.winerror == 33) or (
                os.name != "nt" and error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
            ):
                raise LifecycleBusy(
                    "The installation is busy. Stop its runtime or wait for the current lifecycle operation to finish."
                ) from error
            raise
        _THREAD_GUARDS.held[resolved_root] = _HeldLifecycleGuard(descriptor, shared)
        try:
            yield
        finally:
            try:
                unlock_lifecycle_file(descriptor)
            finally:
                del _THREAD_GUARDS.held[resolved_root]
