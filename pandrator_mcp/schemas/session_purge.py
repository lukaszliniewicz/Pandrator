"""Strict MCP arguments for permanent session cleanup and trash policy."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import ToolInput


class SessionPurgeInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)

    @field_validator("session_id", mode="before")
    @classmethod
    def trim_session_id(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Session ID may not be blank.")
        return normalized


class PreviewSessionDeletionInput(SessionPurgeInput):
    pass


class DeleteSessionPermanentlyInput(SessionPurgeInput):
    expected_revision: int = Field(ge=0)
    impact_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    confirm: Literal[True]


class GetSessionTrashPolicyInput(ToolInput):
    pass


class UpdateSessionTrashPolicyInput(ToolInput):
    expected_revision: int = Field(ge=0)
    days: int | None = Field(ge=1, le=3650)


SESSION_PURGE_INPUT_MODELS = (
    PreviewSessionDeletionInput,
    DeleteSessionPermanentlyInput,
    GetSessionTrashPolicyInput,
    UpdateSessionTrashPolicyInput,
)


__all__ = [
    "DeleteSessionPermanentlyInput",
    "GetSessionTrashPolicyInput",
    "PreviewSessionDeletionInput",
    "SESSION_PURGE_INPUT_MODELS",
    "SessionPurgeInput",
    "UpdateSessionTrashPolicyInput",
]
