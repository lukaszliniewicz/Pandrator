"""Strict MCP arguments for passive whole-recording media-edit dispatch."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"


class CreateMediaEditDispatchRunInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    instructions: str = Field(min_length=1, max_length=16_000)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)

    @field_validator("instructions")
    @classmethod
    def strip_instructions(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Instructions must not be blank.")
        return normalized


class ListMediaEditDispatchRunsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=50, ge=1, le=100)


class GetMediaEditDispatchRunInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)


class ClaimMediaEditDispatchBatchInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)


class RenewMediaEditDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)


class ReleaseMediaEditDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)


class MediaEditDispatchCutInput(ToolInput):
    start_cue_id: str | None = Field(default=None, min_length=1, max_length=120)
    start_at_media_start: bool = False
    end_cue_id: str | None = Field(default=None, min_length=1, max_length=120)
    end_at_media_end: bool = False
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "MediaEditDispatchCutInput":
        if (self.start_cue_id is not None) == self.start_at_media_start:
            raise ValueError(
                "Exactly one of start_cue_id or start_at_media_start=true is required."
            )
        if (self.end_cue_id is not None) == self.end_at_media_end:
            raise ValueError("Exactly one of end_cue_id or end_at_media_end=true is required.")
        return self


class MediaEditDispatchResultInput(ToolInput):
    kind: Literal["media_edit"]
    cuts: list[MediaEditDispatchCutInput] = Field(max_length=1000)


class SubmitMediaEditDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    result: MediaEditDispatchResultInput
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=_SAFE_KEY)
