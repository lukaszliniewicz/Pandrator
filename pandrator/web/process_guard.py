"""Advisory process-presence records for visible worker scaling."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a core dependency
    psutil = None


def _process_is_current(payload: dict[str, Any]) -> bool:
    """Return whether a marker still identifies the same live process."""
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            if not psutil.pid_exists(pid):
                return False
            expected_create_time = payload.get("process_create_time")
            if expected_create_time is None:
                return True
            actual_create_time = psutil.Process(pid).create_time()
            return abs(float(expected_create_time) - float(actual_create_time)) <= 0.01
        except (OSError, TypeError, ValueError, psutil.Error):
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class WorkerPresence:
    """Own an advisory worker marker or report the live process that owns it.

    Queue operations are safe across workers. The marker remains advisory so an
    operator can distinguish intentional scaling from an accidental duplicate
    launch without changing launch semantics.
    """

    def __init__(self, path: str | os.PathLike[str], worker_id: str):
        self.path = Path(path)
        self.worker_id = str(worker_id)
        self.token = uuid.uuid4().hex
        self.acquired = False
        self.conflict: dict[str, Any] | None = None

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self.token,
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "created_at": time.time(),
        }
        if psutil is not None:
            try:
                process = psutil.Process(os.getpid())
                payload["process_create_time"] = process.create_time()
                payload["executable"] = process.exe()
            except (OSError, psutil.Error):
                pass
        return payload

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def acquire(self) -> dict[str, Any] | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._payload()
        for _attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                current = self._read(self.path)
                if _process_is_current(current):
                    self.conflict = current
                    return current
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning("Could not replace stale worker presence record %s: %s", self.path, error)
                    return None
                continue
            except OSError as error:
                logger.warning("Could not create worker presence record %s: %s", self.path, error)
                return None
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return None
        return None

    def release(self) -> None:
        if not self.acquired:
            return
        current = self._read(self.path)
        if current.get("token") == self.token:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("Could not remove worker presence record %s: %s", self.path, error)
        self.acquired = False

    def __enter__(self) -> "WorkerPresence":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()
