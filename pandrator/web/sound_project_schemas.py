"""Strict editing contracts for project sound cues."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SoundAnchor(_Strict):
    kind: Literal["before_segment", "after_segment", "source_time", "after_word", "next_pause"]
    segment_id: str | None = Field(default=None, min_length=1, max_length=36)
    time_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    quote: str | None = Field(default=None, min_length=1, max_length=500)
    occurrence: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> SoundAnchor:
        fields = self.model_fields_set
        if self.kind in {"before_segment", "after_segment"}:
            required, allowed = {"segment_id"}, {"kind", "segment_id"}
        elif self.kind in {"after_word", "next_pause"}:
            required, allowed = (
                {"segment_id", "quote", "occurrence"},
                {"kind", "segment_id", "quote", "occurrence", "start", "end"},
            )
        elif self.kind == "source_time":
            required, allowed = {"time_seconds"}, {"kind", "time_seconds"}
        else:
            raise ValueError("Unsupported sound anchor kind.")
        if not required <= fields or fields - allowed:
            raise ValueError(
                f"{self.kind} anchor requires {sorted(required)} and permits only {sorted(allowed)}."
            )
        if any(getattr(self, name) is None for name in required):
            raise ValueError(f"{self.kind} anchor has a null required value.")
        if self.quote is not None and not self.quote.strip():
            raise ValueError("quote must contain non-whitespace text.")
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be supplied together.")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must precede end.")
        return self


class SoundPlayback(_Strict):
    mode: Literal["insert", "overlay"] | None = None
    offset_ms: int = Field(default=0, ge=-10000, le=10000)
    gain_db: float = Field(default=0.0, ge=-60, le=12, allow_inf_nan=False)
    trim_start_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    trim_end_seconds: float | None = Field(default=None, allow_inf_nan=False)
    fade_in_ms: int = Field(default=5, ge=0, le=1000)
    fade_out_ms: int = Field(default=5, ge=0, le=1000)

    @model_validator(mode="after")
    def validate_trim(self) -> SoundPlayback:
        if self.trim_end_seconds is not None and self.trim_end_seconds <= self.trim_start_seconds:
            raise ValueError("trim_end_seconds must be greater than trim_start_seconds.")
        if self.mode == "insert" and self.offset_ms < 0:
            raise ValueError("insert offset_ms must be nonnegative.")
        return self


class SoundCueCreate(_Strict):
    expected_revision: int = Field(ge=0)
    variant_id: str = Field(min_length=1, max_length=36)
    anchor: SoundAnchor
    playback: SoundPlayback = Field(default_factory=SoundPlayback)
    included: bool = True


class SoundCueUpdate(_Strict):
    expected_revision: int = Field(ge=1)
    variant_id: str | None = Field(default=None, min_length=1, max_length=36)
    anchor: SoundAnchor | None = None
    playback: SoundPlayback | None = None
    included: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> SoundCueUpdate:
        changes = self.model_fields_set - {"expected_revision"}
        if not changes or any(getattr(self, field) is None for field in changes):
            raise ValueError("Provide at least one non-null cue change.")
        return self


class SoundPlanRestore(_Strict):
    expected_revision: int = Field(ge=0)
    source_plan_id: str = Field(min_length=1, max_length=36)
