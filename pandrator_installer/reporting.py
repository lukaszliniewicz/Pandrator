"""Progress reporting adapters for GUI workers and headless runs."""

from __future__ import annotations

import logging
from typing import Protocol


class Reporter(Protocol):
    def progress(self, value: float) -> None: ...

    def status(self, text: str) -> None: ...


class NullReporter:
    def progress(self, value: float) -> None:
        return

    def status(self, text: str) -> None:
        logging.info(str(text))


class HeadlessReporter:
    def progress(self, value: float) -> None:
        try:
            percentage = max(0, min(100, int(float(value) * 100)))
        except (TypeError, ValueError):
            return
        logging.info("Progress: %s%%", percentage)

    def status(self, text: str) -> None:
        message = str(text)
        logging.info(message)
        print(message)


class SignalReporter:
    def __init__(self, progress_signal, status_signal):
        self._progress_signal = progress_signal
        self._status_signal = status_signal

    def progress(self, value: float) -> None:
        self._progress_signal.emit(value)

    def status(self, text: str) -> None:
        self._status_signal.emit(str(text))
