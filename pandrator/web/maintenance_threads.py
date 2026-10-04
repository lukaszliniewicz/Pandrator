"""Explicit callback admission for maintenance thread resource ownership."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Literal


class MaintenanceThread(threading.Thread):
    def __init__(self, *, target: Callable[[], object], name: str, daemon: bool = True) -> None:
        self._work: Callable[[], object] | None = target
        self._work_lock = threading.Lock()
        self._admission = threading.Event()
        self._work_state: Literal["pending", "admitted", "running", "finished", "cancelled"] = (
            "pending"
        )
        self._start_requested = False
        super().__init__(target=self._run_work, name=name, daemon=daemon)

    def start(self) -> None:
        with self._work_lock:
            if self._start_requested:
                raise RuntimeError("threads can only be started once")
            self._start_requested = True
        try:
            super().start()
            self._admit_work()
        except BaseException:
            self._cancel_pending_work()
            raise

    def _admit_work(self) -> None:
        with self._work_lock:
            if self._work_state == "pending":
                self._work_state = "admitted"
                self._admission.set()

    def _cancel_pending_work(self) -> bool:
        with self._work_lock:
            if self._work_state in ("pending", "admitted"):
                self._work_state = "cancelled"
                self._work = None
                self._admission.set()
                return True
            return self._work_state == "cancelled"

    def _run_work(self) -> None:
        self._admission.wait()
        with self._work_lock:
            if self._work_state != "admitted":
                return
            work = self._work
            self._work_state = "running"
        try:
            assert work is not None
            work()
        finally:
            with self._work_lock:
                self._work = None
                self._work_state = "finished"

    def finish(self, *, timeout: float | None = 2) -> bool:
        """Confirm the callback has finished or can no longer begin.

        A cancelled native wrapper may exit later, without access to the callback.
        """
        if self._cancel_pending_work():
            return True
        if self is threading.current_thread():
            return False
        self.join(timeout=timeout)
        return not self.is_alive()
