"""User-bound authentication with compatible solo bootstrap and token adapters."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .accounts import installation_mode
from .accounts.actor_authorization import require_actor_in_session
from .accounts.auth_contracts import ALL_SCOPES as ALL_SCOPES
from .accounts.auth_contracts import MCP_BOOTSTRAP_SCOPES as MCP_BOOTSTRAP_SCOPES
from .accounts.auth_contracts import PRINCIPAL_KINDS as PRINCIPAL_KINDS
from .accounts.auth_contracts import USER_BROWSER_SCOPES
from .accounts.auth_contracts import NetworkZone as NetworkZone
from .accounts.auth_contracts import PrincipalKind as PrincipalKind
from .accounts.auth_contracts import normalize_scopes as normalize_scopes
from .accounts.auth_models import UserCredential
from .accounts.auth_state import AccountState, account_state_in_session, initial_user_id
from .accounts.browser_sessions import BrowserSessionStore
from .accounts.commands import revoke_user_credentials
from .accounts.credential_resources import (
    CredentialOwner,
    CredentialResources,
    default_personal_resources,
    record_resources,
    require_resources_within,
    resources_for_owner,
    validate_resources_for_user,
)
from .accounts.execution_actor import UserActor
from .accounts.models import User
from .accounts.passwords import (
    dummy_verify,
    hash_password,
    mirror_initial_owner_password,
    password_needs_rehash,
    verify_password,
)
from .database import Database, is_sqlite_busy
from .models import ApiToken, AutomationClient, utcnow

_LOGGER = logging.getLogger(__name__)
API_TOKEN_ACTIVITY_INTERVAL = timedelta(seconds=60)
API_TOKEN_TOUCH_TIMEOUT_MS = 250
API_TOKEN_TOUCH_CLAIMS_LIMIT = 4096


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
    user_id: str | None = None
    auth_revision: int | None = None
    browser_session_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


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
    user_id: str | None = None
    auth_revision: int | None = None


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
        user_id: str | None = None,
        auth_revision: int | None = None,
    ) -> str:
        selected_scopes = normalize_scopes(scopes)
        if kind not in PRINCIPAL_KINDS:
            raise ValueError("The bootstrap principal kind is invalid.")
        if kind == "manager_bootstrap" and "app.admin" in selected_scopes:
            raise ValueError("Manager bootstrap cannot grant Administrator authority.")
        if (user_id is None) != (auth_revision is None):
            raise ValueError("Bootstrap account identity and revision must be supplied together.")
        raw = secrets.token_urlsafe(32)
        prefix = raw[:12]
        with self._lock:
            self._tokens[prefix] = BootstrapToken(
                _token_digest(raw),
                time.monotonic() + max(10, ttl_seconds),
                subject=str(subject),
                kind=kind,
                scopes=selected_scopes,
                client_id=client_id,
                user_id=user_id,
                auth_revision=auth_revision,
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
        self.browser_sessions = BrowserSessionStore(database)
        self._touch_claims: dict[tuple[str, str], datetime] = {}
        self._touch_claims_lock = threading.Lock()

    def initialized(self) -> bool:
        with self.database.session() as session:
            return session.scalar(
                select(UserCredential.password_hash).where(
                    UserCredential.user_id == initial_user_id(session)
                )
            ) is not None

    def initialize_owner(self, password: str, *, replace: bool = False) -> None:
        # Keep the existing solo CLI minimum; new account/reset policy is stricter.
        encoded = hash_password(password, minimum_length=10)
        with self.database.immediate_session() as session:
            user_id = initial_user_id(session)
            account_state_in_session(session, user_id, require_admin=True)
            credential = session.get(UserCredential, user_id)
            assert credential is not None
            if credential.password_hash is not None and not replace:
                raise RuntimeError("Owner authentication is already initialized.")
            now = utcnow()
            if credential.password_hash is not None:
                credential.auth_revision += 1
                revoke_user_credentials(session, user_id, now)
            credential.password_hash = encoded
            credential.updated_at = now
            mirror_initial_owner_password(
                session, user_id=user_id, encoded=encoded, updated_at=now
            )

    def verify_password(self, password: str) -> bool:
        """Compatibility for owner-only adapters such as local CLI setup."""
        return self.authenticate_initial_password(password) is not None

    def authenticate_initial_password(self, password: str) -> AccountState | None:
        return self._authenticate("owner", password, initial_administrator=True)

    def collaboration_enabled(self, *, db_session: Session | None = None) -> bool:
        """Read the installation policy in the caller's snapshot when provided."""
        if not installation_mode.COLLABORATION_AVAILABLE:
            return False
        if db_session is not None:
            return installation_mode.installation_mode_in_session(db_session).enabled
        with self.database.snapshot_session() as session:
            return installation_mode.installation_mode_in_session(session).enabled

    def authenticate_login(self, login_name: str | None, password: str) -> AccountState | None:
        if not self.collaboration_enabled():
            if login_name is not None and login_name.strip().lower() != "owner":
                dummy_verify(password)
                return None
            return self.authenticate_initial_password(password)
        name = (login_name or "").strip().lower()
        if not name or len(name) > 64 or not name.isascii():
            dummy_verify(password)
            return None
        return self._authenticate(name, password)

    @staticmethod
    def browser_scopes(account: AccountState) -> frozenset[str]:
        return ALL_SCOPES if account.is_instance_admin else USER_BROWSER_SCOPES

    def _authenticate(
        self, login_name: str, password: str, *, initial_administrator: bool = False
    ) -> AccountState | None:
        with self.database.session() as session:
            statement = (
                select(User.id, UserCredential.password_hash, UserCredential.auth_revision)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(UserCredential.login_name == login_name, User.is_active.is_(True))
            )
            if initial_administrator:
                statement = statement.where(
                    User.legacy_subject == "owner", User.is_instance_admin.is_(True)
                )
            captured = session.execute(statement).one_or_none()
        if captured is None or captured.password_hash is None:
            dummy_verify(password)
            return None
        encoded = captured.password_hash
        if not verify_password(encoded, password):
            return None
        replacement = (
            hash_password(password, minimum_length=1)
            if password_needs_rehash(encoded)
            else None
        )
        # Expensive hashing occurs before this writer. A concurrent reset, disable
        # or role change cannot be revived by an earlier password verification.
        try:
            with self.database.immediate_session() as session:
                state = account_state_in_session(
                    session,
                    captured.id,
                    auth_revision=captured.auth_revision,
                    require_admin=initial_administrator,
                )
                credential = session.get(UserCredential, state.user_id)
                if credential is None or credential.password_hash != encoded:
                    return None
                if replacement is not None:
                    now = utcnow()
                    credential.password_hash = replacement
                    credential.updated_at = now
                    mirror_initial_owner_password(
                        session, user_id=state.user_id, encoded=replacement, updated_at=now
                    )
                return state
        except PermissionError:
            return None

    def initial_account(self) -> AccountState:
        """Trusted solo bootstrap selects the existing initial Administrator."""
        with self.database.session() as session:
            return account_state_in_session(
                session, initial_user_id(session), require_admin=True
            )

    def require_administrator(self, principal: Principal) -> AccountState:
        if (
            not principal.user_id
            or principal.auth_revision is None
            or "app.admin" not in principal.scopes
            or principal.kind == "manager_bootstrap"
        ):
            raise PermissionError("An active Administrator credential is required.")
        with self.database.session() as session:
            return account_state_in_session(
                session,
                principal.user_id,
                auth_revision=principal.auth_revision,
                require_admin=True,
            )

    def solo_access_allowed(self, user_id: str | None, auth_revision: int | None) -> bool:
        """Compatibility name for the current installation account admission gate."""
        if user_id is None or auth_revision is None:
            return False
        initial = and_(User.legacy_subject == "owner", User.is_instance_admin.is_(True))
        admission = (
            or_(initial, installation_mode.collaboration_enabled_clause())
            if installation_mode.COLLABORATION_AVAILABLE else initial
        )
        with self.database.session() as session:
            return session.scalar(
                select(User.id)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(
                    User.id == user_id, User.is_active.is_(True), admission,
                    UserCredential.auth_revision == auth_revision,
                )
            ) is not None

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
        user_id: str | None = None,
        auth_revision: int | None = None,
        resource_grants: CredentialResources | None = None,
    ) -> tuple[ApiToken, str]:
        with self.database.immediate_session() as session:
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
                user_id=user_id,
                auth_revision=auth_revision,
                resource_grants=resource_grants,
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
        user_id: str | None = None,
        auth_revision: int | None = None,
        resource_grants: CredentialResources | None = None,
    ) -> tuple[ApiToken, str]:
        """Create a token in a caller-owned transaction."""

        if principal_kind not in PRINCIPAL_KINDS:
            raise ValueError("The API principal kind is invalid.")
        raw = f"pan_{secrets.token_urlsafe(32)}"
        selected_scopes = normalize_scopes(scopes)
        if principal_kind == "manager_bootstrap" and "app.admin" in selected_scopes:
            raise PermissionError("Manager bootstrap cannot grant Administrator authority.")
        state = account_state_in_session(
            session,
            user_id if user_id is not None else initial_user_id(session),
            auth_revision=auth_revision,
            require_admin="app.admin" in selected_scopes,
        )
        client_resources = None
        if client_id is not None:
            client = session.get(AutomationClient, client_id, populate_existing=True)
            if (
                client is None
                or client.revoked_at is not None
                or client.user_id != state.user_id
                or client.user_auth_revision != state.auth_revision
            ):
                raise PermissionError("The automation credential is inactive or has changed.")
            client_resources = resources_for_owner(session, CredentialOwner.CLIENT, client_id)
        selected_resources = resource_grants
        if selected_resources is None:
            selected_resources = (
                client_resources
                if client_resources is not None
                else default_personal_resources(session, state.user_id)
            )
        selected_resources = validate_resources_for_user(
            session, state.user_id, selected_resources
        )
        if client_resources is not None:
            require_resources_within(session, selected_resources, client_resources)
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
            user_id=state.user_id,
            user_auth_revision=state.auth_revision,
        )
        session.add(token)
        session.flush()
        if not token.subject:
            token.subject = f"api-token:{token.id}"
            session.flush()
        try:
            account_state_in_session(
                session,
                state.user_id,
                auth_revision=state.auth_revision,
                require_admin="app.admin" in selected_scopes,
            )
        except PermissionError:
            session.rollback()
            raise
        record_resources(session, CredentialOwner.TOKEN, token.id, selected_resources)
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
        with self.database.snapshot_session() as session:
            now = utcnow()
            resolved = self._resolve_api_token_in_session(
                session,
                prefix,
                digest,
                now=now,
                network_zone=network_zone,
                target_instance_id=target_instance_id,
                canonical_origin=canonical_origin,
            )
        if resolved is None:
            return None
        principal, token_due, client_due = resolved
        token_due, client_due = self._claim_touch(
            principal, now, token_due=token_due, client_due=client_due
        )
        if token_due or client_due:
            self._touch_api_token(principal, now, token_due=token_due, client_due=client_due)
        return principal

    @staticmethod
    def _activity_due(last_used_at: datetime | None, now: datetime) -> bool:
        # A stored time ahead of now means the clock stepped back; touch again.
        last_used = _aware(last_used_at)
        return (
            last_used is None
            or last_used > now
            or now - last_used >= API_TOKEN_ACTIVITY_INTERVAL
        )

    def _claim_touch(
        self, principal: Principal, now: datetime, *, token_due: bool, client_due: bool
    ) -> tuple[bool, bool]:
        """Let one request per interval in this process attempt a due touch.

        Concurrent requests that all see the touch due then take the writer once, and
        a touch skipped because the writer was busy is not retried by every following
        request. The conditional UPDATE still settles races between processes.
        """
        if not (token_due or client_due):
            return False, False
        with self._touch_claims_lock:
            claims = self._touch_claims
            if len(claims) >= API_TOKEN_TOUCH_CLAIMS_LIMIT:
                for key in [key for key, at in claims.items() if self._activity_due(at, now)]:
                    del claims[key]
                if len(claims) >= API_TOKEN_TOUCH_CLAIMS_LIMIT:
                    claims.clear()

            def claim(kind: str, identifier: str | None, due: bool) -> bool:
                if not due or not identifier:
                    return False
                key = (kind, identifier)
                claimed_at = claims.get(key)
                if claimed_at is not None and not self._activity_due(claimed_at, now):
                    return False
                claims[key] = now
                return True

            return (
                claim("token", principal.token_id, token_due),
                claim("client", principal.client_id, client_due),
            )

    def _resolve_api_token_in_session(
        self,
        session: Session,
        prefix: str,
        digest: str,
        *,
        now: datetime,
        network_zone: NetworkZone,
        target_instance_id: str | None,
        canonical_origin: str | None,
    ) -> tuple[Principal, bool, bool] | None:
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
            if (
                candidate.user_id is None
                or candidate.user_auth_revision is None
                or (raw_kind == "manager_bootstrap" and "app.admin" in scopes)
            ):
                return None
            try:
                state = account_state_in_session(
                    session,
                    candidate.user_id,
                    auth_revision=candidate.user_auth_revision,
                    require_admin="app.admin" in scopes,
                )
            except PermissionError:
                return None
            client_due = False
            if candidate.client_id:
                client = session.get(
                    AutomationClient,
                    candidate.client_id,
                )
                if (
                    client is None
                    or client.revoked_at is not None
                    or client.user_id != state.user_id
                    or client.user_auth_revision != state.auth_revision
                ):
                    return None
                if (
                    target_instance_id is not None
                    and client.target_instance_id != target_instance_id
                ):
                    return None
                if canonical_origin is not None and client.canonical_origin != canonical_origin:
                    return None
                try:
                    if not scopes <= normalize_scopes(client.allowed_scopes_json):
                        return None
                except ValueError:
                    return None
                client_due = self._activity_due(client.last_used_at, now)
            principal = Principal(
                subject=(
                    str(candidate.subject) if candidate.subject else f"api-token:{candidate.id}"
                ),
                kind=cast(PrincipalKind, raw_kind),
                scopes=scopes,
                token_id=candidate.id,
                network_zone=network_zone,
                target_instance_id=(
                    target_instance_id or str(candidate.target_instance_id or "")
                ),
                client_id=candidate.client_id,
                user_id=state.user_id,
                auth_revision=state.auth_revision,
            )
            return principal, self._activity_due(candidate.last_used_at, now), client_due
        return None

    def _touch_api_token(
        self, principal: Principal, now: datetime, *, token_due: bool, client_due: bool
    ) -> None:
        """Record "last used" at most once per interval; contention never fails a request."""
        cutoff = now - API_TOKEN_ACTIVITY_INTERVAL
        try:
            with self.database.immediate_session(
                busy_timeout_ms=API_TOKEN_TOUCH_TIMEOUT_MS
            ) as session:
                if token_due and principal.token_id is not None:
                    session.execute(
                        update(ApiToken)
                        .where(
                            ApiToken.id == principal.token_id,
                            ApiToken.revoked_at.is_(None),
                            or_(
                                ApiToken.last_used_at.is_(None),
                                ApiToken.last_used_at <= cutoff,
                                ApiToken.last_used_at > now,
                            ),
                        )
                        .values(last_used_at=now)
                        .execution_options(synchronize_session=False)
                    )
                if client_due and principal.client_id:
                    session.execute(
                        update(AutomationClient)
                        .where(
                            AutomationClient.id == principal.client_id,
                            AutomationClient.revoked_at.is_(None),
                            or_(
                                AutomationClient.last_used_at.is_(None),
                                AutomationClient.last_used_at <= cutoff,
                                AutomationClient.last_used_at > now,
                            ),
                        )
                        .values(last_used_at=now)
                        .execution_options(synchronize_session=False)
                    )
        except Exception as error:
            if not is_sqlite_busy(error):
                raise
            _LOGGER.debug("API token activity update skipped: database busy")

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

    def list_tokens(self, *, actor: UserActor | None = None) -> list[ApiToken]:
        with self.database.session() as session:
            statement = select(ApiToken).order_by(ApiToken.created_at.desc())
            if actor is not None:
                require_actor_in_session(session, actor, required_scopes={"app.credentials.read"})
                statement = statement.where(ApiToken.user_id == actor.user_id)
            tokens = list(session.scalars(statement).all())
            for token in tokens:
                session.expunge(token)
            return tokens

    def revoke_token(self, token_id: str, *, actor: UserActor | None = None) -> None:
        with self.database.immediate_session() as session:
            statement = select(ApiToken).where(ApiToken.id == token_id)
            if actor is not None:
                require_actor_in_session(session, actor, required_scopes={"app.credentials.write"})
                statement = statement.where(ApiToken.user_id == actor.user_id)
            token = session.scalar(statement)
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

