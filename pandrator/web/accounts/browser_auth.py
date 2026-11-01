"""Cookie transport for the application's revocable browser authentication."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from flask import session

from .auth_contracts import PrincipalKind
from .auth_state import AccountState

if TYPE_CHECKING:
    from ..auth import AuthService


def establish_browser_session(
    auth: AuthService,
    account: AccountState,
    *,
    subject: str,
    principal_kind: PrincipalKind,
    scopes: object,
    client_id: str | None = None,
) -> None:
    """Replace any earlier browser credential after successful authentication."""
    previous = session.get("auth_session")
    if isinstance(previous, str):
        auth.browser_sessions.revoke(previous)
    issued = auth.browser_sessions.issue(
        account.user_id,
        account.auth_revision,
        subject=subject,
        principal_kind=principal_kind,
        scopes=scopes,
        client_id=client_id,
    )
    session.clear()
    session["authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(24)
    session["auth_session"] = issued.raw_token


def revoke_browser_session(auth: AuthService) -> None:
    raw = session.get("auth_session")
    if isinstance(raw, str):
        auth.browser_sessions.revoke(raw)
    session.clear()


__all__ = ["establish_browser_session", "revoke_browser_session"]
