"""Strict MCP arguments for audiobook setup and speech-plan preview."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import ToolInput

_SESSION_ID = Field(min_length=1, max_length=80)
_IDENTIFIER = Field(min_length=1, max_length=128)
_IDEMPOTENCY_KEY = Field(
    min_length=8,
    max_length=200,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
)


def _trim_nonblank(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("This value may not be blank.")
    return normalized


class GetAudiobookSetupInput(ToolInput):
    session_id: str = _SESSION_ID

    @field_validator("session_id", mode="before")
    @classmethod
    def trim_session_id(cls, value: Any) -> Any:
        return _trim_nonblank(value)


class ConfigureAudiobookInput(ToolInput):
    session_id: str = _SESSION_ID
    expected_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    mode: Literal["single_voice", "multi_voice"]
    idempotency_key: str = _IDEMPOTENCY_KEY

    @field_validator("session_id", mode="before")
    @classmethod
    def trim_session_id(cls, value: Any) -> Any:
        return _trim_nonblank(value)


class PreviewSpeechSegmentInput(ToolInput):
    session_id: str = _SESSION_ID
    revision_id: str = _IDENTIFIER
    segment_id: str = _IDENTIFIER
    generation_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_request: bool = False

    @field_validator(
        "session_id", "revision_id", "segment_id", "generation_run_id", mode="before"
    )
    @classmethod
    def trim_identifiers(cls, value: Any) -> Any:
        return _trim_nonblank(value) if value is not None else value


AUDIOBOOK_INPUT_MODELS = (
    GetAudiobookSetupInput,
    ConfigureAudiobookInput,
    PreviewSpeechSegmentInput,
)


__all__ = [
    "AUDIOBOOK_INPUT_MODELS",
    "ConfigureAudiobookInput",
    "GetAudiobookSetupInput",
    "PreviewSpeechSegmentInput",
]
