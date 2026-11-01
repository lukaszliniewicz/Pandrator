"""Password hashing helpers for account authentication primitives."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import OwnerAccount
from .models import User

MINIMUM_PASSWORD_LENGTH = 15
MAXIMUM_PASSWORD_LENGTH = 1024
_MAXIMUM_ENCODED_HASH_LENGTH = 4096
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD = "pandrator-invalid-account-dummy-password"


def validate_password(password: str, *, minimum_length: int = MINIMUM_PASSWORD_LENGTH) -> str:
    """Validate a new password without trimming or changing its Unicode text."""

    if not isinstance(password, str):
        raise ValueError("Password must be text.")
    if (
        isinstance(minimum_length, bool)
        or not isinstance(minimum_length, int)
        or minimum_length < 1
        or minimum_length > MAXIMUM_PASSWORD_LENGTH
    ):
        raise ValueError("Password minimum length is invalid.")
    if len(password) < minimum_length or len(password) > MAXIMUM_PASSWORD_LENGTH:
        raise ValueError(
            f"Passwords must contain between {minimum_length} and "
            f"{MAXIMUM_PASSWORD_LENGTH} characters."
        )
    return password


def hash_password(password: str, *, minimum_length: int = MINIMUM_PASSWORD_LENGTH) -> str:
    """Hash a validated password with the process's configured Argon2 hasher."""
    candidate = validate_password(password, minimum_length=minimum_length)
    try:
        return _PASSWORD_HASHER.hash(candidate)
    except UnicodeError:
        raise ValueError("Password text cannot be encoded safely.") from None


def verify_password(encoded: str, password: str) -> bool:
    """Verify an existing encoded hash without exposing parser or secret errors."""

    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > _MAXIMUM_ENCODED_HASH_LENGTH
        or not isinstance(password, str)
        or len(password) > MAXIMUM_PASSWORD_LENGTH
    ):
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(encoded, password))
    except (VerificationError, InvalidHashError, UnicodeError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    """Check an encoded hash after, and only after, successful verification."""

    if not isinstance(encoded, str) or not encoded or len(encoded) > _MAXIMUM_ENCODED_HASH_LENGTH:
        return False
    try:
        return _PASSWORD_HASHER.check_needs_rehash(encoded)
    except (VerificationError, InvalidHashError, UnicodeError):
        return False


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return _PASSWORD_HASHER.hash(_DUMMY_PASSWORD)


def dummy_verify(password: str) -> None:
    """Run one cached Argon2 verification for an unknown account name."""

    candidate = password if isinstance(password, str) else ""
    if len(candidate) > MAXIMUM_PASSWORD_LENGTH:
        candidate = candidate[:MAXIMUM_PASSWORD_LENGTH]
    try:
        _PASSWORD_HASHER.verify(_dummy_password_hash(), candidate)
    except (VerificationError, InvalidHashError, UnicodeError):
        pass


def mirror_initial_owner_password(
    session: Session,
    *,
    user_id: str,
    encoded: str | None,
    updated_at: datetime,
) -> None:
    """Mirror only the structural initial owner's password to legacy singleton 1.

    Call this from the caller's transaction after an initial-owner password
    change or successful hash re-encoding. It grants no authority and never
    commits the transaction. Existing ``created_at`` is preserved; a newly
    created projection uses ``updated_at`` for both timestamps.
    """

    owner_ids = list(session.scalars(select(User.id).where(User.legacy_subject == "owner")).all())
    if len(owner_ids) != 1 or str(owner_ids[0]) != user_id:
        return

    projection = session.get(OwnerAccount, 1)
    if encoded is None:
        if projection is not None:
            session.delete(projection)
        return

    if projection is None:
        session.add(
            OwnerAccount(
                singleton_id=1,
                password_hash=encoded,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
        return

    projection.password_hash = encoded
    projection.updated_at = updated_at
