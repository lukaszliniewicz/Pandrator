"""Strict MCP arguments for session voice-mode setup."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

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


VOICE_SETUP_INPUT_MODELS = (GetVoiceSetupInput, ConfigureVoiceSetupInput)


__all__ = [
    "ConfigureVoiceSetupInput",
    "GetVoiceSetupInput",
    "VOICE_SETUP_INPUT_MODELS",
]
