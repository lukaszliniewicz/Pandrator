"""Strict request contracts for direct speech-markup range edits."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pandrator.logic.speech_performance import StrictModel


class SpeechSelectionDelivery(StrictModel):
    """A partial delivery patch; omitted fields remain unchanged."""

    instruction: str | None = Field(default=None, max_length=1200)
    emotion: str | None = Field(default=None, max_length=80)
    emotion_intensity: float | None = Field(
        default=None, ge=0, le=2, allow_inf_nan=False
    )
    pace: Literal["", "natural", "slower", "brisk"] | None = None
    cadence: Literal["", "continuing", "concluding", "questioning", "contrast"] | None = None
    emphasis: Literal["", "light", "moderate", "strong"] | None = None


class SpeechSelectionVocalEvent(StrictModel):
    kind: Literal[
        "pause", "laugh", "chuckle", "sigh", "inhale", "exhale",
        "cough", "gasp", "clear_throat",
    ]
    position: Literal["before", "after"] = "before"
    duration_ms: int | None = Field(default=None, ge=50, le=5000)

    @model_validator(mode="after")
    def duration_is_pause_only(self):
        if self.duration_ms is not None and self.kind != "pause":
            raise ValueError("Only a pause can request a duration.")
        return self


class SpeechSelectionRequest(StrictModel):
    revision_id: str = Field(min_length=1, max_length=128)
    segment_id: str = Field(min_length=1, max_length=128)
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    speaker: Literal["unchanged", "narrator", "dialogue", "character"] = "unchanged"
    character_id: str | None = Field(default=None, min_length=1, max_length=128)
    voice_category: Literal["male", "female", "androgynous", "unspecified"] | None = None
    # ``None`` is a meaningful explicit value (clear the voice).  Callers use
    # model_fields_set to distinguish it from an omitted voice field.
    voice: str | None = Field(default=None, max_length=2000)
    delivery: SpeechSelectionDelivery | None = None
    speech_xml: str | None = Field(default=None, max_length=262144)
    expected_sound_plan_revision: int | None = Field(default=None, ge=0)
    vocal_event: SpeechSelectionVocalEvent | None = None
    unlock_locked: bool = False
    enable_casting: bool = False
    enable_performance: bool = False
    enable_vocalizations: bool = False

    @model_validator(mode="after")
    def validate_selection(self):
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
            or {"character_id", "voice_category", "voice", "delivery", "vocal_event"} & self.model_fields_set
        ):
            raise ValueError(
                "speech_xml cannot be combined with speaker, voice, or delivery edits."
            )
        if self.speaker == "character" and self.character_id is None:
            raise ValueError("character_id is required for a character selection.")
        if self.speaker != "character" and self.character_id is not None:
            raise ValueError(
                "character_id is only valid for a character selection."
            )
        if self.speaker != "dialogue" and self.voice_category is not None:
            raise ValueError("voice_category is only valid for a dialogue selection.")
        return self


class SpeechSelectionContextRequest(StrictModel):
    revision_id: str = Field(min_length=1, max_length=128)
    segment_id: str = Field(min_length=1, max_length=128)
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.end < self.start:
            raise ValueError("The selected speech range cannot be reversed.")
        return self


class SpeechSelectionApplyRequest(SpeechSelectionRequest):
    expected_preview_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )


SPEECH_SELECTION_SCHEMAS = {
    SpeechSelectionDelivery.__name__: SpeechSelectionDelivery,
    SpeechSelectionVocalEvent.__name__: SpeechSelectionVocalEvent,
    SpeechSelectionRequest.__name__: SpeechSelectionRequest,
    SpeechSelectionContextRequest.__name__: SpeechSelectionContextRequest,
    SpeechSelectionApplyRequest.__name__: SpeechSelectionApplyRequest,
}


__all__ = [
    "SPEECH_SELECTION_SCHEMAS",
    "SpeechSelectionApplyRequest",
    "SpeechSelectionDelivery",
    "SpeechSelectionContextRequest",
    "SpeechSelectionRequest",
    "SpeechSelectionVocalEvent",
]
