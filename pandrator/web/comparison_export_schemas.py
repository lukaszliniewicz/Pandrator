"""Strict request contracts for selected speech-take comparison exports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComparisonExportClipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation_run_id: str = Field(min_length=1, max_length=36)
    segment_id: str = Field(min_length=1, max_length=36)
    audio_version_side: Literal["before", "after"] = "after"
    take_id: str | None = Field(default=None, min_length=1, max_length=36)


class ComparisonExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    clips: list[ComparisonExportClipRequest] = Field(min_length=1, max_length=100)
    gap_ms: int = Field(default=350, ge=0, le=10_000)
    output_format: Literal["wav", "mp3"] = "wav"


__all__ = ["ComparisonExportClipRequest", "ComparisonExportRequest"]
