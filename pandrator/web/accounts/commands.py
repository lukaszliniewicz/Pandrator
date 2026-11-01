"""Transactional account administration commands without transport dependencies."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, NoReturn

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import Database
from ..model_base import utcnow
from ..models import ApiToken, AutomationClient, AutomationEnrollmentGrant
from .auth_models import BrowserAuthSession, PasswordResetGrant, UserCredential
from .models import User, Workspace, WorkspaceMembership
from .passwords import hash_password, mirror_initial_owner_password

_LOGIN_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_ADMIN_SCOPE = "app.admin"
_RESET_TOKEN_MAX_LENGTH = 256
_INVALID_RESET_CODE = "invalid_reset"
_INVALID_RESET_MESSAGE = "The password reset link is invalid or expired."


class AccountCommandError(Exception):
    """Safe command failure data for an adapter to map to its own response."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AccountActor:
    user_id: str
    auth_revision: int
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", frozenset(str(scope) for scope in self.scopes))


@dataclass(frozen=True, slots=True)
class AccountSummary:
    user_id: str
    login_name: str
    display_name: str
    role: str
    is_active: bool
    auth_revision: int
    password_configured: bool = False


@dataclass(frozen=True, slots=True)
class IssuedResetGrant:
    grant_id: str
    user_id: str
    expires_at: datetime
    raw_token: str = field(repr=False)


def _normalize_login_name(login_name: str) -> str:
    if not isinstance(login_name, str):
        raise AccountCommandError("invalid_login_name", "Login name is invalid.", 400)
    normalized = login_name.strip().lower()
    if not normalized.isascii() or not _LOGIN_NAME_PATTERN.fullmatch(normalized):
        raise AccountCommandError(
            "invalid_login_name",
            "Login names must use 1 to 64 ASCII letters, numbers, dots, underscores, or hyphens, and start with a letter or number.",
            400,
        )
    return normalized


def _normalize_display_name(display_name: str) -> str:
    if not isinstance(display_name, str):
        raise AccountCommandError("invalid_display_name", "Display name is invalid.", 400)
    normalized = display_name.strip()
    if not normalized or len(normalized) > 255:
        raise AccountCommandError(
            "invalid_display_name",
            "Display names must contain between 1 and 255 characters.",
            400,
        )
    return normalized


def _normalize_role(role: str) -> str:
    if not isinstance(role, str):
        raise AccountCommandError("invalid_role", "Role must be user or administrator.", 400)
    normalized = role.strip().lower()
    if normalized not in {"user", "administrator"}:
        raise AccountCommandError("invalid_role", "Role must be user or administrator.", 400)
    return normalized


