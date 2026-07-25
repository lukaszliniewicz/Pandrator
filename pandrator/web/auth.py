"""Single-owner authentication, bootstrap exchange, and API tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select

from .database import Database
from .models import ApiToken, OwnerAccount, utcnow


_password_hasher = PasswordHasher()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class BootstrapToken:
    digest: str
    expires_at: float


class BootstrapTokenStore:
    """One-use local browser tokens supplied by the supervising launcher."""

    def __init__(self):
        self._tokens: dict[str, BootstrapToken] = {}
        self._lock = threading.Lock()

    def issue(self, ttl_seconds: int = 120) -> str:
        raw = secrets.token_urlsafe(32)
        prefix = raw[:12]
        with self._lock:
            self._tokens[prefix] = BootstrapToken(_token_digest(raw), time.monotonic() + max(10, ttl_seconds))
        return raw

    def add(self, raw: str, ttl_seconds: int = 120) -> None:
        with self._lock:
            self._tokens[raw[:12]] = BootstrapToken(_token_digest(raw), time.monotonic() + max(10, ttl_seconds))

    def consume(self, raw: str) -> bool:
        prefix = str(raw or "")[:12]
        with self._lock:
            record = self._tokens.pop(prefix, None)
        if record is None or record.expires_at < time.monotonic():
            return False
        return hmac.compare_digest(record.digest, _token_digest(raw))


@dataclass(slots=True)
class LoginFailureState:
    attempts: deque[float]
    blocked_until: float = 0.0


class LoginThrottle:
    """Bound expensive remote password verification without affecting loopback."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        initial_block_seconds: float = 30.0,
        max_block_seconds: float = 300.0,
        max_clients: int = 4096,
    ):
        self.max_attempts = max(2, int(max_attempts))
        self.window_seconds = max(10.0, float(window_seconds))
        self.initial_block_seconds = max(1.0, float(initial_block_seconds))
        self.max_block_seconds = max(self.initial_block_seconds, float(max_block_seconds))
        self.max_clients = max(16, int(max_clients))
        self._states: dict[str, LoginFailureState] = {}
        self._lock = threading.Lock()

    def _prune(self, state: LoginFailureState, now: float) -> None:
        cutoff = now - self.window_seconds
        while state.attempts and state.attempts[0] < cutoff:
            state.attempts.popleft()

    def retry_after(self, key: object) -> int:
        normalized = str(key or "unknown")
        now = time.monotonic()
        with self._lock:
            state = self._states.get(normalized)
            if state is None:
                return 0
            self._prune(state, now)
            if state.blocked_until <= now:
                state.blocked_until = 0.0
                if not state.attempts:
                    self._states.pop(normalized, None)
                return 0
            return max(1, int(state.blocked_until - now + 0.999))

    def record_failure(self, key: object) -> int:
        normalized = str(key or "unknown")
        now = time.monotonic()
        with self._lock:
            if normalized not in self._states and len(self._states) >= self.max_clients:
                expired: list[str] = []
                for client_key, existing in self._states.items():
                    self._prune(existing, now)
                    if not existing.attempts and existing.blocked_until <= now:
                        expired.append(client_key)
                for client_key in expired:
                    self._states.pop(client_key, None)
                if len(self._states) >= self.max_clients:
                    oldest = min(
                        self._states,
                        key=lambda client_key: max(
                            self._states[client_key].blocked_until,
                            self._states[client_key].attempts[-1]
                            if self._states[client_key].attempts
                            else 0.0,
                        ),
                    )
                    self._states.pop(oldest, None)
            state = self._states.setdefault(
                normalized,
                LoginFailureState(attempts=deque()),
            )
            self._prune(state, now)
            state.attempts.append(now)
            if len(state.attempts) >= self.max_attempts:
                exponent = len(state.attempts) - self.max_attempts
                block_seconds = min(
                    self.max_block_seconds,
                    self.initial_block_seconds * (2**exponent),
                )
                state.blocked_until = max(state.blocked_until, now + block_seconds)
            return (
                max(1, int(state.blocked_until - now + 0.999))
                if state.blocked_until > now
                else 0
            )

    def reset(self, key: object) -> None:
        with self._lock:
            self._states.pop(str(key or "unknown"), None)


class AuthService:
    def __init__(self, database: Database):
        self.database = database

    def initialized(self) -> bool:
        with self.database.session() as session:
            return session.get(OwnerAccount, 1) is not None

    def initialize_owner(self, password: str, *, replace: bool = False) -> None:
        if len(password) < 10:
            raise ValueError("Owner password must contain at least 10 characters.")
        with self.database.session() as session:
            owner = session.get(OwnerAccount, 1)
            if owner is not None and not replace:
                raise RuntimeError("Owner authentication is already initialized.")
            encoded = _password_hasher.hash(password)
            if owner is None:
                session.add(OwnerAccount(singleton_id=1, password_hash=encoded))
            else:
                owner.password_hash = encoded
                owner.updated_at = utcnow()

    def verify_password(self, password: str) -> bool:
        with self.database.session() as session:
            owner = session.get(OwnerAccount, 1)
            if owner is None:
                return False
            encoded = owner.password_hash
        try:
            return _password_hasher.verify(encoded, password)
        except VerifyMismatchError:
            return False

    def create_api_token(self, label: str) -> tuple[ApiToken, str]:
        raw = f"pan_{secrets.token_urlsafe(32)}"
        token = ApiToken(label=str(label or "CLI token").strip() or "CLI token", token_hash=_token_digest(raw), token_prefix=raw[:12])
        with self.database.session() as session:
            session.add(token)
            session.flush()
            session.expunge(token)
        return token, raw

    def verify_api_token(self, raw: str) -> bool:
        prefix = str(raw or "")[:12]
        digest = _token_digest(str(raw or ""))
        with self.database.session() as session:
            candidates = list(
                session.scalars(
                    select(ApiToken).where(ApiToken.token_prefix == prefix, ApiToken.revoked_at.is_(None))
                ).all()
            )
            for candidate in candidates:
                if hmac.compare_digest(candidate.token_hash, digest):
                    candidate.last_used_at = utcnow()
                    return True
        return False

    def list_tokens(self) -> list[ApiToken]:
        with self.database.session() as session:
            tokens = list(session.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all())
            for token in tokens:
                session.expunge(token)
            return tokens

    def revoke_token(self, token_id: str) -> None:
        with self.database.session() as session:
            token = session.get(ApiToken, token_id)
            if token is None:
                raise KeyError(token_id)
            token.revoked_at = utcnow()

