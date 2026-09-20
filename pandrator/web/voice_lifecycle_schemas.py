"""Schemas for managed voice-reference lifecycle operations."""

from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)


class VoiceReferenceImportRequest(BaseModel):
    """Import an existing managed audio artifact as a voice reference."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: StrictStr = Field(min_length=1, max_length=160)
    transcript: StrictStr | None = Field(default=None, max_length=4000)
    transcript_reviewed: StrictBool = False
    language: StrictStr | None = Field(default=None, max_length=40)
    expected_voice_revision: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def _reviewed_transcript_is_required(self) -> VoiceReferenceImportRequest:
        if self.transcript_reviewed and not str(self.transcript or "").strip():
            raise ValueError(
                "A nonblank transcript is required when transcript_reviewed is true."
            )
        return self
