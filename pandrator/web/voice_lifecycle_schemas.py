"""Schemas for managed voice-reference lifecycle operations."""

from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from .schemas import VoiceDesignedSampleCreate


class VoiceDesignedSamplePreparationRequest(VoiceDesignedSampleCreate):
    """Carry the complete recipe identity through preparation idempotency."""

    recipe_signature: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class VoiceReferenceImportRequest(BaseModel):
    """Import an existing managed audio artifact as a voice reference."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: StrictStr = Field(min_length=1, max_length=160)
    transcript: StrictStr | None = Field(default=None, max_length=4000)
    transcript_reviewed: StrictBool = False
    language: StrictStr | None = Field(default=None, max_length=40)
    expected_voice_revision: StrictInt = Field(ge=1)
    start_seconds: StrictFloat = Field(default=0.0, ge=0, allow_inf_nan=False)
    end_seconds: StrictFloat | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def _reviewed_transcript_is_required(self) -> VoiceReferenceImportRequest:
        if self.transcript_reviewed and not str(self.transcript or "").strip():
            raise ValueError(
                "A nonblank transcript is required when transcript_reviewed is true."
            )
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds.")
        return self


class VoicePublishRequest(BaseModel):
    """Optionally bind publication to one exact managed normalized sample."""

    model_config = ConfigDict(extra="forbid")

    sample_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    sample_sha256: StrictStr | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    recipe_signature: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _pins_are_paired(self) -> VoicePublishRequest:
        if (self.sample_id is None) != (self.sample_sha256 is None):
            raise ValueError("sample_id and sample_sha256 must be provided together.")
        return self
