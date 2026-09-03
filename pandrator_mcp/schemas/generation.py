"""Input schemas for fine-grained generation segments, takes, and assembly."""

from __future__ import annotations

from pydantic import Field

from .common import ToolInput


class ListGenerationSegmentsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    generation_run_id: str | None = Field(default=None, max_length=80)


class UpdateGenerationSegmentInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    segment_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    optimized_text: str | None = Field(default=None, max_length=2000)
    voice_id: str | None = Field(default=None, max_length=100)
    voice: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=120)


class SelectTakeInput(ToolInput):
    segment_id: str = Field(min_length=1, max_length=80)
    take_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=120)


class RegenerateSegmentsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    segment_ids: list[str] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=120)


class AssembleGenerationRunInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    generation_run_id: str | None = Field(default=None, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=120)
