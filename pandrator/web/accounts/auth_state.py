"""Fresh account state for credential minting and use; no subject-based grants."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_models import UserCredential
from .models import User


@dataclass(frozen=True, slots=True)
class AccountState:
    user_id: str
    auth_revision: int
    login_name: str
    display_name: str
    is_instance_admin: bool


def account_state_in_session(
    session: Session,
    user_id: str,
    *,
    auth_revision: int | None = None,
    require_admin: bool = False,
) -> AccountState:
    """Select current columns, without trusting a cached User or claimed role."""
    statement = (
        select(
            User.id,
            UserCredential.auth_revision,
            UserCredential.login_name,
            User.display_name,
            User.is_instance_admin,
        )
        .join(UserCredential, UserCredential.user_id == User.id)
        .where(User.id == user_id, User.is_active.is_(True))
    )
    if auth_revision is not None:
        statement = statement.where(UserCredential.auth_revision == auth_revision)
    if require_admin:
        statement = statement.where(User.is_instance_admin.is_(True))
    with session.no_autoflush:
        row = session.execute(statement).one_or_none()
    if row is None:
        raise PermissionError("The account credential is inactive or has changed.")
    return AccountState(*row)


def initial_user_id(session: Session) -> str:
    """Resolve the adopted identity structurally, without conferring authority."""
    with session.no_autoflush:
        identifier = session.scalar(select(User.id).where(User.legacy_subject == "owner"))
    if identifier is None:
        raise RuntimeError("The migration-seeded initial user is missing.")
    return identifier


__all__ = ["AccountState", "account_state_in_session", "initial_user_id"]
