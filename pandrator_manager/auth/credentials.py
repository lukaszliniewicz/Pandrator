"""Per-user credentials, one-use launches, and durable browser sessions."""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..state import ManagerStore


def protect_path(path: Path, *, directory: bool = False) -> None:
    """Restrict a state path to the owning user without broad ACL grants."""
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return

    # Newly-created files inherit the user's profile DACL. Remove broader
    # inheritance explicitly and grant the current account full control.
    account = getpass.getuser()
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{account}:(OI)(CI)F" if directory else f"{account}:F",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not protect manager state path {path}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _atomic_secret(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protect_path(path.parent, directory=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        protect_path(temporary)
        os.replace(temporary, path)
        protect_path(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_client_secret(path: Path) -> str:
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret = ""
    if len(secret) >= 43:
        protect_path(path)
        return secret
    secret = secrets.token_urlsafe(32)
    _atomic_secret(path, secret)
    return secret


def read_client_secret(path: Path) -> str:
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < 43:
        raise RuntimeError("Manager client credential is invalid.")
    return secret


def derive_browser_session_keys(
    client_secret: str,
    security_boundary: Mapping[str, Any],
) -> tuple[str, bytes]:
    """Derive restart-stable, boundary-specific browser-session material."""

    canonical = json.dumps(
        dict(security_boundary),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    root = hmac.new(
        client_secret.encode("utf-8"),
        b"pandrator-browser-session-v1\0" + canonical,
        hashlib.sha256,
    ).digest()
    security_context = hashlib.sha256(b"context\0" + root).hexdigest()
    csrf_secret = hmac.new(root, b"csrf-v1", hashlib.sha256).digest()
    return security_context, csrf_secret


@dataclass(frozen=True, slots=True)
class RecoverySession:
    record_id: str
    session_id: str
    csrf_token: str
    created_at: float
    last_seen_at: float
    expires_at: float
    absolute_expires_at: float
    remembered: bool
    user_agent: str


class RecoverySessionManager:
    """One-use launch tokens exchanged for durable, revocable browser sessions."""

    def __init__(
        self,
        *,
        token_ttl_seconds: int = 60,
        session_ttl_seconds: int = 30 * 60,
        remembered_idle_ttl_seconds: int = 30 * 24 * 60 * 60,
        remembered_absolute_ttl_seconds: int = 90 * 24 * 60 * 60,
        touch_interval_seconds: int = 15 * 60,
        store: ManagerStore | None = None,
        security_context: str = "ephemeral",
        csrf_secret: bytes | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.token_ttl_seconds = max(10, int(token_ttl_seconds))
        self.session_ttl_seconds = max(60, int(session_ttl_seconds))
        self.remembered_idle_ttl_seconds = max(
            self.session_ttl_seconds,
            int(remembered_idle_ttl_seconds),
        )
        self.remembered_absolute_ttl_seconds = max(
            self.remembered_idle_ttl_seconds,
            int(remembered_absolute_ttl_seconds),
        )
        self.touch_interval_seconds = max(30, int(touch_interval_seconds))
        self.store = store
        self.security_context = str(security_context)
        self._csrf_secret = bytes(csrf_secret or secrets.token_bytes(32))
        self._clock = clock
        self._launch_tokens: dict[str, float] = {}
        self._memory_sessions: dict[str, dict[str, Any]] = {}
        self._next_prune_at = 0.0
        self._lock = threading.RLock()
        with self._lock:
            self._prune(self._clock(), force=True)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _csrf_token(self, session_id: str) -> str:
        digest = hmac.new(
            self._csrf_secret,
            b"session-csrf\0" + session_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @staticmethod
    def _clean_user_agent(value: str) -> str:
        return " ".join(str(value or "").split())[:256]

    def _prune(self, now: float, *, force: bool = False) -> None:
        self._launch_tokens = {
            digest: expires
            for digest, expires in self._launch_tokens.items()
            if expires > now
        }
        if not force and now < self._next_prune_at:
            return
        if self.store is not None:
            self.store.prune_browser_sessions(
                now=now,
                security_context=self.security_context,
            )
        else:
            self._memory_sessions = {
                digest: record
                for digest, record in self._memory_sessions.items()
                if record["security_context"] == self.security_context
                and record["idle_expires_at"] > now
                and record["absolute_expires_at"] > now
            }
        self._next_prune_at = now + min(300, self.touch_interval_seconds)

    def _create_record(self, record: Mapping[str, Any]) -> None:
        if self.store is not None:
            self.store.create_browser_session(**dict(record))
        else:
            self._memory_sessions[str(record["token_digest"])] = dict(record)

    def _record(self, token_digest: str) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.browser_session(token_digest)
        record = self._memory_sessions.get(token_digest)
        return dict(record) if record is not None else None

    def _touch_record(
        self,
        token_digest: str,
        *,
        last_seen_at: float,
        idle_expires_at: float,
    ) -> bool:
        if self.store is not None:
            return self.store.touch_browser_session(
                token_digest,
                last_seen_at=last_seen_at,
                idle_expires_at=idle_expires_at,
            )
        record = self._memory_sessions.get(token_digest)
        if record is None:
            return False
        record["last_seen_at"] = last_seen_at
        record["idle_expires_at"] = idle_expires_at
        return True

    def _delete_record(self, token_digest: str) -> bool:
        if self.store is not None:
            return self.store.delete_browser_session(token_digest)
        return self._memory_sessions.pop(token_digest, None) is not None

    def _records(self) -> list[dict[str, Any]]:
        if self.store is not None:
            return self.store.browser_sessions(self.security_context)
        return [
            dict(record)
            for record in self._memory_sessions.values()
            if record["security_context"] == self.security_context
        ]

    def _as_session(
        self,
        session_id: str,
        record: Mapping[str, Any],
    ) -> RecoverySession:
        return RecoverySession(
            record_id=str(record["session_id"]),
            session_id=session_id,
            csrf_token=self._csrf_token(session_id),
            created_at=float(record["created_at"]),
            last_seen_at=float(record["last_seen_at"]),
            expires_at=min(
                float(record["idle_expires_at"]),
                float(record["absolute_expires_at"]),
            ),
            absolute_expires_at=float(record["absolute_expires_at"]),
            remembered=bool(record["remembered"]),
            user_agent=str(record["user_agent"]),
        )

    def mint_launch_token(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._launch_tokens[self._digest(token)] = now + self.token_ttl_seconds
        return token

    def exchange(
        self,
        token: str,
        *,
        remember: bool = True,
        user_agent: str = "",
    ) -> RecoverySession | None:
        now = self._clock()
        digest = self._digest(str(token))
        with self._lock:
            self._prune(now)
            expires = self._launch_tokens.pop(digest, None)
            if expires is None or expires <= now:
                return None
            session_id = secrets.token_urlsafe(32)
            token_digest = self._digest(session_id)
            idle_ttl = (
                self.remembered_idle_ttl_seconds
                if remember
                else self.session_ttl_seconds
            )
            absolute_ttl = (
                self.remembered_absolute_ttl_seconds
                if remember
                else self.session_ttl_seconds
            )
            record = {
                "session_id": secrets.token_urlsafe(18),
                "token_digest": token_digest,
                "security_context": self.security_context,
                "remembered": bool(remember),
                "created_at": now,
                "last_seen_at": now,
                "idle_ttl_seconds": idle_ttl,
                "idle_expires_at": now + idle_ttl,
                "absolute_expires_at": now + absolute_ttl,
                "user_agent": self._clean_user_agent(user_agent),
            }
            self._create_record(record)
            return self._as_session(session_id, record)

    def authenticate(
        self,
        session_id: str | None,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> RecoverySession | None:
        raw_session_id = str(session_id or "")
        if len(raw_session_id) < 32:
            return None
        now = self._clock()
        token_digest = self._digest(raw_session_id)
        with self._lock:
            self._prune(now)
            record = self._record(token_digest)
            if record is None:
                return None
            if (
                record["security_context"] != self.security_context
                or float(record["idle_expires_at"]) <= now
                or float(record["absolute_expires_at"]) <= now
            ):
                self._delete_record(token_digest)
                return None
            expected_csrf = self._csrf_token(raw_session_id)
            if require_csrf and not hmac.compare_digest(
                expected_csrf,
                str(csrf_token or ""),
            ):
                return None
            if now - float(record["last_seen_at"]) >= self.touch_interval_seconds:
                idle_expires_at = min(
                    now + int(record["idle_ttl_seconds"]),
                    float(record["absolute_expires_at"]),
                )
                if self._touch_record(
                    token_digest,
                    last_seen_at=now,
                    idle_expires_at=idle_expires_at,
                ):
                    record = {
                        **record,
                        "last_seen_at": now,
                        "idle_expires_at": idle_expires_at,
                    }
            return self._as_session(raw_session_id, record)

    def validate(
        self,
        session_id: str | None,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> bool:
        return (
            self.authenticate(
                session_id,
                csrf_token=csrf_token,
                require_csrf=require_csrf,
            )
            is not None
        )

    def sessions(self, current_session_id: str | None = None) -> list[dict[str, Any]]:
        now = self._clock()
        current_digest = self._digest(str(current_session_id or ""))
        with self._lock:
            self._prune(now)
            return [
                {
                    "id": str(record["session_id"]),
                    "current": hmac.compare_digest(
                        str(record["token_digest"]),
                        current_digest,
                    ),
                    "remembered": bool(record["remembered"]),
                    "created_at": float(record["created_at"]),
                    "last_seen_at": float(record["last_seen_at"]),
                    "expires_at": min(
                        float(record["idle_expires_at"]),
                        float(record["absolute_expires_at"]),
                    ),
                    "absolute_expires_at": float(record["absolute_expires_at"]),
                    "user_agent": str(record["user_agent"]),
                }
                for record in self._records()
                if float(record["idle_expires_at"]) > now
                and float(record["absolute_expires_at"]) > now
            ]

    def revoke(self, session_id: str | None) -> bool:
        raw_session_id = str(session_id or "")
        if not raw_session_id:
            return False
        with self._lock:
            return self._delete_record(self._digest(raw_session_id))

    def revoke_all(self) -> int:
        with self._lock:
            if self.store is not None:
                return self.store.delete_browser_sessions()
            count = len(self._memory_sessions)
            self._memory_sessions.clear()
            return count