def _summary(user: User, credential: UserCredential) -> AccountSummary:
    return AccountSummary(
        user_id=user.id,
        login_name=credential.login_name,
        display_name=user.display_name,
        role="administrator" if user.is_instance_admin else "user",
        is_active=user.is_active,
        auth_revision=credential.auth_revision,
        password_configured=bool(credential.password_hash),
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _reset_digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _raise_invalid_reset() -> NoReturn:
    raise AccountCommandError(
        _INVALID_RESET_CODE,
        _INVALID_RESET_MESSAGE,
        400,
    )


def revoke_user_credentials(session: Session, user_id: str, now: datetime) -> None:
    """Revoke only credentials and delegations linked to this user.

    The caller owns the transaction and must have already advanced the user's
    authentication revision when this is part of an account security change.
    This also revokes unused password resets issued by the user, so a later
    demotion cannot leave delegated reset authority active. Legacy rows without
    a user binding are intentionally left untouched.
    """

    session.execute(
        update(BrowserAuthSession)
        .where(
            BrowserAuthSession.user_id == user_id,
            BrowserAuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    session.execute(
        update(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    client_ids = list(
        session.scalars(
            select(AutomationClient.id).where(AutomationClient.user_id == user_id)
        ).all()
    )
    if client_ids:
        session.execute(
            update(AutomationEnrollmentGrant)
            .where(
                AutomationEnrollmentGrant.client_id.in_(client_ids),
                AutomationEnrollmentGrant.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
    session.execute(
        update(AutomationClient)
        .where(
            AutomationClient.user_id == user_id,
            AutomationClient.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    session.execute(
        update(PasswordResetGrant)
        .where(
            or_(
                PasswordResetGrant.user_id == user_id,
                PasswordResetGrant.created_by_user_id == user_id,
            ),
            PasswordResetGrant.consumed_at.is_(None),
            PasswordResetGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


class AccountCommands:
    """Account administration and reset commands bound to one database."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.database = database
        self.clock = clock

    def _now(self) -> datetime:
        return _aware_utc(self.clock())

    @staticmethod
    def _require_admin(session: Session, actor: AccountActor) -> None:
        if _ADMIN_SCOPE not in actor.scopes:
            raise AccountCommandError("forbidden", "Administrator access is required.", 403)
        if (
            not actor.user_id
            or isinstance(actor.auth_revision, bool)
            or not isinstance(actor.auth_revision, int)
            or actor.auth_revision < 1
        ):
            raise AccountCommandError(
                "stale_actor", "Administrator access is no longer valid.", 401
            )
        current_actor_id = session.scalar(
            select(User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                User.id == actor.user_id,
                User.is_active.is_(True),
                User.is_instance_admin.is_(True),
                UserCredential.auth_revision == actor.auth_revision,
            )
        )
        if current_actor_id is None:
            raise AccountCommandError(
                "stale_actor", "Administrator access is no longer valid.", 401
            )

    @staticmethod
    def _account_row(session: Session, user_id: str) -> tuple[User, UserCredential]:
        row = session.execute(
            select(User, UserCredential)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(User.id == user_id)
        ).one_or_none()
        if row is None:
            raise AccountCommandError("account_not_found", "Account was not found.", 404)
        return row[0], row[1]

    @staticmethod
    def _check_expected_revision(credential: UserCredential, expected_auth_revision: int) -> None:
        if (
            isinstance(expected_auth_revision, bool)
            or not isinstance(expected_auth_revision, int)
            or expected_auth_revision < 1
            or credential.auth_revision != expected_auth_revision
        ):
            raise AccountCommandError(
                "stale_account",
                "The account changed. Refresh the account list and try again.",
                409,
            )

    @staticmethod
    def _has_other_ready_administrator(session: Session, target_user_id: str) -> bool:
        return (
            session.scalar(
                select(User.id)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(
                    User.id != target_user_id,
                    User.is_active.is_(True),
                    User.is_instance_admin.is_(True),
                    UserCredential.password_hash.is_not(None),
                    UserCredential.password_hash != "",
                )
                .limit(1)
            )
            is not None
        )

    def list_users(self, actor: AccountActor) -> list[AccountSummary]:
        with self.database.immediate_session() as session:
            return self.list_users_in_session(session, actor)

    def list_users_in_session(
        self, session: Session, actor: AccountActor
    ) -> list[AccountSummary]:
        self._require_admin(session, actor)
        rows = session.execute(
            select(User, UserCredential)
            .join(UserCredential, UserCredential.user_id == User.id)
            .order_by(UserCredential.login_name)
        ).all()
        return [_summary(user, credential) for user, credential in rows]

    def create_user(
        self,
        actor: AccountActor,
        *,
        login_name: str,
        display_name: str,
        role: str = "user",
    ) -> AccountSummary:
        try:
            with self.database.immediate_session() as session:
                return self.create_user_in_session(
                    session,
                    actor,
                    login_name=login_name,
                    display_name=display_name,
                    role=role,
                )
        except IntegrityError:
            raise AccountCommandError(
                "login_name_taken", "That login name is already in use.", 409
            ) from None

    def create_user_in_session(
        self,
        session: Session,
        actor: AccountActor,
        *,
        login_name: str,
        display_name: str,
        role: str = "user",
    ) -> AccountSummary:
        self._require_admin(session, actor)
        normalized_login = _normalize_login_name(login_name)
        normalized_display = _normalize_display_name(display_name)
        normalized_role = _normalize_role(role)
        existing = session.scalar(
            select(UserCredential.user_id).where(
                UserCredential.login_name == normalized_login
            )
        )
        if existing is not None:
            raise AccountCommandError(
                "login_name_taken", "That login name is already in use.", 409
            )

        now = self._now()
        user = User(
            legacy_subject=None,
            display_name=normalized_display,
            is_active=True,
            is_instance_admin=normalized_role == "administrator",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush([user])
        credential = UserCredential(
            user_id=user.id,
            login_name=normalized_login,
            password_hash=None,
            auth_revision=1,
            created_at=now,
            updated_at=now,
        )
        workspace = Workspace(
            kind="personal",
            name=normalized_display,
            personal_owner_user_id=user.id,
            state="active",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add_all([credential, workspace])
        session.flush([credential, workspace])
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role="owner",
                created_at=now,
            )
        )
        session.flush()
        return _summary(user, credential)

    def update_user(
        self,
        actor: AccountActor,
        target_user_id: str,
        *,
        expected_auth_revision: int,
        display_name: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> AccountSummary:
        with self.database.immediate_session() as session:
            return self.update_user_in_session(
                session,
                actor,
                target_user_id,
                expected_auth_revision=expected_auth_revision,
                display_name=display_name,
                role=role,
                is_active=is_active,
            )

    def update_user_in_session(
        self,
        session: Session,
        actor: AccountActor,
        target_user_id: str,
        *,
        expected_auth_revision: int,
        display_name: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> AccountSummary:
        self._require_admin(session, actor)
        user, credential = self._account_row(session, target_user_id)
        self._check_expected_revision(credential, expected_auth_revision)

        next_display_name = (
            _normalize_display_name(display_name)
            if display_name is not None
            else user.display_name
        )
        next_is_admin = (
            _normalize_role(role) == "administrator"
            if role is not None
            else user.is_instance_admin
        )
        if is_active is not None and not isinstance(is_active, bool):
            raise AccountCommandError(
                "invalid_account_state", "Active state must be true or false.", 400
            )
        next_is_active = is_active if is_active is not None else user.is_active
        security_changed = (
            next_is_admin != user.is_instance_admin or next_is_active != user.is_active
        )
        current_active_admin = user.is_active and user.is_instance_admin
        next_active_admin = next_is_active and next_is_admin
        if (
            current_active_admin
            and not next_active_admin
            and not self._has_other_ready_administrator(session, user.id)
        ):
            raise AccountCommandError(
                "last_administrator",
                "Another active Administrator with a configured password is required before this account can be retired.",
                409,
            )

        now = self._now()
        changed = next_display_name != user.display_name or security_changed
        if changed:
            user.display_name = next_display_name
            user.is_instance_admin = next_is_admin
            user.is_active = next_is_active
            user.updated_at = now
        if security_changed:
            credential.auth_revision += 1
            credential.updated_at = now
            revoke_user_credentials(session, user.id, now)
        session.flush()
        return _summary(user, credential)

    def issue_password_reset(
        self,
        actor: AccountActor,
        target_user_id: str,
        *,
        expected_auth_revision: int,
        ttl_seconds: int = 1800,
    ) -> IssuedResetGrant:
        with self.database.immediate_session() as session:
            return self.issue_password_reset_in_session(
                session,
                actor,
                target_user_id,
                expected_auth_revision=expected_auth_revision,
                ttl_seconds=ttl_seconds,
            )

    def issue_password_reset_in_session(
        self,
        session: Session,
        actor: AccountActor,
        target_user_id: str,
        *,
        expected_auth_revision: int,
        ttl_seconds: int = 1800,
    ) -> IssuedResetGrant:
        self._require_admin(session, actor)
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 60 <= ttl_seconds <= 3600
        ):
            raise AccountCommandError(
                "invalid_reset_ttl",
                "Reset grant lifetime must be between 60 and 3600 seconds.",
                400,
            )
        user, credential = self._account_row(session, target_user_id)
        self._check_expected_revision(credential, expected_auth_revision)
        if not user.is_active:
            raise AccountCommandError(
                "account_disabled",
                "Re-enable the account before issuing a password reset.",
                409,
            )

        now = self._now()
        session.execute(
            update(PasswordResetGrant)
            .where(
                PasswordResetGrant.user_id == user.id,
                PasswordResetGrant.consumed_at.is_(None),
                PasswordResetGrant.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        raw_token = secrets.token_urlsafe(32)
        grant = PasswordResetGrant(
            user_id=user.id,
            created_by_user_id=actor.user_id,
            token_hash=_reset_digest(raw_token),
            auth_revision=credential.auth_revision,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        session.add(grant)
        session.flush([grant])
        return IssuedResetGrant(
            grant_id=grant.id,
            user_id=user.id,
            expires_at=grant.expires_at,
            raw_token=raw_token,
        )

    @staticmethod
    def _reset_proof_is_valid(
        grant: PasswordResetGrant,
        user: User,
        credential: UserCredential,
        now: datetime,
    ) -> bool:
        return (
            grant.consumed_at is None
            and grant.revoked_at is None
            and _aware_utc(grant.expires_at) > now
            and user.is_active
            and grant.auth_revision == credential.auth_revision
        )

    def redeem_password_reset(self, raw_token: str, new_password: str) -> None:
        if (
            not isinstance(raw_token, str)
            or not raw_token
            or not raw_token.isascii()
            or len(raw_token) > _RESET_TOKEN_MAX_LENGTH
        ):
            _raise_invalid_reset()
        token_hash = _reset_digest(raw_token)
        now = self._now()
        with self.database.snapshot_session() as session:
            row = session.execute(
                select(PasswordResetGrant, User, UserCredential)
                .join(User, User.id == PasswordResetGrant.user_id)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(PasswordResetGrant.token_hash == token_hash)
            ).one_or_none()
            if row is None or not self._reset_proof_is_valid(row[0], row[1], row[2], now):
                _raise_invalid_reset()

        try:
            encoded = hash_password(new_password)
        except ValueError:
            raise AccountCommandError(
                "invalid_password",
                "Passwords must contain between 15 and 1024 characters.",
                400,
            ) from None

        with self.database.immediate_session() as session:
            row = session.execute(
                select(PasswordResetGrant, User, UserCredential)
                .join(User, User.id == PasswordResetGrant.user_id)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(PasswordResetGrant.token_hash == token_hash)
            ).one_or_none()
            now = self._now()
            if row is None or not self._reset_proof_is_valid(row[0], row[1], row[2], now):
                _raise_invalid_reset()

            grant, user, credential = row
            grant.consumed_at = now
            session.flush([grant])
            credential.password_hash = encoded
            credential.auth_revision += 1
            credential.updated_at = now
            user.updated_at = now
            revoke_user_credentials(session, user.id, now)
            mirror_initial_owner_password(
                session,
                user_id=user.id,
                encoded=credential.password_hash,
                updated_at=credential.updated_at,
            )
            session.flush()
