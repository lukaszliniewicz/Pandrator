"""Fresh account and transport validation for captured execution actors."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..model_base import utcnow
from ..models import ApiToken, AutomationClient
from .auth_models import BrowserAuthSession
from .auth_state import AccountState, account_state_in_session
from .execution_actor import (
    _MANAGER_BROWSER_CLIENT_IDS,
    HostActor,
    UserActor,
    _scope_set,
    _validate_user_actor,
)

_DENIED = "The execution actor is not currently authorized."


class ActorAuthorizationDenied(PermissionError):
    """An execution credential failed a current application authority check."""


def _deny() -> ActorAuthorizationDenied:
    return ActorAuthorizationDenied(_DENIED)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stored_scopes(value: object) -> frozenset[str] | None:
    if type(value) is not list:
        return None
    try:
        return _scope_set(value)
    except ValueError:
        return None


def _has_required_scopes(grant: frozenset[str], required: frozenset[str]) -> bool:
    return required <= grant


def _client_is_current(
    session: Session,
    actor: UserActor,
    client_id: str,
) -> bool:
    row = session.execute(
        select(
            AutomationClient.user_id,
            AutomationClient.user_auth_revision,
            AutomationClient.revoked_at,
            AutomationClient.allowed_scopes_json,
        ).where(AutomationClient.id == client_id)
    ).one_or_none()
    if (
        row is None
        or row.user_id != actor.user_id
        or row.user_auth_revision != actor.auth_revision
        or row.revoked_at is not None
    ):
        return False
    approved_scopes = _stored_scopes(row.allowed_scopes_json)
    return approved_scopes is not None and actor.scopes <= approved_scopes


def _resolve_api_token(
    session: Session,
    actor: UserActor,
    now: datetime,
    *,
    require_unexpired: bool = True,
) -> frozenset[str] | None:
    if type(require_unexpired) is not bool:
        return None
    if actor.token_id is None:
        return None
    row = session.execute(
        select(
            ApiToken.user_id,
            ApiToken.user_auth_revision,
            ApiToken.revoked_at,
            ApiToken.expires_at,
            ApiToken.subject,
            ApiToken.principal_kind,
            ApiToken.scopes_json,
            ApiToken.client_id,
        ).where(ApiToken.id == actor.token_id)
    ).one_or_none()
    if (
        row is None
        or row.user_id != actor.user_id
        or row.user_auth_revision != actor.auth_revision
        or row.revoked_at is not None
        or (
            require_unexpired
            and row.expires_at is not None
            and _aware(row.expires_at) <= now
        )
    ):
        return None
    subject = row.subject or f"api-token:{actor.token_id}"
    principal_kind = row.principal_kind or "api_token"
    if (
        type(subject) is not str
        or subject != actor.subject
        or principal_kind != actor.principal_kind
        or row.client_id != actor.client_id
    ):
        return None
    scopes = _stored_scopes(row.scopes_json)
    if scopes is None or not actor.scopes <= scopes:
        return None
    if actor.client_id is not None and not _client_is_current(session, actor, actor.client_id):
        return None
    return scopes


def _resolve_browser_session(
    session: Session,
    actor: UserActor,
    now: datetime,
) -> frozenset[str] | None:
    if actor.browser_session_id is None:
        return None
    row = session.execute(
        select(
            BrowserAuthSession.user_id,
            BrowserAuthSession.auth_revision,
            BrowserAuthSession.revoked_at,
            BrowserAuthSession.expires_at,
            BrowserAuthSession.idle_expires_at,
            BrowserAuthSession.last_seen_at,
            BrowserAuthSession.subject,
            BrowserAuthSession.principal_kind,
            BrowserAuthSession.scopes_json,
            BrowserAuthSession.client_id,
        ).where(BrowserAuthSession.id == actor.browser_session_id)
    ).one_or_none()
    if (
        row is None
        or row.user_id != actor.user_id
        or row.auth_revision != actor.auth_revision
        or row.revoked_at is not None
        or type(row.expires_at) is not datetime
        or type(row.idle_expires_at) is not datetime
        or type(row.last_seen_at) is not datetime
    ):
        return None
    absolute_expiry = _aware(row.expires_at)
    idle_expiry = _aware(row.idle_expires_at)
    last_seen = _aware(row.last_seen_at)
    if absolute_expiry <= now or idle_expiry <= now or idle_expiry <= last_seen:
        return None
    if (
        type(row.subject) is not str
        or row.subject != actor.subject
        or row.principal_kind != actor.principal_kind
        or row.client_id != actor.client_id
    ):
        return None
    scopes = _stored_scopes(row.scopes_json)
    if scopes is None or not actor.scopes <= scopes:
        return None
    if (
        actor.client_id is not None
        and actor.client_id not in _MANAGER_BROWSER_CLIENT_IDS
        and not _client_is_current(session, actor, actor.client_id)
    ):
        return None
    return scopes


def require_actor_in_session(
    session: Session,
    actor: UserActor | HostActor | None,
    *,
    required_scopes: object = (),
    require_admin: bool = False,
    allow_host: bool = False,
) -> AccountState | None:
    """Revalidate a captured actor without owning the caller's transaction."""
    if type(require_admin) is not bool or type(allow_host) is not bool:
        raise _deny()
    if actor is None:
        raise _deny()
    if type(actor) is HostActor:
        if actor.label != "pandrator-cli" or not allow_host:
            raise _deny()
        return None
    if type(actor) is not UserActor:
        raise _deny()
    try:
        _validate_user_actor(actor)
        required = _scope_set(required_scopes, allow_empty=True)
    except ValueError as error:
        raise _deny() from error

    now = _aware(utcnow())
    try:
        account = account_state_in_session(
            session,
            actor.user_id,
            auth_revision=actor.auth_revision,
            require_admin=require_admin or "app.admin" in actor.scopes,
        )
    except PermissionError as error:
        raise _deny() from error

    if actor.token_id is not None:
        current_scopes = _resolve_api_token(session, actor, now)
    else:
        current_scopes = _resolve_browser_session(session, actor, now)
    if (
        current_scopes is None
        or not _has_required_scopes(actor.scopes, required)
        or not _has_required_scopes(current_scopes, required)
    ):
        raise _deny()
    if actor.principal_kind == "project_link":
        from ..public_links.authorization import require_project_link_actor_in_session

        require_project_link_actor_in_session(session, actor)
    return account
