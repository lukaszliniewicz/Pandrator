"""Strict MCP inputs for bounded subtitle audio evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
EvidenceRoute = Literal[
    "whisper", "moss", "azure_mai_transcribe_2", "audio_llm"
]


class RequestSubtitleEvidenceInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    source_artifact_id: str = Field(min_length=1, max_length=80)
    cue_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4_000)
    routes: list[EvidenceRoute] = Field(min_length=1, max_length=4)
    audio_model_ids: list[str] = Field(default_factory=list, max_length=3)
    padding_before_ms: int = Field(default=2_000, ge=0, le=15_000)
    padding_after_ms: int = Field(default=2_000, ge=0, le=15_000)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @field_validator("routes")
    @classmethod
    def validate_unique_routes(cls, value: list[EvidenceRoute]) -> list[EvidenceRoute]:
        if len(set(value)) != len(value):
            raise ValueError("Evidence routes must be unique.")
        return value

    @field_validator("audio_model_ids")
    @classmethod
    def validate_audio_model_ids(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value]
        if any(not 1 <= len(item) <= 80 for item in normalized):
            raise ValueError("Audio model IDs must be between 1 and 80 characters.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Audio model IDs must be unique.")
        return normalized

    @model_validator(mode="after")
    def validate_audio_route(self) -> "RequestSubtitleEvidenceInput":
        if ("audio_llm" in self.routes) != bool(self.audio_model_ids):
            raise ValueError(
                "audio_llm requires audio_model_ids, and audio_model_ids require "
                "the audio_llm route."
            )
        return self


class GetSubtitleEvidenceInput(ToolInput):
    evidence_id: str = Field(min_length=1, max_length=120)


class ResolveSubtitleEvidenceInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    evidence_id: str = Field(min_length=1, max_length=120)
    action: Literal["accepted", "edited", "deleted", "uncertain", "dismissed"]
    candidate_id: str | None = Field(default=None, min_length=1, max_length=120)
    text: str | None = Field(default=None, max_length=16_000)
    note: str = Field(default="", max_length=4_000)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @model_validator(mode="after")
    def validate_resolution(self) -> "ResolveSubtitleEvidenceInput":
        if self.action == "accepted" and not self.candidate_id:
            raise ValueError("accepted evidence requires candidate_id.")
        if self.action == "edited" and not str(self.text or "").strip():
            raise ValueError("edited evidence requires nonblank text.")
        if self.action == "uncertain" and not self.note.strip():
            raise ValueError("uncertain evidence requires a concrete note.")
        if self.action != "accepted" and self.candidate_id is not None:
            raise ValueError("candidate_id is only valid for accepted evidence.")
        if self.action != "edited" and self.text is not None:
            raise ValueError("text is only valid for edited evidence.")
        return self
