"""Typed MCP inputs for application-account administration."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .common import ToolInput

_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"


class _StrictAccountInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListUsersInput(_StrictAccountInput):
    """List application accounts and their password setup state."""


class CreateUserInput(_StrictAccountInput):
    """Create a user or administrator account."""

    login_name: str = Field(max_length=4096)
    display_name: str = Field(max_length=4096)
    role: Literal["user", "administrator"] = "user"
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_IDEMPOTENCY_PATTERN,
    )


class UserAccountChanges(_StrictAccountInput):
    """Supported fields for a revision-checked account update."""

    display_name: str | None = Field(default=None, max_length=4096)
    role: Literal["user", "administrator"] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_non_null_change(self) -> UserAccountChanges:
        if not self.model_fields_set or any(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("At least one non-null account change is required.")
        return self


class UpdateUserInput(_StrictAccountInput):
    """Update an account at its current authentication revision."""

    user_id: str = Field(min_length=1, max_length=64)
    expected_auth_revision: int = Field(gt=0, strict=True)
    changes: UserAccountChanges
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_IDEMPOTENCY_PATTERN,
    )

    @field_validator("user_id")
    @classmethod
    def user_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_id must not be blank.")
        return value


class IssueUserPasswordResetInput(_StrictAccountInput):
    """Issue a one-use password setup link for one account."""

    user_id: str = Field(min_length=1, max_length=64)
    expected_auth_revision: int = Field(gt=0, strict=True)

    @field_validator("user_id")
    @classmethod
    def user_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_id must not be blank.")
        return value


ACCOUNT_INPUT_MODELS = (
    ListUsersInput,
    CreateUserInput,
    UpdateUserInput,
    IssueUserPasswordResetInput,
)

__all__ = [
    "ACCOUNT_INPUT_MODELS",
    "CreateUserInput",
    "IssueUserPasswordResetInput",
    "ListUsersInput",
    "UpdateUserInput",
    "UserAccountChanges",
]
