"""Single-owner authentication, bootstrap exchange, and API tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Database
from .models import ApiToken, AutomationClient, OwnerAccount, utcnow

_password_hasher = PasswordHasher()

ALL_SCOPES = frozenset(
    {
        "app.read",
        "app.write",
        "app.run",
        "app.cancel",
        "app.credentials.read",
        "app.credentials.write",
        "manager.read",
        "manager.runtime",
        "manager.mutate",
        "app.admin",
    }
)
MCP_BOOTSTRAP_SCOPES = frozenset(
    {
        "app.read",
        "app.write",
        "app.run",
        "app.cancel",
        "manager.read",
        "manager.runtime",
        "manager.mutate",
    }
)

PrincipalKind = Literal[
    "owner_session",
    "api_token",
    "manager_bootstrap",
    "automation_client",
    "service",
]
NetworkZone = Literal["loopback", "private", "public"]
PRINCIPAL_KINDS = frozenset(
    {
        "owner_session",
        "api_token",
        "manager_bootstrap",
        "automation_client",
        "service",
    }
)


def normalize_scopes(
    scopes: object,
    *,
    allow_empty: bool = False,
) -> frozenset[str]:
    if isinstance(scopes, str):
        values = scopes.split()
    elif isinstance(scopes, (list, tuple, set, frozenset)):
        values = [str(value) for value in scopes]
    else:
        values = []
    normalized = frozenset(value.strip() for value in values if value.strip())
    unknown = normalized - ALL_SCOPES
    if unknown:
        raise ValueError(f"Unknown API scope(s): {', '.join(sorted(unknown))}.")
    if not normalized and not allow_empty:
        raise ValueError("At least one API scope is required.")
    return normalized


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    kind: PrincipalKind
    scopes: frozenset[str]
    token_id: str | None
    network_zone: NetworkZone
    target_instance_id: str
    client_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "app.admin" in self.scopes


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class BootstrapToken:
    digest: str
    expires_at: float
    subject: str = "owner"
    kind: PrincipalKind = "owner_session"
    scopes: frozenset[str] = ALL_SCOPES
    client_id: str | None = None


class BootstrapTokenStore:
    """One-use local browser tokens supplied by the supervising launcher."""

    def __init__(self):
        self._tokens: dict[str, BootstrapToken] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        ttl_seconds: int = 120,
        *,
        subject: str = "owner",
        kind: PrincipalKind = "owner_session",
        scopes: object = ALL_SCOPES,
        client_id: str | None = None,
    ) -> str:
        raw = secrets.token_urlsafe(32)
        prefix = raw[:12]
        with self._lock:
            self._tokens[prefix] = BootstrapToken(
                _token_digest(raw),
                time.monotonic() + max(10, ttl_seconds),
                subject=str(subject),
                kind=kind,
                scopes=normalize_scopes(scopes),
                client_id=client_id,
            )
        return raw

    def add(self, raw: str, ttl_seconds: int = 120) -> None:
        with self._lock:
            self._tokens[raw[:12]] = BootstrapToken(
                _token_digest(raw),
                time.monotonic() + max(10, ttl_seconds),
            )

    def consume_grant(self, raw: str) -> BootstrapToken | None:
        prefix = str(raw or "")[:12]
        with self._lock:
            record = self._tokens.pop(prefix, None)
        if record is None or record.expires_at < time.monotonic():
            return None
        if not hmac.compare_digest(record.digest, _token_digest(raw)):
            return None
        return record

    def consume(self, raw: str) -> bool:
        return self.consume_grant(raw) is not None


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

    def create_api_token(
        self,
        label: str,
        *,
        scopes: object = ALL_SCOPES,
        expires_at: datetime | None = None,
        principal_kind: PrincipalKind = "api_token",
        created_by: str | None = None,
        subject: str | None = None,
        client_id: str | None = None,
        target_instance_id: str | None = None,
        canonical_origin: str | None = None,
    ) -> tuple[ApiToken, str]:
        with self.database.session() as session:
            token, raw = self.create_api_token_in_session(
                session,
                label,
                scopes=scopes,
                expires_at=expires_at,
                principal_kind=principal_kind,
                created_by=created_by,
                subject=subject,
                client_id=client_id,
                target_instance_id=target_instance_id,
                canonical_origin=canonical_origin,
            )
            session.expunge(token)
            return token, raw

    def create_api_token_in_session(
        self,
        session: Session,
        label: str,
        *,
        scopes: object = ALL_SCOPES,
        expires_at: datetime | None = None,
        principal_kind: PrincipalKind = "api_token",
        created_by: str | None = None,
        subject: str | None = None,
        client_id: str | None = None,
        target_instance_id: str | None = None,
        canonical_origin: str | None = None,
    ) -> tuple[ApiToken, str]:
        """Create a token in a caller-owned transaction."""

        if principal_kind not in PRINCIPAL_KINDS:
            raise ValueError("The API principal kind is invalid.")
        raw = f"pan_{secrets.token_urlsafe(32)}"
        selected_scopes = normalize_scopes(scopes)
        token = ApiToken(
            label=str(label or "CLI token").strip() or "CLI token",
            token_hash=_token_digest(raw),
            token_prefix=raw[:12],
            subject=subject,
            scopes_json=sorted(selected_scopes),
            expires_at=_aware(expires_at),
            principal_kind=principal_kind,
            created_by=created_by,
            client_id=client_id,
            target_instance_id=target_instance_id,
            canonical_origin=canonical_origin,
        )
        session.add(token)
        session.flush()
        if not token.subject:
            token.subject = f"api-token:{token.id}"
            session.flush()
        return token, raw

    def resolve_api_token(
        self,
        raw: str,
        *,
        network_zone: NetworkZone,
        target_instance_id: str | None,
        canonical_origin: str | None = None,
    ) -> Principal | None:
        prefix = str(raw or "")[:12]
        digest = _token_digest(str(raw or ""))
        now = utcnow()
        with self.database.session() as session:
            candidates = list(
                session.scalars(
                    select(ApiToken).where(
                        ApiToken.token_prefix == prefix,
                        ApiToken.revoked_at.is_(None),
                    )
                ).all()
            )
            for candidate in candidates:
                if not hmac.compare_digest(candidate.token_hash, digest):
                    continue
                expires_at = _aware(candidate.expires_at)
                if expires_at is not None and expires_at <= now:
                    return None
                if (
                    candidate.target_instance_id
                    and target_instance_id is not None
                    and candidate.target_instance_id != target_instance_id
                ):
                    return None
                if (
                    candidate.canonical_origin
                    and canonical_origin is not None
                    and candidate.canonical_origin != canonical_origin
                ):
                    return None
                try:
                    scopes = normalize_scopes(candidate.scopes_json)
                except ValueError:
                    return None
                raw_kind = str(candidate.principal_kind or "api_token")
                if raw_kind not in PRINCIPAL_KINDS:
                    return None
                candidate.last_used_at = now
                if candidate.client_id:
                    client = session.get(
                        AutomationClient,
                        candidate.client_id,
                    )
                    if client is None or client.revoked_at is not None:
                        return None
                    if (
                        target_instance_id is not None
                        and client.target_instance_id != target_instance_id
                    ):
                        return None
                    if (
                        canonical_origin is not None
                        and client.canonical_origin != canonical_origin
                    ):
                        return None
                    client.last_used_at = now
                return Principal(
                    subject=(
                        str(candidate.subject)
                        if candidate.subject
                        else f"api-token:{candidate.id}"
                    ),
                    kind=raw_kind,
                    scopes=scopes,
                    token_id=candidate.id,
                    network_zone=network_zone,
                    target_instance_id=(
                        target_instance_id
                        or str(candidate.target_instance_id or "")
                    ),
                    client_id=candidate.client_id,
                )
        return None

    def verify_api_token(self, raw: str) -> bool:
        """Compatibility helper for non-request callers."""

        return (
            self.resolve_api_token(
                raw,
                network_zone="loopback",
                target_instance_id=None,
            )
            is not None
        )

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

    def cleanup_expired_tokens(self, *, grace_days: int = 30) -> int:
        cutoff = utcnow() - timedelta(days=max(1, int(grace_days)))
        removed = 0
        with self.database.session() as session:
            tokens = list(
                session.scalars(
                    select(ApiToken).where(ApiToken.expires_at.is_not(None))
                ).all()
            )
            for token in tokens:
                expires_at = _aware(token.expires_at)
                if expires_at is not None and expires_at < cutoff:
                    session.delete(token)
                    removed += 1
        return removed

