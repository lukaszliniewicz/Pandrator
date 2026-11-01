"""Database-backed browser sessions with revocable user credential epochs."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..database import Database
from ..model_base import utcnow
from ..models import AutomationClient
from .auth_contracts import PRINCIPAL_KINDS, PrincipalKind, normalize_scopes
from .auth_models import BrowserAuthSession, UserCredential
from .credential_resources import (
    CredentialOwner,
    CredentialResources,
    CredentialResourcesDenied,
    default_personal_resources,
    record_resources,
    require_resources_within,
    resources_for_owner,
    validate_resources_for_user,
)
from .models import User

_MANAGER_PROVENANCE_CLIENT_IDS = frozenset({"pandrator-manager-browser", "pandrator-mcp-local"})
_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_RAW_TOKEN_LENGTH = 256


@dataclass(frozen=True, slots=True)
class IssuedBrowserSession:
    record_id: str
    raw_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BrowserSessionPrincipal:
    user_id: str
    auth_revision: int
    subject: str
    kind: PrincipalKind
    scopes: frozenset[str]
    client_id: str | None
    session_id: str


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validated_identifier(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        raise ValueError(f"The browser session {label} is invalid.")
    return value


def _validated_ttl(value: int, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_TTL_SECONDS:
        raise ValueError(f"The browser session {label} must be between 1 second and 30 days.")
    return value


def _approved_client_scopes(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise ValueError("The automation client approved scopes are invalid.")
    try:
        return normalize_scopes(value)
    except ValueError as error:
        raise ValueError("The automation client approved scopes are invalid.") from error


class BrowserSessionStore:
    def __init__(self, database: Database):
        self.database = database

    def issue(
        self,
        actor_id: str,
        auth_revision: int,
        *,
        subject: str,
        principal_kind: str,
        scopes: object,
        client_id: str | None = None,
        idle_seconds: int = 43200,
        absolute_seconds: int = 604800,
        resource_grants: CredentialResources | None = None,
    ) -> IssuedBrowserSession:
        actor_id = _validated_identifier(actor_id, "user ID", 36)
        if type(auth_revision) is not int or auth_revision < 1:
            raise ValueError("The browser session authentication revision is invalid.")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            raise ValueError("The browser session subject is invalid.")
        if not isinstance(principal_kind, str) or principal_kind not in PRINCIPAL_KINDS:
            raise ValueError("The browser session principal kind is invalid.")
        selected_scopes = normalize_scopes(scopes)
        idle_seconds = _validated_ttl(idle_seconds, "idle lifetime")
        absolute_seconds = _validated_ttl(absolute_seconds, "absolute lifetime")
        if idle_seconds > absolute_seconds:
            raise ValueError(
                "The browser session idle lifetime cannot exceed its absolute lifetime."
            )
        if client_id is not None:
            client_id = _validated_identifier(client_id, "client ID", 200)
            if client_id not in _MANAGER_PROVENANCE_CLIENT_IDS:
                client_id = _validated_identifier(client_id, "client ID", 36)
        if principal_kind == "manager_bootstrap" and "app.admin" in selected_scopes:
            raise ValueError("Manager bootstrap sessions cannot include app.admin.")
        interactive = principal_kind == "owner_session" and client_id is None
        if interactive and resource_grants is not None:
            raise ValueError("Interactive browser sessions do not accept delegated resource grants.")

        raw_token = secrets.token_urlsafe(32)
        token_hash = _token_hash(raw_token)
        expires_at: datetime
        with self.database.immediate_session() as session:
            row = session.execute(
                select(User, UserCredential)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(
                    User.id == actor_id,
                    User.is_active.is_(True),
                    UserCredential.auth_revision == auth_revision,
                )
                .execution_options(populate_existing=True)
            ).one_or_none()
            if row is None:
                raise ValueError(
                    "The browser session user is inactive or its authentication revision is stale."
                )
            user, _credential = row
            if "app.admin" in selected_scopes and not user.is_instance_admin:
                raise ValueError("Only a current instance administrator may receive app.admin.")
            if client_id is not None and client_id not in _MANAGER_PROVENANCE_CLIENT_IDS:
                client = session.scalar(
                    select(AutomationClient)
                    .where(
                        AutomationClient.id == client_id,
                        AutomationClient.user_id == actor_id,
                        AutomationClient.user_auth_revision == auth_revision,
                        AutomationClient.revoked_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                )
                if client is None:
                    raise ValueError(
                        "The browser session client is missing, revoked, or belongs to another user epoch."
                    )
                approved_scopes = _approved_client_scopes(client.allowed_scopes_json)
                if not selected_scopes <= approved_scopes:
                    raise ValueError(
                        "The browser session scopes exceed the client's approved scopes."
                    )
            selected_resources = None
            if not interactive:
                ceiling = (
                    resources_for_owner(session, CredentialOwner.CLIENT, client_id)
                    if client_id is not None and client_id not in _MANAGER_PROVENANCE_CLIENT_IDS
                    else default_personal_resources(session, actor_id)
                )
                if principal_kind == "project_link":
                    from ..public_links.authorization import require_project_link_governance

                    if (client_id is not None or resource_grants is None
                            or resource_grants.workspace_ids or len(resource_grants.project_ids) != 1):
                        raise ValueError("Project link sessions require exactly one explicit project.")
                    require_project_link_governance(
                        session, actor_id, auth_revision, next(iter(resource_grants.project_ids)),
                    )
                    ceiling = resource_grants
                selected_resources = ceiling if resource_grants is None else resource_grants
                try:
                    selected_resources = validate_resources_for_user(
                        session, actor_id, selected_resources
                    )
                    require_resources_within(session, selected_resources, ceiling)
                except CredentialResourcesDenied as error:
                    raise ValueError(
                        "The browser session resource approval is no longer available."
                    ) from error
            created_at = utcnow()
            expires_at = created_at + timedelta(seconds=absolute_seconds)
            idle_expires_at = min(created_at + timedelta(seconds=idle_seconds), expires_at)
            record = BrowserAuthSession(
                user_id=actor_id,
                token_hash=token_hash,
                auth_revision=auth_revision,
                subject=subject,
                principal_kind=principal_kind,
                scopes_json=sorted(selected_scopes),
                client_id=client_id,
                created_at=created_at,
                last_seen_at=created_at,
                idle_expires_at=idle_expires_at,
                expires_at=expires_at,
            )
            session.add(record)
            session.flush()
            record_id = record.id
            if selected_resources is not None:
                record_resources(session, CredentialOwner.BROWSER, record_id, selected_resources)
        return IssuedBrowserSession(record_id, raw_token, expires_at)

    def resolve(self, raw_token: str) -> BrowserSessionPrincipal | None:
        if (
            not isinstance(raw_token, str)
            or not raw_token
            or len(raw_token) > _MAX_RAW_TOKEN_LENGTH
        ):
            return None
        digest = _token_hash(raw_token)
        with self.database.snapshot_session() as session:
            principal, touch_needed = self._resolve_in_session(
                session, digest, update_activity=False
            )
        if not touch_needed:
            return principal
        with self.database.immediate_session() as session:
            principal, _touch_needed = self._resolve_in_session(
                session, digest, update_activity=True
            )
            return principal

    @staticmethod
    def _resolve_in_session(
        session: Session, digest: str, *, update_activity: bool
    ) -> tuple[BrowserSessionPrincipal | None, bool]:
        row = session.execute(
            select(BrowserAuthSession, User, UserCredential)
            .join(User, User.id == BrowserAuthSession.user_id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                BrowserAuthSession.token_hash == digest,
                BrowserAuthSession.revoked_at.is_(None),
                User.is_active.is_(True),
                UserCredential.auth_revision == BrowserAuthSession.auth_revision,
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            return None, False
        record, user, _credential = row
        now = utcnow()
        absolute_expiry = _aware(record.expires_at)
        idle_expiry = _aware(record.idle_expires_at)
        last_seen = _aware(record.last_seen_at)
        if absolute_expiry <= now or idle_expiry <= now or idle_expiry <= last_seen:
            return None, False
        raw_kind = str(record.principal_kind or "")
        if raw_kind not in PRINCIPAL_KINDS:
            return None, False
        if not isinstance(record.scopes_json, list):
            return None, False
        try:
            selected_scopes = normalize_scopes(record.scopes_json)
        except (TypeError, ValueError):
            return None, False
        if (raw_kind == "manager_bootstrap" and "app.admin" in selected_scopes) or (
            "app.admin" in selected_scopes and not user.is_instance_admin
        ):
            return None, False
        if (
            not isinstance(record.subject, str)
            or not record.subject.strip()
            or len(record.subject) > 200
        ):
            return None, False
        if record.client_id is not None:
            if (
                not isinstance(record.client_id, str)
                or not record.client_id
                or len(record.client_id) > 200
            ):
                return None, False
            if record.client_id not in _MANAGER_PROVENANCE_CLIENT_IDS:
                if len(record.client_id) > 36:
                    return None, False
                client = session.scalar(
                    select(AutomationClient)
                    .where(
                        AutomationClient.id == record.client_id,
                        AutomationClient.user_id == user.id,
                        AutomationClient.user_auth_revision == record.auth_revision,
                        AutomationClient.revoked_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                )
                if client is None:
                    return None, False
                try:
                    approved_scopes = _approved_client_scopes(client.allowed_scopes_json)
                except ValueError:
                    return None, False
                if not selected_scopes <= approved_scopes:
                    return None, False
        idle_window = idle_expiry - last_seen
        touch_interval = min(timedelta(seconds=30), idle_window / 100)
        touch_needed = now - last_seen >= touch_interval
        if update_activity and touch_needed:
            if idle_expiry >= absolute_expiry:
                next_idle_expiry = absolute_expiry
            else:
                next_idle_expiry = min(now + idle_window, absolute_expiry)
            record.last_seen_at = now
            record.idle_expires_at = next_idle_expiry
            session.flush()
        principal = BrowserSessionPrincipal(
            user_id=user.id,
            auth_revision=record.auth_revision,
            subject=record.subject,
            kind=cast(PrincipalKind, raw_kind),
            scopes=selected_scopes,
            client_id=record.client_id,
            session_id=record.id,
        )
        return principal, touch_needed

    def revoke(self, raw_token: str) -> None:
        if (
            not isinstance(raw_token, str)
            or not raw_token
            or len(raw_token) > _MAX_RAW_TOKEN_LENGTH
        ):
            return
        digest = _token_hash(raw_token)
        with self.database.immediate_session() as session:
            session.execute(
                update(BrowserAuthSession)
                .where(
                    BrowserAuthSession.token_hash == digest,
                    BrowserAuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow())
                .execution_options(synchronize_session=False)
            )

    def revoke_user(self, user_id: str) -> None:
        user_id = _validated_identifier(user_id, "user ID", 36)
        with self.database.immediate_session() as session:
            self.revoke_user_in_session(session, user_id)

    @staticmethod
    def revoke_user_in_session(session: Session, user_id: str) -> None:
        user_id = _validated_identifier(user_id, "user ID", 36)
        session.execute(
            update(BrowserAuthSession)
            .where(
                BrowserAuthSession.user_id == user_id,
                BrowserAuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
            .execution_options(synchronize_session=False)
        )


__all__ = [
    "BrowserSessionPrincipal",
    "BrowserSessionStore",
    "IssuedBrowserSession",
]
