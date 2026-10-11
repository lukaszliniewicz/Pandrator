"""Strict MCP arguments for audiobook setup and speech-plan preview."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

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

    @field_validator("session_id", "revision_id", "segment_id", "generation_run_id", mode="before")
    @classmethod
    def trim_identifiers(cls, value: Any) -> Any:
        return _trim_nonblank(value) if value is not None else value


class SpeechSelectionDeliveryInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    instruction: str | None = Field(default=None, max_length=1200)
    emotion: str | None = Field(default=None, max_length=80)
    emotion_intensity: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    pace: Literal["", "natural", "slower", "brisk"] | None = None
    cadence: Literal["", "continuing", "concluding", "questioning", "contrast"] | None = None
    emphasis: Literal["", "light", "moderate", "strong"] | None = None


class SpeechSelectionVocalEventInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal[
        "pause",
        "laugh",
        "chuckle",
        "sigh",
        "inhale",
        "exhale",
        "cough",
        "gasp",
        "clear_throat",
    ]
    position: Literal["before", "after"] = "before"
    duration_ms: int | None = Field(default=None, ge=50, le=5000)

    @model_validator(mode="after")
    def duration_is_pause_only(self):
        if self.duration_ms is not None and self.kind != "pause":
            raise ValueError("Only a pause can request a duration.")
        return self


class SpeechSelectionContextInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = _SESSION_ID
    revision_id: str = _IDENTIFIER
    segment_id: str = _IDENTIFIER
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.end < self.start:
            raise ValueError("The selected speech range cannot be reversed.")
        return self


class PreviewSpeechSelectionInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = _SESSION_ID
    revision_id: str = _IDENTIFIER
    segment_id: str = _IDENTIFIER
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    speaker: Literal["unchanged", "narrator", "dialogue", "character"] = Field(
        default="unchanged",
        json_schema_extra=_hide_signature_default,
    )
    character_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        json_schema_extra=_hide_signature_default,
    )
    voice_category: Literal["male", "female", "androgynous", "unspecified"] | None = Field(
        default=None,
        json_schema_extra=_hide_signature_default,
    )
    voice: str | None = Field(
        default=None,
        max_length=2000,
        json_schema_extra=_hide_signature_default,
    )
    delivery: SpeechSelectionDeliveryInput | None = Field(
        default=None,
        json_schema_extra=_hide_signature_default,
    )
    speech_xml: str | None = Field(
        default=None,
        max_length=262144,
        json_schema_extra=_hide_signature_default,
    )
    expected_sound_plan_revision: StrictInt | None = Field(
        default=None,
        ge=0,
        json_schema_extra=_hide_signature_default,
    )
    vocal_event: SpeechSelectionVocalEventInput | None = Field(
        default=None,
        json_schema_extra=_hide_signature_default,
    )
    unlock_locked: bool = Field(
        default=False,
        json_schema_extra=_hide_signature_default,
    )
    enable_casting: bool = Field(
        default=False,
        json_schema_extra=_hide_signature_default,
    )
    enable_performance: bool = Field(
        default=False,
        json_schema_extra=_hide_signature_default,
    )
    enable_vocalizations: bool = Field(
        default=False,
        json_schema_extra=_hide_signature_default,
    )

    @model_validator(mode="after")
    def validate_selection(self):
        if self.end < self.start:
            raise ValueError("The selected speech range cannot be reversed.")
        if self.end == self.start:
            if self.vocal_event is None:
                raise ValueError("An empty speech selection requires a vocal_event.")
            if (
                self.speaker != "unchanged"
                or {
                    "voice",
                    "character_id",
                    "voice_category",
                    "delivery",
                    "speech_xml",
                    "expected_sound_plan_revision",
                }
                & self.model_fields_set
            ):
                raise ValueError(
                    "A cursor selection only supports a vocal_event without speech edits."
                )
        if "speech_xml" in self.model_fields_set and self.speech_xml is None:
            raise ValueError("speech_xml must be a string when provided.")
        if "expected_sound_plan_revision" in self.model_fields_set and self.speech_xml is None:
            raise ValueError("expected_sound_plan_revision is only valid with speech_xml.")
        if "vocal_event" in self.model_fields_set and self.vocal_event is None:
            raise ValueError("vocal_event must be an object when provided.")
        if self.speech_xml is not None and (
            self.speaker != "unchanged"
            or {"character_id", "voice_category", "voice", "delivery", "vocal_event"}
            & self.model_fields_set
        ):
            raise ValueError(
                "speech_xml cannot be combined with speaker, voice, or delivery edits."
            )
        if self.speaker == "character" and self.character_id is None:
            raise ValueError("character_id is required for a character selection.")
        if self.speaker != "character" and self.character_id is not None:
            raise ValueError("character_id is only valid for a character selection.")
        if self.speaker != "dialogue" and self.voice_category is not None:
            raise ValueError("voice_category is only valid for a dialogue selection.")
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
    SpeechSelectionContextInput,
    PreviewSpeechSelectionInput,
    ApplySpeechSelectionInput,
)


__all__ = [
    "AUDIOBOOK_INPUT_MODELS",
    "ConfigureAudiobookInput",
    "GetAudiobookSetupInput",
    "PreviewSpeechSegmentInput",
    "SpeechSelectionContextInput",
    "SpeechSelectionVocalEventInput",
    "SpeechSelectionDeliveryInput",
    "PreviewSpeechSelectionInput",
    "ApplySpeechSelectionInput",
]
