"""Strict request contracts for independently queued sound work."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SoundMixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    base_artifact_id: str = Field(min_length=1, max_length=36)
    timeline_artifact_id: str | None = Field(default=None, min_length=1, max_length=36)


class SoundResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    cue_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
