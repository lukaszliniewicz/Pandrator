"""Pydantic API schemas shared with OpenAPI generation."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CredentialBackend = Literal["database", "environment", "keyring", "file"]
ApiScope = Literal[
    "app.read",
    "app.write",
    "app.run",
    "app.cancel",
    "app.credentials.read",
    "app.credentials.write",
    "manager.read",
    "manager.runtime",
    "manager.mutate",
    "app.admin",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _default_manager_scopes() -> list[ApiScope]:
    return [
        "app.read",
        "app.write",
        "app.run",
        "app.cancel",
        "manager.read",
        "manager.runtime",
        "manager.mutate",
    ]


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


class SessionForkRequest(StrictModel):
    checkpoint_artifact_id: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=255)


class JobCreate(StrictModel):
    kind: str = Field(min_length=1, max_length=120)
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=1, ge=1, le=10)


class LoginRequest(StrictModel):
    password: str


class BootstrapRequest(StrictModel):
    token: str


class ManagerBootstrapRequest(StrictModel):
    scopes: list[ApiScope] = Field(
        default_factory=_default_manager_scopes,
        min_length=1,
    )


class TokenCreateRequest(StrictModel):
    label: str = Field(default="CLI token", max_length=160)
    scopes: list[ApiScope] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class AutomationClientCreateRequest(StrictModel):
    client_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    name: str = Field(min_length=1, max_length=160)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    scopes: list[ApiScope] = Field(min_length=1)


class WorkflowPlanCreateRequest(StrictModel):
    target_stage: str = Field(
        default="generate_audio",
        pattern=r"^[a-z][a-z0-9_]{0,79}$",
    )
    overrides: dict[str, Any] = Field(default_factory=dict)
    expires_in_minutes: int = Field(default=30, ge=1, le=60)


class WorkflowPlanExecuteRequest(StrictModel):
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_confirmations: list[str] = Field(default_factory=list)


class ProviderCreate(StrictModel):
    kind: str = "llm"
    provider_key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    base_url: str | None = None
    secret_ref: str | None = None
    api_key: str | None = Field(default=None, max_length=65536)
    credential_backend: CredentialBackend | None = None
    credential_reference: str | None = Field(default=None, max_length=4096)
    delete_previous_credential: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(StrictModel):
    provider_key: str | None = Field(default=None, min_length=1, max_length=80)
    label: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: bool | None = None
    base_url: str | None = None
    secret_ref: str | None = None
    api_key: str | None = Field(default=None, max_length=65536)
    clear_api_key: bool = False
    credential_backend: CredentialBackend | None = None
    credential_reference: str | None = Field(default=None, max_length=4096)
    delete_previous_credential: bool = False
    options: dict[str, Any] | None = None


class CredentialUpdate(StrictModel):
    api_key: str | None = Field(default=None, max_length=65536)
    clear: bool = False
    credential_backend: CredentialBackend | None = None
    credential_reference: str | None = Field(default=None, max_length=4096)
    delete_previous_credential: bool = False


class PronunciationCreate(StrictModel):
    source_form: str = Field(min_length=1, max_length=512)
    phonetic: str = Field(min_length=1, max_length=1024)
    language: str = Field(default="und", min_length=2, max_length=40)
    backend: str = Field(default="*", min_length=1, max_length=80)
    scope: Literal["global", "session"] = "global"
    session_id: str | None = None
    status: Literal["proposed", "reviewed", "disabled"] = "reviewed"
    alphabet: Literal["respelling"] = "respelling"
    notes: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PronunciationUpdate(StrictModel):
    source_form: str | None = Field(default=None, min_length=1, max_length=512)
    phonetic: str | None = Field(default=None, min_length=1, max_length=1024)
    language: str | None = Field(default=None, min_length=2, max_length=40)
    backend: str | None = Field(default=None, min_length=1, max_length=80)
    scope: Literal["global", "session"] | None = None
    session_id: str | None = None
    status: Literal["proposed", "reviewed", "disabled"] | None = None
    alphabet: Literal["respelling"] | None = None
    notes: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] | None = None


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
    context_window_tokens: int = Field(default=262_144, ge=4096)
    max_output_tokens: int | None = Field(default=None, ge=1)
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
    context_window_tokens: int | None = Field(default=None, ge=4096)
    max_output_tokens: int | None = Field(default=None, ge=1)
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
    color: list[float] = Field(
        default_factory=lambda: [1.0, 1.0, 1.0], min_length=3, max_length=3
    )


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
    source_artifact_id: str | None = None
    expected_revision: int = Field(ge=1)
    segments: list[SubtitleSegmentInput] = Field(min_length=1)


class VoiceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    description: str | None = None


class VoiceUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    description: str | None = None


class VoiceTranscriptReview(StrictModel):
    transcript: str = Field(min_length=1)
    language: str | None = Field(default=None, max_length=40)
    expected_voice_revision: int | None = Field(default=None, ge=1)


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


class StageSelectionUpdate(StrictModel):
    artifact_id: str | None = None


class ChunkUploadInitialize(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: str | None = Field(default=None, max_length=160)
    session_id: str | None = None
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    chunk_size: int = Field(
        default=8 * 1024 * 1024, ge=1024 * 1024, le=16 * 1024 * 1024
    )


class GenerationSegmentCreate(StrictModel):
    text: str = Field(min_length=1)
    source_segment_ids: list[str] = Field(default_factory=list)
    alignment_group: str | None = Field(default=None, max_length=64)
    node_kind: Literal["paragraph", "heading", "chapter_marker", "subtitle_cue"] = (
        "paragraph"
    )
    paragraph_break_after: bool = False
    speaker: str | None = Field(default=None, max_length=160)
    voice_id: str | None = None
    voice: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    silence_after_ms: int = Field(default=0, ge=0)


class GenerationPlanCreate(StrictModel):
    source_revision_id: str | None = None
    segments: list[GenerationSegmentCreate] = Field(min_length=1)
    settings: dict[str, Any] = Field(default_factory=dict)


class GenerationSegmentUpdate(StrictModel):
    text: str | None = Field(default=None, min_length=1)
    optimized_text: str | None = None
    node_kind: (
        Literal["paragraph", "heading", "chapter_marker", "subtitle_cue"] | None
    ) = None
    paragraph_break_after: bool | None = None
    voice_id: str | None = None
    voice: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=40)
    silence_after_ms: int | None = Field(default=None, ge=0)
    marked: bool | None = None
    removed: bool | None = None


class GenerationSegmentBatchUpdateItem(StrictModel):
    id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    changes: GenerationSegmentUpdate


class GenerationSegmentBatchUpdate(StrictModel):
    updates: list[GenerationSegmentBatchUpdateItem] = Field(
        min_length=1,
        max_length=20_000,
    )


class GenerationStartRequest(StrictModel):
    run_override: dict[str, Any] = Field(default_factory=dict)
    # This intentionally remains distinct from a normal run override.  It is
    # applied only to the requested segment set, after any saved per-segment
    # voice/language choices, and is retained in the immutable run snapshot.
    selected_segment_override: dict[str, Any] = Field(default_factory=dict)
    segment_ids: list[str] = Field(default_factory=list)
    generation_run_id: str | None = None
    operation: Literal["generate", "regenerate", "rvc"] = "generate"


class OptimizationReviewItem(StrictModel):
    index: int = Field(ge=0)
    text: str = Field(min_length=1)


class OptimizationReviewRequest(StrictModel):
    items: list[OptimizationReviewItem] = Field(min_length=1)


class OutputAssemblyCreateRequest(StrictModel):
    generation_run_id: str | None = None
    run_override: dict[str, Any] = Field(default_factory=dict)


class OutputMixPreviewRequest(StrictModel):
    generation_run_id: str = Field(min_length=1, max_length=160)
    start_seconds: float | None = Field(default=None, ge=0.0, le=604800.0)
    duration_seconds: float = Field(default=12.0, ge=4.0, le=30.0)
    mix_source_gain_db: float = Field(default=0.0, ge=-60.0, le=12.0)
    mix_voice_gain_db: float = Field(default=0.0, ge=-30.0, le=12.0)
    mix_voice_lufs: float = Field(default=-16.0, ge=-30.0, le=-8.0)
    mix_ducking: Literal[
        "off",
        "gentle",
        "balanced",
        "strong",
        "very_strong",
    ] = "strong"
    mix_attack_ms: int = Field(default=25, ge=1, le=2000)
    mix_release_ms: int = Field(default=350, ge=10, le=5000)


class TtsEndpointDiscoveryRequest(StrictModel):
    base_url: str = Field(min_length=8, max_length=2048)
    service_id: str | None = Field(default=None, min_length=1, max_length=160)
    api_key: str | None = Field(default=None, max_length=65536)


class TtsVoicePreviewRequest(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    model: str = Field(default="", max_length=300)
    voice: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=40)


class ManagerDesiredComponentState(StrictModel):
    present: bool = True
    compute: Literal[
        "auto",
        "cpu",
        "cuda",
        "vulkan",
        "metal",
        "rocm",
        "wgpu",
    ] = "auto"
    quantization: str | None = Field(default=None, max_length=120)
    options: dict[str, Any] = Field(default_factory=dict)


class ManagerPlanRequest(StrictModel):
    kind: Literal[
        "install",
        "update",
        "repair",
        "remove",
        "uninstall",
        "start",
        "stop",
        "restart",
        "import",
    ]
    desired: dict[str, ManagerDesiredComponentState]
    expected_revision: int | None = Field(default=None, ge=0)


class ManagerReleasePlanRequest(StrictModel):
    """Signed product release envelope forwarded without adding trust inputs."""

    manifest: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=0)
    offline: bool = False
    start_after_activation: bool = True


class ManagerUninstallPlanRequest(StrictModel):
    expected_revision: int | None = Field(default=None, ge=0)
    purge_data: bool = False
    export_data: str | None = Field(default=None, min_length=1, max_length=4096)


class ManagerLegacyImportRequest(StrictModel):
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool = True


class ManagerOperationRequest(StrictModel):
    plan_id: str = Field(min_length=1, max_length=160)
    plan_digest: str = Field(min_length=32, max_length=256)
    accepted_confirmations: list[str] = Field(default_factory=list)


class ManagerRuntimeRequest(StrictModel):
    service_ids: list[str] = Field(default_factory=list)


class AgentRunCreateRequest(StrictModel):
    source_artifact_id: str
    settings: dict[str, Any] = Field(default_factory=dict)


class DispatchRunCreateRequest(StrictModel):
    kind: Literal["correction", "translation"]
    source_artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_language: str | None = Field(default=None, max_length=40)
    target_language: str | None = Field(default=None, max_length=40)
    instructions: str = Field(default="", max_length=16_000)
    char_limit: int = Field(default=6000, ge=1, le=100_000)
    max_segments_per_batch: int = Field(default=40, ge=1, le=500)
    no_remove_subtitles: bool = False
    context_before: int = Field(default=8, ge=0, le=20)
    context_after: int = Field(default=2, ge=0, le=20)
    timing_context_mode: Literal["full", "overlap_only", "none"] = "full"
    include_timing_context: bool | None = Field(
        default=None,
        exclude=True,
        deprecated=True,
        description=(
            "Deprecated compatibility input. False maps to timing_context_mode=none; "
            "true maps to full."
        ),
    )
    substantial_gap_ms: int = Field(default=2000, ge=0, le=60_000)
    glossary: dict[str, str] = Field(default_factory=dict, max_length=2_000)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_timing_context(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "include_timing_context" not in value:
            return value
        legacy = value.get("include_timing_context")
        if legacy is None:
            return value
        migrated = dict(value)
        legacy_enabled = (
            legacy.strip().lower() not in {"0", "false", "no", "off"}
            if isinstance(legacy, str)
            else bool(legacy)
        )
        requested = "full" if legacy_enabled else "none"
        canonical = migrated.get("timing_context_mode")
        if canonical is not None and canonical != requested:
            raise ValueError(
                "include_timing_context conflicts with timing_context_mode."
            )
        migrated["timing_context_mode"] = requested
        return migrated

    @field_validator("glossary")
    @classmethod
    def validate_dispatch_glossary(cls, value: dict[str, str]) -> dict[str, str]:
        for term, replacement in value.items():
            if not term.strip() or len(term) > 500:
                raise ValueError("Glossary terms must be 1-500 characters.")
            if not replacement.strip() or len(replacement) > 2_000:
                raise ValueError("Glossary replacements must be 1-2,000 characters.")
        return value


class DispatchBatchClaimRequest(StrictModel):
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class DispatchBatchRenewRequest(StrictModel):
    lease_token: str = Field(min_length=1, max_length=160)
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class DispatchBatchReleaseRequest(StrictModel):
    lease_token: str = Field(min_length=1, max_length=160)


class DispatchCorrectionOperation(StrictModel):
    action: Literal["edit", "delete", "merge", "split"]
    cue_ids: list[Annotated[int, Field(ge=1)]] = Field(
        min_length=1,
        max_length=500,
    )
    texts: list[Annotated[str, Field(min_length=1, max_length=16_000)]] = Field(
        default_factory=list,
        max_length=500,
    )
    speakers: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_operation_shape(self) -> DispatchCorrectionOperation:
        ids = self.cue_ids
        texts = self.texts
        valid = (
            (self.action == "edit" and len(ids) == 1 and len(texts) == 1)
            or (self.action == "delete" and not texts)
            or (self.action == "merge" and len(ids) >= 2 and bool(texts))
            or (self.action == "split" and len(ids) == 1 and len(texts) >= 2)
        )
        if not valid:
            raise ValueError("Correction operation fields do not match its action.")
        if len(set(ids)) != len(ids):
            raise ValueError("Correction operation cue_ids must be unique.")
        if self.speakers and len(self.speakers) != len(texts):
            raise ValueError("speakers must be empty or match texts one-for-one.")
        return self


class DispatchCorrectionResult(StrictModel):
    kind: Literal["correction"]
    operations: list[DispatchCorrectionOperation] = Field(
        default_factory=list,
        max_length=500,
    )


class DispatchTranslationItem(StrictModel):
    cue_id: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=16_000)
    speaker: str | None = Field(default=None, max_length=500)


class DispatchTranslationResult(StrictModel):
    kind: Literal["translation"]
    translations: list[DispatchTranslationItem] = Field(
        min_length=1,
        max_length=500,
    )
    glossary_updates: dict[str, str] = Field(default_factory=dict, max_length=2_000)

    @field_validator("glossary_updates")
    @classmethod
    def validate_glossary_updates(cls, value: dict[str, str]) -> dict[str, str]:
        for term, replacement in value.items():
            if not term.strip() or len(term) > 500:
                raise ValueError("Glossary terms must be 1-500 characters.")
            if not replacement.strip() or len(replacement) > 2_000:
                raise ValueError("Glossary replacements must be 1-2,000 characters.")
        return value


DispatchStructuredResult = Annotated[
    DispatchCorrectionResult | DispatchTranslationResult,
    Field(discriminator="kind"),
]


class DispatchBatchSubmitRequest(StrictModel):
    lease_token: str = Field(min_length=1, max_length=160)
    result: DispatchStructuredResult | None = None
    response_text: str | None = Field(default=None, max_length=512 * 1024)

    @model_validator(mode="after")
    def validate_exactly_one_result(self):
        if (self.result is None) == (self.response_text is None):
            raise ValueError("Provide exactly one of result or response_text.")
        return self

    @field_validator("response_text")
    @classmethod
    def validate_dispatch_response_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 512 * 1024:
            raise ValueError("Model response exceeds the 512 KiB limit.")
        return value


class DispatchCueTiming(StrictModel):
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    gap_from_previous_ms: int | None = Field(default=None, ge=0)
    overlap_with_previous_ms: int | None = Field(default=None, ge=0)


class DispatchCue(StrictModel):
    cue_id: int = Field(ge=1)
    text: str
    speaker: str | None = None
    timing: DispatchCueTiming | None = None


class DispatchBoundaryCue(StrictModel):
    text: str
    speaker: str | None = None


class DispatchBoundaryContext(StrictModel):
    previous_output: list[DispatchBoundaryCue]
    following_source: list[DispatchBoundaryCue]


class DispatchClaimedBatch(StrictModel):
    id_namespace: Literal["source_revision_cue"]
    source_revision_id: str
    cue_count: int = Field(ge=1, le=500)
    valid_cue_ids: list[int]
    cues: list[DispatchCue]
    context: DispatchBoundaryContext


class DispatchTaskContract(StrictModel):
    kind: Literal["correction", "translation"]
    output_role: Literal["correction", "translation"]
    source_language: str
    target_language: str | None = None
    instructions: str
    result_contract: dict[str, Any]
    no_remove_subtitles: bool
    known_speakers: list[str]
    glossary: dict[str, str]
    timing_context_mode: Literal["full", "overlap_only", "none"]
    substantial_gap_ms: int | None = Field(default=None, ge=0, le=60_000)


class DispatchBatchClaimResponse(StrictModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    batch_id: str
    batch_ordinal: int = Field(ge=1)
    status: str
    run_status: str
    batch_status: str
    task: DispatchTaskContract
    batch: DispatchClaimedBatch
    lease_token: str
    lease_expires_at: str | None


class DispatchBatchSubmitResponse(StrictModel):
    run_id: str
    batch_id: str
    output_role: Literal["correction", "translation"]
    status: str
    run_status: str
    batch_status: str
    accepted: bool
    completed_batch_count: int = Field(ge=0)
    completed_batches: int = Field(ge=0)
    batch_count: int = Field(ge=1)
    total_batches: int = Field(ge=1)
    remaining_batches: int = Field(ge=0)
    result_artifact_id: str | None = None
    final_artifact_id: str | None = None
    finalized: bool
    result_revision_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class SourceCleaningDispatchRunCreateRequest(StrictModel):
    source_artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=16_000)
    evidence_limit: int = Field(
        default=500,
        ge=20,
        le=2_000,
        description=(
            "Maximum evidence items exposed in each phase packet. This is a "
            "transport bound, not a model-token or iteration budget."
        ),
    )
    remove_footnotes: bool | None = None
    filter_citations: bool | None = None
    pdf_ocr_mode: Literal["auto", "off", "force"] | None = None
    pdf_ocr_language: str | None = Field(default=None, min_length=2, max_length=80)
    pdf_ocr_dpi: int | None = Field(default=None, ge=120, le=400)
    pdf_remove_toc: bool | None = None
    pdf_remove_repeated_marginals: bool | None = None


class SourceCleaningDispatchDecision(StrictModel):
    operation_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["accept", "reject"]


class SourceCleaningDispatchOperation(StrictModel):
    op: Literal[
        "set_metadata",
        "delete_blocks",
        "mark_chapter",
        "unmark_chapter",
        "replace_block",
    ]
    metadata: dict[
        Annotated[str, Field(min_length=1, max_length=40)],
        Annotated[str, Field(min_length=1, max_length=2_000)],
    ] = Field(default_factory=dict, max_length=20)
    block_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=2_000,
    )
    block_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=2_000)
    replacement: str = Field(default="", max_length=50_000)
    reason: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def validate_operation_shape(self):
        if self.op == "set_metadata":
            valid = (
                bool(self.metadata)
                and not self.block_ids
                and self.block_id is None
                and not self.replacement
            )
        elif self.op == "delete_blocks":
            valid = (
                bool(self.block_ids)
                and not self.metadata
                and self.block_id is None
                and not self.replacement
            )
        elif self.op == "mark_chapter":
            valid = (
                self.block_id is not None
                and not self.metadata
                and not self.block_ids
                and not self.replacement
            )
        elif self.op == "unmark_chapter":
            valid = (
                self.block_id is not None
                and not self.metadata
                and not self.block_ids
                and self.title is None
                and not self.replacement
            )
        else:
            valid = (
                self.block_id is not None
                and bool(self.replacement.strip())
                and not self.metadata
                and not self.block_ids
                and self.title is None
            )
        if not valid:
            raise ValueError("Source-cleaning operation fields do not match its op.")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("delete_blocks block_ids must be unique.")
        return self


class SourceCleaningDispatchResult(StrictModel):
    kind: Literal["source_cleaning"]
    phase: Literal[
        "metadata",
        "navigation",
        "boilerplate",
        "repeated_elements",
        "chapter_marking",
        "text_repair",
    ]
    decisions: list[SourceCleaningDispatchDecision] = Field(
        default_factory=list,
        max_length=5_000,
    )
    operations: list[SourceCleaningDispatchOperation] = Field(
        default_factory=list,
        max_length=2_000,
    )
    summary: str = Field(default="", max_length=8_000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SourceCleaningDispatchBatchSubmitRequest(StrictModel):
    lease_token: str = Field(min_length=1, max_length=160)
    result: SourceCleaningDispatchResult


class SourceCleaningDispatchInspectionRequest(StrictModel):
    lease_token: str = Field(min_length=1, max_length=160)
    action: Literal[
        "batch",
        "inspect_document_structure",
        "inspect_navigation",
        "search",
        "regex_search",
        "preview",
        "inspect_block",
        "get_epub_markup_for_text",
        "preview_raw_markup_range",
        "list_epub_selectors",
        "preview_selector",
        "list_repeated_lines",
        "find_heading_candidates",
        "analyze_chapter_structure",
        "analyze_cleanup_structure",
        "find_footnote_candidates",
        "find_metadata_candidates",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=100)
    view: Literal["working", "baseline"] = "working"


class SourceCleaningDispatchInspectionResponse(StrictModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    batch_id: str
    phase: str
    inspection_id: str
    view: Literal["working", "baseline"]
    action: str
    observation: Any
    promoted_block_ids: list[str]
    baseline_only_block_ids: list[str]
    valid_block_id_count: int = Field(ge=0)
    lease_expires_at: str | None


class SourceCleaningDispatchBatchClaimResponse(StrictModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    batch_id: str
    batch_ordinal: int = Field(ge=1)
    status: str
    run_status: str
    batch_status: str
    task: dict[str, Any]
    batch: dict[str, Any]
    lease_token: str
    lease_expires_at: str | None


class SourceCleaningDispatchBatchSubmitResponse(StrictModel):
    run_id: str
    batch_id: str
    output_role: Literal["clean_text"]
    status: str
    run_status: str
    batch_status: str
    accepted: bool
    completed_batch_count: int = Field(ge=0)
    completed_batches: int = Field(ge=0)
    batch_count: int = Field(ge=1)
    total_batches: int = Field(ge=1)
    remaining_batches: int = Field(ge=0)
    accepted_operation_count: int = Field(ge=0)
    rejected_proposal_count: int = Field(ge=0)
    result_artifact_id: str | None = None
    final_artifact_id: str | None = None
    finalized: bool
    requires_review: bool
    validation: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None


SCHEMA_MODELS = {
    model.__name__: model
    for model in (
        ErrorBody,
        SessionCreate,
        SessionUpdate,
        SessionForkRequest,
        JobCreate,
        LoginRequest,
        BootstrapRequest,
        ManagerBootstrapRequest,
        TokenCreateRequest,
        AutomationClientCreateRequest,
        WorkflowPlanCreateRequest,
        WorkflowPlanExecuteRequest,
        ProviderCreate,
        ProviderUpdate,
        ProviderTestRequest,
        CredentialUpdate,
        PronunciationCreate,
        PronunciationUpdate,
        ModelCreate,
        ModelUpdate,
        PdfRectInput,
        PdfCropInput,
        PdfWhiteoutInput,
        PdfEditRequest,
        SubtitleSegmentInput,
        SubtitleReviewRequest,
        VoiceCreate,
        VoiceUpdate,
        VoiceTranscriptReview,
        TtsVoicePreviewRequest,
        ManagerDesiredComponentState,
        ManagerPlanRequest,
        ManagerReleasePlanRequest,
        ManagerUninstallPlanRequest,
        ManagerLegacyImportRequest,
        ManagerOperationRequest,
        ManagerRuntimeRequest,
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
        StageSelectionUpdate,
        ChunkUploadInitialize,
        GenerationSegmentCreate,
        GenerationPlanCreate,
        GenerationSegmentUpdate,
        GenerationSegmentBatchUpdateItem,
        GenerationSegmentBatchUpdate,
        GenerationStartRequest,
        OptimizationReviewItem,
        OptimizationReviewRequest,
        OutputAssemblyCreateRequest,
        OutputMixPreviewRequest,
        TtsEndpointDiscoveryRequest,
        AgentRunCreateRequest,
        DispatchRunCreateRequest,
        DispatchBatchClaimRequest,
        DispatchBatchRenewRequest,
        DispatchBatchReleaseRequest,
        DispatchCorrectionOperation,
        DispatchCorrectionResult,
        DispatchTranslationItem,
        DispatchTranslationResult,
        DispatchBatchSubmitRequest,
        DispatchCueTiming,
        DispatchCue,
        DispatchBoundaryCue,
        DispatchBoundaryContext,
        DispatchClaimedBatch,
        DispatchTaskContract,
        DispatchBatchClaimResponse,
        DispatchBatchSubmitResponse,
        SourceCleaningDispatchRunCreateRequest,
        SourceCleaningDispatchDecision,
        SourceCleaningDispatchOperation,
        SourceCleaningDispatchResult,
        SourceCleaningDispatchInspectionRequest,
        SourceCleaningDispatchInspectionResponse,
        SourceCleaningDispatchBatchSubmitRequest,
        SourceCleaningDispatchBatchClaimResponse,
        SourceCleaningDispatchBatchSubmitResponse,
    )
}
