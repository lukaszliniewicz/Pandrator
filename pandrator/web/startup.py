"""Non-blocking application-start maintenance."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from pandrator.runtime import DataPaths

from .database import Database
from .maintenance import apply_retention
from .models import AppSetting
from .uploads import ChunkUploadService


@dataclass(slots=True)
class StartupMaintenance:
    database: Database
    paths: DataPaths
    uploads: ChunkUploadService
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    result: dict[str, Any] | None = None
    error: str | None = None
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _completed: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _run_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.run,
            name="pandrator-startup-maintenance",
            daemon=True,
        )
        self._thread.start()

    def run(self) -> dict[str, Any]:
        with self._run_lock:
            if self._completed.is_set():
                return dict(self.result or {})
            errors: list[str] = []
            try:
                value: dict[str, Any] = {}
                try:
                    with self.database.session() as session:
                        record = session.get(AppSetting, "web.preferences")
                        value = (
                            record.value_json
                            if record and isinstance(record.value_json, dict)
                            else {}
                        )
                except Exception as error:
                    errors.append(f"settings:{type(error).__name__}")
                    self.logger.exception(
                        "Background maintenance could not read preferences"
                    )
                try:
                    retention_days = int(value.get("retention_days", 30))
                except (TypeError, ValueError):
                    retention_days = 30

                result: dict[str, Any] = {}
                try:
                    result["retention"] = apply_retention(
                        self.database,
                        self.paths,
                        retention_days,
                    )
                except Exception as error:
                    errors.append(f"retention:{type(error).__name__}")
                    self.logger.exception("Background retention maintenance failed")
                try:
                    result["expired_uploads"] = self.uploads.cleanup_expired()
                except Exception as error:
                    errors.append(f"uploads:{type(error).__name__}")
                    self.logger.exception("Background upload maintenance failed")
                self.result = result
                self.error = ",".join(errors) or None
                return dict(result)
            finally:
                self._completed.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._completed.wait(timeout)

    @property
    def completed(self) -> bool:
        return self._completed.is_set()
