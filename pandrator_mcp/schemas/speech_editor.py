"""Portable strict inputs for batched speech-plan selection editing."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from .common import ToolInput


def _hide_signature_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


class SpeechSelectionDeliveryInput(ToolInput):
    """Partial delivery fields; only supplied nested keys are sent to the API."""

    model_config = ConfigDict(extra="forbid", strict=True)

    instruction: str | None = Field(default=None, max_length=1200, json_schema_extra=_hide_signature_default)
    emotion: str | None = Field(default=None, max_length=80, json_schema_extra=_hide_signature_default)
    emotion_intensity: float | None = Field(
        default=None, ge=0, le=2, allow_inf_nan=False,
        json_schema_extra=_hide_signature_default,
    )
    pace: Literal["", "natural", "slower", "brisk"] | None = Field(
        default=None, json_schema_extra=_hide_signature_default
    )
    cadence: Literal["", "continuing", "concluding", "questioning", "contrast"] | None = Field(
        default=None, json_schema_extra=_hide_signature_default
    )
    emphasis: Literal["", "light", "moderate", "strong"] | None = Field(
        default=None, json_schema_extra=_hide_signature_default
    )


class SpeechSelectionVocalEventInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal[
        "pause", "laugh", "chuckle", "sigh", "inhale", "exhale",
        "cough", "gasp", "clear_throat",
    ]
    position: Literal["before", "after"] = Field(
        default="before", json_schema_extra=_hide_signature_default
    )
    duration_ms: int | None = Field(
        default=None, ge=50, le=5000, json_schema_extra=_hide_signature_default
    )

    @model_validator(mode="after")
    def duration_is_pause_only(self) -> "SpeechSelectionVocalEventInput":
        if self.duration_ms is not None and self.kind != "pause":
            raise ValueError("Only a pause can request a duration.")
        return self


class SpeechSelectionChangeInput(ToolInput):
    """One original-codepoint selection and its optional speech edits."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision_id: str = Field(min_length=1, max_length=128)
    segment_id: str = Field(min_length=1, max_length=128)
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    speaker: Literal["unchanged", "narrator", "dialogue", "character"] = Field(
        default="unchanged", json_schema_extra=_hide_signature_default
    )
    character_id: str | None = Field(
        default=None, min_length=1, max_length=128,
        json_schema_extra=_hide_signature_default,
    )
    voice_category: Literal["male", "female", "androgynous", "unspecified"] | None = Field(
        default=None, json_schema_extra=_hide_signature_default
    )
    # Explicit None clears the voice; omission leaves it unchanged.
    voice: str | None = Field(
        default=None, max_length=2000, json_schema_extra=_hide_signature_default
    )
    delivery: SpeechSelectionDeliveryInput | None = Field(
        default=None, json_schema_extra=_hide_signature_default
    )
    speech_xml: str | None = Field(
        default=None, max_length=262144, json_schema_extra=_hide_signature_default
    )
    expected_sound_plan_revision: int | None = Field(
        default=None, ge=0, json_schema_extra=_hide_signature_default
    )
    vocal_event: SpeechSelectionVocalEventInput | None = Field(
        default=None, json_schema_extra=_hide_signature_default
    )
    unlock_locked: bool = Field(default=False, json_schema_extra=_hide_signature_default)
    enable_casting: bool = Field(default=False, json_schema_extra=_hide_signature_default)
    enable_performance: bool = Field(default=False, json_schema_extra=_hide_signature_default)
    enable_vocalizations: bool = Field(default=False, json_schema_extra=_hide_signature_default)

    @model_validator(mode="after")
    def validate_selection(self) -> "SpeechSelectionChangeInput":
        if self.end < self.start:
            raise ValueError("The selected speech range cannot be reversed.")
        if self.end == self.start:
            if self.vocal_event is None:
                raise ValueError("An empty speech selection requires a vocal_event.")
            if self.speaker != "unchanged" or {
                "voice", "character_id", "voice_category", "delivery", "speech_xml",
            } & self.model_fields_set:
                raise ValueError(
                    "A cursor selection only supports a vocal_event without speech edits."
                )
        if "speech_xml" in self.model_fields_set and self.speech_xml is None:
            raise ValueError("speech_xml must be a string when provided.")
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


_IDEMPOTENCY_KEY = Field(
    min_length=8,
    max_length=200,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
)


class PreviewSpeechSelectionBatchInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(min_length=1, max_length=80)
    changes: list[SpeechSelectionChangeInput] = Field(min_length=1, max_length=100)
    detailed_preview: bool = False

    @model_validator(mode="after")
    def one_revision(self) -> "PreviewSpeechSelectionBatchInput":
        if len({item.revision_id for item in self.changes}) != 1:
            raise ValueError("All speech selections must use one plan revision.")
        return self


class ApplySpeechSelectionBatchInput(PreviewSpeechSelectionBatchInput):
    expected_preview_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    idempotency_key: str = _IDEMPOTENCY_KEY


__all__ = [
    "ApplySpeechSelectionBatchInput",
    "PreviewSpeechSelectionBatchInput",
    "SpeechSelectionChangeInput",
    "SpeechSelectionDeliveryInput",
    "SpeechSelectionVocalEventInput",
]
