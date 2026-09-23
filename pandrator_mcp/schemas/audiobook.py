"""Strict MCP arguments for audiobook setup and speech-plan preview."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

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


def _hide_signature_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


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


class SpeechSelectionDeliveryInput(ToolInput):
    instruction: str | None = Field(default=None, max_length=1200)
    emotion: str | None = Field(default=None, max_length=80)
    pace: Literal["", "natural", "slower", "brisk"] | None = None
    cadence: Literal["", "continuing", "concluding", "questioning", "contrast"] | None = None
    emphasis: Literal["", "light", "moderate", "strong"] | None = None


class PreviewSpeechSelectionInput(ToolInput):
    session_id: str = _SESSION_ID
    revision_id: str = _IDENTIFIER
    segment_id: str = _IDENTIFIER
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    speaker: Literal["unchanged", "narrator", "character"] = Field(
        default="unchanged", json_schema_extra=_hide_signature_default,
    )
    character_id: str | None = Field(
        default=None, min_length=1, max_length=128,
        json_schema_extra=_hide_signature_default,
    )
    voice: str | None = Field(
        default=None, max_length=2000,
        json_schema_extra=_hide_signature_default,
    )
    delivery: SpeechSelectionDeliveryInput | None = Field(
        default=None,
        json_schema_extra=_hide_signature_default,
    )
    unlock_locked: bool = Field(
        default=False, json_schema_extra=_hide_signature_default,
    )
    enable_casting: bool = Field(
        default=False, json_schema_extra=_hide_signature_default,
    )
    enable_performance: bool = Field(
        default=False, json_schema_extra=_hide_signature_default,
    )

    @model_validator(mode="after")
    def validate_selection(self):
        if self.end <= self.start:
            raise ValueError("The selected speech range must not be empty.")
        if self.speaker == "character" and self.character_id is None:
            raise ValueError("character_id is required for a character selection.")
        if self.speaker != "character" and self.character_id is not None:
            raise ValueError("character_id is only valid for a character selection.")
        return self


class ApplySpeechSelectionInput(PreviewSpeechSelectionInput):
    expected_preview_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    idempotency_key: str = _IDEMPOTENCY_KEY


AUDIOBOOK_INPUT_MODELS = (
    GetAudiobookSetupInput,
    ConfigureAudiobookInput,
    PreviewSpeechSegmentInput,
    PreviewSpeechSelectionInput,
    ApplySpeechSelectionInput,
)


__all__ = [
    "AUDIOBOOK_INPUT_MODELS",
    "ConfigureAudiobookInput",
    "GetAudiobookSetupInput",
    "PreviewSpeechSegmentInput",
    "SpeechSelectionDeliveryInput",
    "PreviewSpeechSelectionInput",
    "ApplySpeechSelectionInput",
]
