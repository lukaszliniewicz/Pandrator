"""Serialize local audio.cpp work and protect native GPU memory use."""

from __future__ import annotations

import errno
import ipaddress
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .cancellable_process import ProcessCancelled


class AudioCppExecutionError(RuntimeError):
    """Local audio.cpp execution cannot safely proceed."""


_thread_lock = threading.RLock()
_thread_state = threading.local()
_POLL_SECONDS = 0.05
_MAX_RESPONSE_BYTES = 1024 * 1024


def _lock_path() -> Path:
    return Path.home() / ".cache/pandrator/execution/audio-cpp.lock"


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Audio.cpp execution was canceled.")


def _lock_file(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _wait(cancel_event: threading.Event | None, deadline: float) -> None:
    _check_cancelled(cancel_event)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AudioCppExecutionError("Timed out waiting for local audio.cpp execution.")
    if cancel_event is None:
        time.sleep(min(_POLL_SECONDS, remaining))
    else:
        cancel_event.wait(min(_POLL_SECONDS, remaining))


@contextmanager
def local_audio_cpp_lock(
    cancel_event: threading.Event | None = None, *, timeout: float = 600.0
) -> Iterator[None]:
    """Hold a cancellable per-thread and cross-process local audio.cpp lock."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("Audio.cpp lock timeout must be a positive finite number.")
    deadline = time.monotonic() + timeout
    while True:
        _check_cancelled(cancel_event)
        if _thread_lock.acquire(blocking=False):
            break
        _wait(cancel_event, deadline)
    try:
        _check_cancelled(cancel_event)
        if getattr(_thread_state, "depth", 0):
            _thread_state.depth += 1
            try:
                yield
            finally:
                _thread_state.depth -= 1
            return

        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if sys.platform == "win32":
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
            while True:
                _check_cancelled(cancel_event)
                try:
                    _lock_file(handle)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    _wait(cancel_event, deadline)
            try:
                _check_cancelled(cancel_event)
                _thread_state.depth = 1
                try:
                    yield
                finally:
                    _thread_state.depth = 0
            finally:
                _unlock_file(handle)
    finally:
        _thread_lock.release()


def _local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _server_url(command: Sequence[str | os.PathLike[str]]) -> str:
    if not command:
        raise AudioCppExecutionError("Audio.cpp native command has no executable.")
    config_path = Path(command[0]).expanduser().parent / "server.json"
    if not config_path.exists():
        return "http://127.0.0.1:8060"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("Invalid server configuration")
        host = config["host"]
        port = config["port"]
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise ValueError("Invalid server address")
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        elif not _local_host(host):
            raise ValueError("Nonlocal server address")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise AudioCppExecutionError(
            "Cannot inspect the local audio.cpp server configuration."
        ) from error
    address = f"[{host}]" if ":" in host else host
    return f"http://{address}:{port}"


def _backend(command: Sequence[str | os.PathLike[str]]) -> str:
    values = [os.fspath(value) for value in command]
    for index, value in enumerate(values):
        if value == "--backend":
            return values[index + 1].lower() if index + 1 < len(values) else ""
        if value.startswith("--backend="):
            return value.partition("=")[2].lower()
    return ""


def _request_json(
    url: str, *, payload: dict | None = None, allow_refused: bool = False
) -> object | None:
    request = (
        urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if payload is not None
        else url
    )
    try:
        with urllib.request.urlopen(
            request, timeout=10 if payload is not None else 5
        ) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise AudioCppExecutionError(
            "Cannot release local audio.cpp models; stop its service or choose CPU."
        ) from error
    except urllib.error.URLError as error:
        reason = error.reason
        if allow_refused and (
            isinstance(reason, ConnectionRefusedError)
            or (isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED)
        ):
            return None
        raise AudioCppExecutionError(
            "Cannot inspect local audio.cpp models; stop its service or choose CPU."
        ) from error
    except (OSError, TimeoutError) as error:
        if allow_refused and error.errno == errno.ECONNREFUSED:
            return None
        raise AudioCppExecutionError(
            "Cannot inspect local audio.cpp models; stop its service or choose CPU."
        ) from error
    if len(body) > _MAX_RESPONSE_BYTES:
        raise AudioCppExecutionError("Local audio.cpp model response is too large.")
    try:
        return json.loads(body)
    except (UnicodeError, ValueError) as error:
        raise AudioCppExecutionError(
            "Local audio.cpp returned invalid model data."
        ) from error


def _model_rows(payload: object) -> list[dict]:
    try:
        if not isinstance(payload, dict):
            raise ValueError("Invalid response")
        rows = payload["data"]
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("loaded"), bool)
            for row in rows
        ):
            raise ValueError("Invalid model rows")
        return rows
    except (ValueError, TypeError, KeyError) as error:
        raise AudioCppExecutionError(
            "Local audio.cpp returned invalid model residency data."
        ) from error


def _assert_no_loaded_model(command: Sequence[str | os.PathLike[str]]) -> None:
    base_url = _server_url(command)
    rows_payload = _request_json(f"{base_url}/v1/models", allow_refused=True)
    if rows_payload is None:
        return
    rows = _model_rows(rows_payload)
    loaded_ids = [row.get("id") for row in rows if row["loaded"]]
    if not loaded_ids:
        return
    if any(
        not isinstance(model_id, str) or not model_id.strip() for model_id in loaded_ids
    ):
        raise AudioCppExecutionError(
            "Local audio.cpp returned invalid loaded model IDs."
        )
    unload = _request_json(
        f"{base_url}/v1/tasks/unload_models", payload={"model_ids": loaded_ids}
    )
    if not isinstance(unload, dict) or any(
        not isinstance(unload.get(key), list)
        or any(not isinstance(value, str) for value in unload[key])
        for key in ("unloaded", "not_found")
    ):
        raise AudioCppExecutionError(
            "Cannot verify local audio.cpp model release; stop its service or choose CPU."
        )
    verified = _model_rows(_request_json(f"{base_url}/v1/models"))
    if any(row["loaded"] for row in verified):
        raise AudioCppExecutionError(
            "Local audio.cpp still has a speech model loaded; stop its service or choose CPU."
        )


@contextmanager
def native_audio_cpp_guard(
    command: Sequence[str | os.PathLike[str]],
    cancel_event: threading.Event | None = None,
) -> Iterator[None]:
    """Serialize native execution and exclude resident server GPU models."""
    with local_audio_cpp_lock(cancel_event):
        if _backend(command) != "cpu":
            _assert_no_loaded_model(command)
        _check_cancelled(cancel_event)
        yield


@contextmanager
def local_tts_audio_cpp_guard(
    base_url: str, cancel_event: threading.Event | None = None
) -> Iterator[None]:
    """Serialize local TTS with native audio.cpp work."""
    try:
        parsed = urlsplit(base_url)
        local = parsed.scheme in {"http", "https"} and _local_host(
            parsed.hostname or ""
        )
    except ValueError:
        local = False
    if local:
        with local_audio_cpp_lock(cancel_event):
            yield
    else:
        yield
