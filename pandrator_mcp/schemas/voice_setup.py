"""Strict MCP arguments for session voice-mode setup."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, StrictInt, StrictStr, field_validator

from .common import ToolInput


def _trim_session_id(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("Session ID may not be blank.")
    return normalized


class GetVoiceSetupInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)

    @field_validator("session_id", mode="before")
    @classmethod
    def trim_session_id(cls, value: Any) -> Any:
        return _trim_session_id(value)


class ConfigureVoiceSetupInput(GetVoiceSetupInput):
    expected_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    mode: Literal["single_voice", "multi_voice"]
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )


class SetupDesignedVoiceInput(ToolInput):
    """Resume preparation/publication only after explicit design sample review."""

    voice_id: StrictStr = Field(min_length=1, max_length=160)
    artifact_id: StrictStr = Field(min_length=1, max_length=160)
    transcript: StrictStr = Field(min_length=1, max_length=4_000)
    transcript_reviewed: Literal[True]
    language: StrictStr | None = Field(default=None, max_length=40)
    service_id: StrictStr = Field(min_length=1, max_length=160)
    expected_voice_revision: StrictInt = Field(ge=1)
    idempotency_key: StrictStr = Field(
        min_length=8, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )

    @field_validator("transcript_reviewed", mode="before")
    @classmethod
    def require_explicit_review(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("transcript_reviewed must be explicitly true.")
        return value


VOICE_SETUP_INPUT_MODELS = (
    GetVoiceSetupInput, ConfigureVoiceSetupInput, SetupDesignedVoiceInput,
)


__all__ = [
    "ConfigureVoiceSetupInput",
    "GetVoiceSetupInput",
    "SetupDesignedVoiceInput",
    "VOICE_SETUP_INPUT_MODELS",
]
