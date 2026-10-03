"""Bridge worker termination signals to an ordinary shutdown monitor."""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Thread
from types import FrameType


@contextmanager
def worker_termination(stop: Callable[[], None]) -> Iterator[Callable[[], bool]]:
    """Request worker stop outside the SIGTERM callback and restore its handler."""

    requested = False
    finished = Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal requested
        requested = True

    def termination_requested() -> bool:
        return requested

    def monitor_termination() -> None:
        while not finished.wait(0.05):
            if requested:
                stop()
                return

    monitor = Thread(
        target=monitor_termination,
        name="pandrator-worker-termination",
        daemon=True,
    )
    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        monitor.start()
        yield termination_requested
    finally:
        finished.set()
        try:
            signal.signal(signal.SIGTERM, previous)
        finally:
            if monitor.ident is not None:
                monitor.join(timeout=1.0)
