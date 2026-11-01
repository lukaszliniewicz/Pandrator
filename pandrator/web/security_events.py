"""Optional, content-free JSONL security events for external ban tooling."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from datetime import UTC, datetime

_LOGGER = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_ROUTE_SEGMENT = r"(?:[A-Za-z0-9._~-]+|<(?:[A-Za-z_][A-Za-z0-9_]*:)?[A-Za-z_][A-Za-z0-9_]*>)"
_API_ROUTE = re.compile(rf"^/api/{_ROUTE_SEGMENT}(?:/{_ROUTE_SEGMENT})*/?$")
_EVENTS = frozenset(
    {
        "authentication_failed",
        "authentication_throttled",
        "project_link_redemption_failed",
        "project_link_redemption_throttled",
    }
)
_THROTTLED_EVENTS = frozenset({"authentication_throttled", "project_link_redemption_throttled"})
_THROTTLE_SECONDS = 30.0
_THROTTLE_CACHE_LIMIT = 4096
_MAX_LINE_BYTES = 512


class SecurityEventSink:
    """Write tightly validated security events without affecting auth flows."""

    def __init__(self, target: str | None) -> None:
        self._target = target if isinstance(target, str) else None
        self._enabled = bool(self._target) and (
            self._target == "stdout" or os.path.isabs(self._target)
        )
        self._lock = threading.Lock()
        self._warning_lock = threading.Lock()
        self._warning_emitted = False
        self._throttled_events: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def emit(
        self,
        event: str,
        *,
        client_ip: str | None,
        status: int,
        request_id: str | None,
        route: str,
        method: str,
    ) -> bool:
        """Emit one valid record, returning false when it is dropped or unavailable."""

        if not self.enabled:
            return False
        if (
            not isinstance(event, str)
            or event not in _EVENTS
            or type(status) is not int
            or not 100 <= status <= 599
        ):
            return False
        if type(method) is not str or method != "POST":
            return False
        if not isinstance(route, str) or len(route) > 160:
            return False
        if not route.isascii() or not _API_ROUTE.fullmatch(route):
            return False
        normalized_ip = _normalize_ip(client_ip)
        if client_ip is not None and normalized_ip is None:
            return False

        if event in _THROTTLED_EVENTS and normalized_ip is not None:
            with self._lock:
                if self._should_suppress_throttled(event, normalized_ip, time.monotonic()):
                    return False

        record: dict[str, str | int | None] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "event": event,
            "client_ip": normalized_ip,
            "status": status,
        }
        if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id):
            record["request_id"] = request_id
        record["route"] = route
        record["method"] = method

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        payload = line.encode("utf-8")
        if len(payload) > _MAX_LINE_BYTES:
            self._warn_once()
            return False
        if self._target == "stdout":
            try:
                written = sys.stdout.write(line)
                if written is not None and written != len(line):
                    raise OSError("short stdout write")
            except (OSError, ValueError):
                self._warn_once()
                return False
            return True

        if self._append_file(payload):
            return True
        self._warn_once()
        return False

    def _should_suppress_throttled(self, event: str, client_ip: str, now: float) -> bool:
        expired = [
            key
            for key, emitted_at in self._throttled_events.items()
            if now - emitted_at >= _THROTTLE_SECONDS
        ]
        for key in expired:
            del self._throttled_events[key]

        key = (event, client_ip)
        if key in self._throttled_events:
            return True
        if len(self._throttled_events) >= _THROTTLE_CACHE_LIMIT:
            # Preserve live keys so no known client is re-emitted inside its window.
            return True
        self._throttled_events[key] = now
        return False

    def _append_file(self, payload: bytes) -> bool:
        if self._target is None:
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        # Avoid hanging if an operator accidentally points the sink at a FIFO.
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor: int | None = None
        failed = False
        try:
            descriptor = os.open(self._target, flags, 0o600)
            path_info = os.lstat(self._target)
            descriptor_info = os.fstat(descriptor)
            if (
                stat.S_ISLNK(path_info.st_mode)
                or not stat.S_ISREG(descriptor_info.st_mode)
                or (path_info.st_dev, path_info.st_ino)
                != (descriptor_info.st_dev, descriptor_info.st_ino)
            ):
                raise OSError("security event target is not a regular file")
            fchmod = getattr(os, "fchmod", None)
            if fchmod is None:
                raise OSError("descriptor permissions cannot be restricted")
            fchmod(descriptor, 0o600)
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short security event write")
        except (OSError, ValueError):
            failed = True
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
        return not failed

    def _warn_once(self) -> None:
        with self._warning_lock:
            if self._warning_emitted:
                return
            self._warning_emitted = True
        try:
            _LOGGER.warning("Security event sink write failed")
        except Exception:
            # Logging failures must not change the authentication result.
            pass


def _normalize_ip(client_ip: str | None) -> str | None:
    if client_ip is None:
        return None
    if not isinstance(client_ip, str) or not 1 <= len(client_ip) <= 45 or "%" in client_ip:
        return None
    try:
        return ipaddress.ip_address(client_ip).compressed
    except ValueError:
        return None
