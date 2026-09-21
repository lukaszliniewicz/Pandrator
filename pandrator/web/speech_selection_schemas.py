"""Strict request contracts for direct speech-markup range edits."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pandrator.logic.speech_performance import StrictModel


class SpeechSelectionDelivery(StrictModel):
    """A partial delivery patch; omitted fields remain unchanged."""

    instruction: str | None = Field(default=None, max_length=1200)
    emotion: str | None = Field(default=None, max_length=80)
    pace: Literal["", "natural", "slower", "brisk"] | None = None
    cadence: Literal["", "continuing", "concluding", "questioning", "contrast"] | None = None
    emphasis: Literal["", "light", "moderate", "strong"] | None = None


class SpeechSelectionRequest(StrictModel):
    revision_id: str = Field(min_length=1, max_length=128)
    segment_id: str = Field(min_length=1, max_length=128)
    expected_segment_revision: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    speaker: Literal["unchanged", "narrator", "character"] = "unchanged"
    character_id: str | None = Field(default=None, min_length=1, max_length=128)
    # ``None`` is a meaningful explicit value (clear the voice).  Callers use
    # model_fields_set to distinguish it from an omitted voice field.
    voice: str | None = Field(default=None, max_length=2000)
    delivery: SpeechSelectionDelivery | None = None
    unlock_locked: bool = False
    enable_casting: bool = False
    enable_performance: bool = False

    @model_validator(mode="after")
    def validate_selection(self):
        if self.end <= self.start:
            raise ValueError("The selected speech range must not be empty.")
        if self.speaker == "character" and self.character_id is None:
            raise ValueError("character_id is required for a character selection.")
        if self.speaker != "character" and self.character_id is not None:
            raise ValueError(
                "character_id is only valid for a character selection."
            )
        return self


class SpeechSelectionApplyRequest(SpeechSelectionRequest):
    expected_preview_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )


SPEECH_SELECTION_SCHEMAS = {
    SpeechSelectionDelivery.__name__: SpeechSelectionDelivery,
    SpeechSelectionRequest.__name__: SpeechSelectionRequest,
    SpeechSelectionApplyRequest.__name__: SpeechSelectionApplyRequest,
}


__all__ = [
    "SPEECH_SELECTION_SCHEMAS",
    "SpeechSelectionApplyRequest",
    "SpeechSelectionDelivery",
    "SpeechSelectionRequest",
]
