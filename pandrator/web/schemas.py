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
    source_language: str = Field(default="auto", min_length=2, max_length=40)
    target_language: str | None = Field(default=None, min_length=2, max_length=40)
    workflow_preset: str = "custom"
    included_stages: list[str] = Field(default_factory=list)
    overwrite_session_id: str | None = None


class SessionUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    workflow_kind: Literal["audiobook", "subtitles", "voiceover"] | None = None
    source_language: str | None = Field(default=None, min_length=2, max_length=40)
    target_language: str | None = Field(default=None, min_length=2, max_length=40)
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
    provider_key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    base_url: str | None = None
    secret_ref: str | None = None
    api_key: str | None = Field(default=None, max_length=65536)
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(StrictModel):
    provider_key: str | None = Field(default=None, min_length=1, max_length=80)
    label: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: bool | None = None
    base_url: str | None = None
    secret_ref: str | None = None
    api_key: str | None = Field(default=None, max_length=65536)
    clear_api_key: bool = False
    options: dict[str, Any] | None = None


class CredentialUpdate(StrictModel):
    api_key: str | None = Field(default=None, max_length=65536)
    clear: bool = False


class ProviderTestRequest(StrictModel):
    model_id: str | None = None


class ModelCreate(StrictModel):
    model_id: str
    is_active: bool = False
    is_default: bool = False
    default_temperature: float | None = None
    default_reasoning_effort: str | None = None
    input_cost_per_million: float | None = Field(default=None, ge=0)
    cached_input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    options: dict[str, Any] = Field(default_factory=dict)


class ModelUpdate(StrictModel):
    model_id: str | None = None
    is_active: bool | None = None
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


class RvcModelUploadRequest(StrictModel):
    pth_artifact_id: str
    index_artifact_id: str


class RvcConvertRequest(StrictModel):
    source_artifact_id: str
    session_id: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class TrainingCreateRequest(StrictModel):
    model_name: str = Field(min_length=1, max_length=255)
    source_artifact_id: str
    source_text_artifact_id: str | None = None
    voice_id: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class SettingUpdate(StrictModel):
    value: Any


class BundleExportRequest(StrictModel):
    include_sources: bool = True


class BundleImportRequest(StrictModel):
    source_artifact_id: str
    name: str | None = Field(default=None, min_length=1, max_length=255)


class SourceUrlRequest(StrictModel):
    url: str = Field(min_length=8, max_length=4096)


class SourceReuseRequest(StrictModel):
    artifact_id: str


class SessionSettingsUpdate(StrictModel):
    value: dict[str, Any] = Field(default_factory=dict)


class OutcomePlanUpdate(StrictModel):
    value: dict[str, Any]


class SourceAttachRequest(StrictModel):
    source_asset_id: str
    role: str = Field(default="primary", min_length=1, max_length=80)


class SourceUpdateRequest(StrictModel):
    display_name: str = Field(min_length=1, max_length=255)


class ChunkUploadInitialize(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: str | None = Field(default=None, max_length=160)
    session_id: str | None = None
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    chunk_size: int = Field(default=8 * 1024 * 1024, ge=1024 * 1024, le=16 * 1024 * 1024)


class GenerationSegmentCreate(StrictModel):
    text: str = Field(min_length=1)
    source_segment_ids: list[str] = Field(default_factory=list)
    node_kind: Literal["paragraph", "heading", "chapter_marker", "subtitle_cue"] = "paragraph"
    paragraph_break_after: bool = False
    voice_id: str | None = None
    language: str | None = Field(default=None, max_length=40)
    silence_after_ms: int = Field(default=0, ge=0)


class GenerationPlanCreate(StrictModel):
    source_revision_id: str | None = None
    segments: list[GenerationSegmentCreate] = Field(min_length=1)
    settings: dict[str, Any] = Field(default_factory=dict)


class GenerationSegmentUpdate(StrictModel):
    text: str | None = Field(default=None, min_length=1)
    optimized_text: str | None = None
    node_kind: Literal["paragraph", "heading", "chapter_marker", "subtitle_cue"] | None = None
    paragraph_break_after: bool | None = None
    voice_id: str | None = None
    language: str | None = Field(default=None, max_length=40)
    silence_after_ms: int | None = Field(default=None, ge=0)
    marked: bool | None = None
    removed: bool | None = None


class GenerationStartRequest(StrictModel):
    run_override: dict[str, Any] = Field(default_factory=dict)
    segment_ids: list[str] = Field(default_factory=list)
    operation: Literal["generate", "regenerate", "rvc"] = "generate"


class OptimizationReviewItem(StrictModel):
    index: int = Field(ge=0)
    text: str = Field(min_length=1)


class OptimizationReviewRequest(StrictModel):
    items: list[OptimizationReviewItem] = Field(min_length=1)


class OutputAssemblyCreateRequest(StrictModel):
    generation_run_id: str | None = None
    run_override: dict[str, Any] = Field(default_factory=dict)


class TtsEndpointDiscoveryRequest(StrictModel):
    base_url: str = Field(min_length=8, max_length=2048)
    service_id: str | None = Field(default=None, min_length=1, max_length=160)
    api_key: str | None = Field(default=None, max_length=65536)


class TtsVoicePreviewRequest(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    model: str = Field(default="", max_length=300)
    voice: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=40)


class AgentRunCreateRequest(StrictModel):
    source_artifact_id: str
    settings: dict[str, Any] = Field(default_factory=dict)


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
        ProviderUpdate,
        ProviderTestRequest,
        CredentialUpdate,
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
        TtsVoicePreviewRequest,
        RvcModelUploadRequest,
        RvcConvertRequest,
        TrainingCreateRequest,
        SettingUpdate,
        BundleExportRequest,
        BundleImportRequest,
        SourceUrlRequest,
        SourceReuseRequest,
        SessionSettingsUpdate,
        OutcomePlanUpdate,
        SourceAttachRequest,
        SourceUpdateRequest,
        ChunkUploadInitialize,
        GenerationSegmentCreate,
        GenerationPlanCreate,
        GenerationSegmentUpdate,
        GenerationStartRequest,
        OptimizationReviewItem,
        OptimizationReviewRequest,
        OutputAssemblyCreateRequest,
        TtsEndpointDiscoveryRequest,
        AgentRunCreateRequest,
    )
}
