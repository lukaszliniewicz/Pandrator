"""Source-owned logical subtitle edits, independent of display cue numbers."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import ToolInput


class PreviewSubtitlePassagesInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    query: str | None = Field(default=None, max_length=500)


class SubtitlePassagePatch(ToolInput):
    id: str = Field(min_length=1, max_length=120)
    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    speaker: str | None = Field(default=None, max_length=160)
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, gt=0, strict=True)
    starts_new_turn: bool = Field(default=False, strict=True)
    review_state: Literal["clear", "uncertain"] | None = None
    review_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_edit(self) -> SubtitlePassagePatch:
        if not any(
            getattr(self, field) is not None
            for field in ("text", "speaker", "start_ms", "end_ms", "review_state", "review_note")
        ) and not self.starts_new_turn:
            raise ValueError("A passage patch must change text, timing, review or turn metadata.")
        return self


class PatchSubtitlePassagesInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    source_artifact_id: str = Field(min_length=1, max_length=80)
    expected_source_hash: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    expected_composition_hash: str = Field(min_length=1, max_length=128)
    passages: list[SubtitlePassagePatch] = Field(min_length=1, max_length=100)
    dry_run: bool = Field(default=False, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def unique_ids(self) -> PatchSubtitlePassagesInput:
        ids = [row.id for row in self.passages]
        if len(set(ids)) != len(ids):
            raise ValueError("Passage patches must contain unique source passage IDs.")
        return self
