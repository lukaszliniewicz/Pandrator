"""Portable MCP contracts for speech-take comparison export requests."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .common import ToolInput


class ComparisonExportClipInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation_run_id: str = Field(min_length=1, max_length=36)
    segment_id: str = Field(min_length=1, max_length=36)
    audio_version_side: Literal["before", "after"] = "after"
    take_id: str | None = Field(default=None, min_length=1, max_length=36)


class ComparisonExportInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(min_length=1, max_length=80)
    clips: list[ComparisonExportClipInput] | None = Field(
        default=None, min_length=1, max_length=100
    )
    generation_run_ids: list[str] | None = Field(
        default=None, min_length=2, max_length=2
    )
    segment_ids: list[str] | None = Field(
        default=None, min_length=1, max_length=50
    )
    gap_ms: int = Field(default=350, ge=0, le=10_000)
    output_format: Literal["wav", "mp3"] = "wav"
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )

    @model_validator(mode="after")
    def validate_clip_selection(self) -> "ComparisonExportInput":
        convenience_present = (
            self.generation_run_ids is not None or self.segment_ids is not None
        )
        if self.clips is not None:
            if convenience_present:
                raise ValueError(
                    "Supply canonical clips or convenience run and segment IDs, not both."
                )
            return self
        if self.generation_run_ids is None or self.segment_ids is None:
            raise ValueError(
                "Supply clips or both generation_run_ids and segment_ids."
            )
        if len(set(self.generation_run_ids)) != 2:
            raise ValueError("generation_run_ids must contain exactly two unique IDs.")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("segment_ids must contain unique IDs.")
        if any(not value or len(value) > 36 for value in self.generation_run_ids):
            raise ValueError("Each generation run ID must contain 1–36 characters.")
        if any(not value or len(value) > 36 for value in self.segment_ids):
            raise ValueError("Each segment ID must contain 1–36 characters.")
        return self


__all__ = ["ComparisonExportClipInput", "ComparisonExportInput"]
