"""Pydantic API schemas shared with OpenAPI generation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(StrictModel):
    code: str
    message: str
    details: Any = None
    request_id: str


class SessionCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    workflow_kind: Literal["audiobook", "subtitles", "voiceover"] = "audiobook"
    workflow_preset: str = "custom"
    included_stages: list[str] = Field(default_factory=list)


class SessionUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    workflow_kind: Literal["audiobook", "subtitles", "voiceover"] | None = None
    workflow_preset: str | None = None
    included_stages: list[str] | None = None
    status: str | None = None


class JobCreate(StrictModel):
    kind: str = Field(min_length=1, max_length=120)
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=1, ge=1, le=10)


class LoginRequest(StrictModel):
    password: str


class BootstrapRequest(StrictModel):
    token: str


class TokenCreateRequest(StrictModel):
    label: str = Field(default="CLI token", max_length=160)


class ProviderCreate(StrictModel):
    kind: str = "llm"
    provider_key: str
    label: str
    base_url: str | None = None
    secret_ref: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ModelCreate(StrictModel):
    model_id: str
    is_default: bool = False
    default_temperature: float | None = None
    default_reasoning_effort: str | None = None
    input_cost_per_million: float | None = Field(default=None, ge=0)
    cached_input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    options: dict[str, Any] = Field(default_factory=dict)


class ModelUpdate(StrictModel):
    model_id: str | None = None
    is_default: bool | None = None
    default_temperature: float | None = None
    default_reasoning_effort: str | None = None
    input_cost_per_million: float | None = Field(default=None, ge=0)
    cached_input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    options: dict[str, Any] | None = None


class PdfRectInput(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float


class PdfCropInput(StrictModel):
    original_page: int = Field(ge=0)
    rect: PdfRectInput


class PdfWhiteoutInput(PdfCropInput):
    color: list[float] = Field(default_factory=lambda: [1.0, 1.0, 1.0], min_length=3, max_length=3)


class PdfEditRequest(StrictModel):
    source_artifact_id: str
    first_page_side: Literal["left", "right"] = "right"
    crops: list[PdfCropInput] = Field(default_factory=list)
    whiteouts: list[PdfWhiteoutInput] = Field(default_factory=list)
    deleted_pages: list[int] = Field(default_factory=list)


class SubtitleSegmentInput(StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1)
    speaker: str | None = None


class SubtitleReviewRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    segments: list[SubtitleSegmentInput] = Field(min_length=1)


class VoiceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    description: str | None = None


class VoiceTranscriptReview(StrictModel):
    transcript: str = Field(min_length=1)
    language: str | None = Field(default=None, max_length=40)


class SettingUpdate(StrictModel):
    value: Any


class BundleExportRequest(StrictModel):
    include_sources: bool = True


class BundleImportRequest(StrictModel):
    source_artifact_id: str
    name: str | None = Field(default=None, min_length=1, max_length=255)


SCHEMA_MODELS = {
    model.__name__: model
    for model in (
        ErrorBody,
        SessionCreate,
        SessionUpdate,
        JobCreate,
        LoginRequest,
        BootstrapRequest,
        TokenCreateRequest,
        ProviderCreate,
        ModelCreate,
        ModelUpdate,
        PdfRectInput,
        PdfCropInput,
        PdfWhiteoutInput,
        PdfEditRequest,
        SubtitleSegmentInput,
        SubtitleReviewRequest,
        VoiceCreate,
        VoiceTranscriptReview,
        SettingUpdate,
        BundleExportRequest,
        BundleImportRequest,
    )
}
