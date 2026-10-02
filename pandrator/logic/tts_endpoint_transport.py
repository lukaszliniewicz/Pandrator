"""Endpoint coordination and reusable HTTP sessions for audio.cpp."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Lock, RLock
from urllib.parse import urlparse, urlunparse

import requests

from .cancellable_process import ProcessCancelled

# audio.cpp keeps resident model state, so requests to one normalized endpoint
# must never overlap. RLocks allow an ordered batch to reenter for each item.
_audio_cpp_endpoint_locks_guard = Lock()
_audio_cpp_endpoint_locks: dict[str, RLock] = {}


def audio_cpp_endpoint_key(base_url: str) -> str:
    """Return a stable endpoint identity without request or voice data."""
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{hostname}:{port}{path}"


def endpoint_lock_key(base_url: str, *, key_for: Callable[[str], str]) -> str:
    """Resident model state belongs to a server, independent of its URL path."""
    parsed = urlparse(str(base_url or "").strip())
    origin = urlunparse(parsed._replace(path="", params="", query="", fragment=""))
    return key_for(origin)


def endpoint_lock_urls(
    base_url: str, speech_url: str, *, lock_key_for: Callable[[str], str]
) -> list[str]:
    """Order distinct base/target servers consistently for nested acquisition."""
    urls_by_key: dict[str, str] = {}
    for url in (base_url, speech_url):
        urls_by_key.setdefault(lock_key_for(url), url)
    return [urls_by_key[key] for key in sorted(urls_by_key)]


def audio_cpp_endpoint_lock_for_key(key: str) -> RLock:
    with _audio_cpp_endpoint_locks_guard:
        lock = _audio_cpp_endpoint_locks.get(key)
        if lock is None:
            lock = RLock()
            _audio_cpp_endpoint_locks[key] = lock
        return lock


@contextmanager
def endpoint_lock_guard(
    lock: RLock, cancel_event: Event | None = None
) -> Iterator[None]:
    if cancel_event is None:
        with lock:
            yield
        return
    while not lock.acquire(timeout=0.05):
        if cancel_event.is_set():
            raise ProcessCancelled("Audio.cpp execution was canceled.")
    try:
        if cancel_event.is_set():
            raise ProcessCancelled("Audio.cpp execution was canceled.")
        yield
    finally:
        lock.release()


class EndpointSessionPool:
    """Create one caller-supplied HTTP session for each endpoint key."""

    def __init__(self) -> None:
        self._sessions_guard = Lock()
        self._sessions: dict[str, requests.Session] = {}

    def session_for_key(
        self, key: str, *, create_session: Callable[[], requests.Session]
    ) -> requests.Session:
        with self._sessions_guard:
            session = self._sessions.get(key)
            if session is None:
                session = create_session()
                self._sessions[key] = session
            return session
