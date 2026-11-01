"""Strict request and response models for account administration HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

AccountRole = Literal["user", "administrator"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreateRequest(_StrictModel):
    login_name: StrictStr = Field(max_length=4096)
    display_name: StrictStr = Field(max_length=4096)
    role: AccountRole = "user"


class AccountUpdateRequest(_StrictModel):
    expected_auth_revision: StrictInt = Field(ge=1)
    display_name: StrictStr | None = Field(default=None, max_length=4096)
    role: AccountRole | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="after")
    def require_non_null_update(self) -> AccountUpdateRequest:
        update_fields = {"display_name", "role", "is_active"}
        supplied = self.model_fields_set & update_fields
        if not supplied:
            raise ValueError("At least one account update field is required.")
        if any(getattr(self, field) is None for field in supplied):
            raise ValueError("Account update fields cannot be null.")
        return self


class AccountResetIssueRequest(_StrictModel):
    expected_auth_revision: StrictInt = Field(ge=1)


class AccountResetRedeemRequest(_StrictModel):
    token: StrictStr = Field(min_length=1, max_length=256)
    new_password: StrictStr = Field(min_length=15, max_length=1024)

    @field_validator("token")
    @classmethod
    def token_must_be_ascii(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("The reset token must contain only ASCII characters.")
        return value


class AccountSummaryResponse(_StrictModel):
    user_id: StrictStr
    login_name: StrictStr
    display_name: StrictStr
    role: AccountRole
    is_active: StrictBool
    auth_revision: StrictInt
    password_configured: StrictBool


class AccountListResponse(_StrictModel):
    items: list[AccountSummaryResponse]
    initial_user_id: StrictStr
    named_user_access: StrictBool = False


class AccountResetIssueResponse(_StrictModel):
    grant_id: StrictStr
    user_id: StrictStr
    expires_at: datetime
    reset_path: StrictStr


__all__ = [
    "AccountCreateRequest",
    "AccountListResponse",
    "AccountResetIssueRequest",
    "AccountResetIssueResponse",
    "AccountResetRedeemRequest",
    "AccountSummaryResponse",
    "AccountUpdateRequest",
]
