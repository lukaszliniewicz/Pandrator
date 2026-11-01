"""Strict bounded request and response contracts for public project links."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
UuidString = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=_UUID_PATTERN,
    ),
]
LinkMode = Literal["manage_project", "review_translation"]
SubmissionStatus = Literal["pending", "accepted", "rejected"]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublicSharingSettingsRequest(_StrictRequest):
    expected_revision: int = Field(ge=0)
    enabled: bool
    public_origin: str | None


class PublicSharingHosting(BaseModel):
    observed_origin: str
    canonical_origin: str
    secure_transport: bool
    secure_cookies: bool
    trusted_hosts: list[str]
    proxy_hops: int = Field(ge=0)


class PublicSharingSettingsResponse(BaseModel):
    enabled: bool
    public_origin: str | None
    revision: int = Field(ge=0)
    hosting: PublicSharingHosting


class ProjectLinkCreateRequest(_StrictRequest):
    label: str = Field(min_length=1, max_length=80)
    mode: LinkMode
    expires_in_days: int = Field(default=7, ge=1, le=30)
    target_artifact_id: UuidString | None = None
    request_key: UuidString

    @field_validator("label")
    @classmethod
    def nonblank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label must not be blank")
        return value

    @model_validator(mode="after")
    def target_matches_mode(self) -> ProjectLinkCreateRequest:
        if (self.mode == "review_translation") != (self.target_artifact_id is not None):
            raise ValueError("target_artifact_id must match the selected link mode")
        return self


class ProjectLinkRevokeRequest(_StrictRequest):
    expected_revision: int = Field(ge=1)


class PublicLinkRedeemRequest(_StrictRequest):
    token: str = Field(min_length=1, max_length=256)


class PublicReviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class TranslationChange(_StrictRequest):
    segment_id: str = Field(min_length=1, max_length=36)
    text: str = Field(min_length=1, max_length=20_000)


class PublicTranslationSubmissionRequest(_StrictRequest):
    expected_source_hash: str = Field(min_length=1, max_length=128)
    changes: list[TranslationChange] = Field(min_length=1, max_length=5000)
    reviewer_label: str | None = Field(default=None, max_length=80)


class LinkSubmissionsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubmissionStatus = "pending"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=100)


class SubmissionDetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class SubmissionAcceptRequest(_StrictRequest):
    expected_revision: int = Field(ge=1)
    expected_source_hash: str = Field(min_length=1, max_length=128)
    expected_document_revision: int = Field(ge=1)
    expected_selected_artifact_id: UuidString


class SubmissionRejectRequest(_StrictRequest):
    expected_revision: int = Field(ge=1)


class ProjectLink(BaseModel):
    id: UuidString
    project_id: UuidString
    root_session_id: UuidString
    label: str
    mode: LinkMode
    revision: int = Field(ge=1)
    target_session_id: UuidString | None
    target_artifact_id: UuidString | None
    target_revision_id: UuidString | None
    target_hash: str | None
    target_revision_number: int | None
    target_composition_hash: str | None
    expires_at: str
    revoked_at: str | None


class ProjectLinkListResponse(BaseModel):
    items: list[ProjectLink]


class ProjectLinkCreateResponse(BaseModel):
    link: ProjectLink
    url: str | None
    already_created: bool


class ProjectLinkResponse(BaseModel):
    link: ProjectLink


class PublicLinkRedeemResponse(BaseModel):
    link: ProjectLink
    csrf_token: str


class PublicReviewItem(BaseModel):
    id: str
    ordinal: int
    start_ms: int | None
    end_ms: int | None
    text: str


class PublicTranslationReviewResponse(BaseModel):
    link_id: UuidString
    project_id: UuidString
    label: str
    mode: Literal["review_translation"]
    target_artifact_id: UuidString
    target_hash: str
    document_revision: int = Field(ge=1)
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    items: list[PublicReviewItem]


class PublicTranslationSubmissionResponse(BaseModel):
    submission_id: UuidString
    status: SubmissionStatus
    revision: int = Field(ge=1)
    change_count: int = Field(ge=0)
    created_at: str


class LinkSubmissionSummary(PublicTranslationSubmissionResponse):
    link_id: UuidString
    link_label: str
    reviewer_label: str | None
    reviewer_label_verified: bool
    reviewed_at: str | None
    reviewed_by_user_id: UuidString | None
    accepted_artifact_id: UuidString | None


class LinkSubmissionsResponse(BaseModel):
    items: list[LinkSubmissionSummary]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)


class SubmissionChangeDetail(BaseModel):
    segment_id: str
    ordinal: int | None
    start_ms: int | None
    end_ms: int | None
    before: str | None
    after: str


class LinkSubmissionDetailResponse(BaseModel):
    submission: LinkSubmissionSummary
    project_id: UuidString
    target_artifact_id: UuidString
    target_hash: str
    document_revision: int = Field(ge=1)
    target_composition_hash: str
    selected_artifact_id: UuidString | None
    changes: list[SubmissionChangeDetail]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    source_unavailable: bool = False


class ProjectLinkServiceModel(BaseModel):
    id: str
    label: str | None = None
    family: str | None = None
    voice_mode: str | None = None
    available: bool | None = None
    online: bool | None = None
    loaded: bool | None = None
    supported_languages: list[str] | None = None


class ProjectLinkService(BaseModel):
    id: str
    name: str | None = None
    adapter: str | None = None
    available: bool | None = None
    online: bool | None = None
    supports_voice_cloning: bool | None = None
    supports_predefined_voices: bool | None = None
    has_multiple_models: bool | None = None
    models: list[str]
    model_catalog: list[ProjectLinkServiceModel]


class ProjectLinkServiceWarning(BaseModel):
    code: str
    service_id: str
    model_id: str


class ProjectLinkServicesResponse(BaseModel):
    services: list[ProjectLinkService]
    session_id: UuidString
    scope: Literal["project"]
    can_edit: Literal[False]
    warnings: list[ProjectLinkServiceWarning]


PUBLIC_LINK_SCHEMAS = {
    model.__name__: model
    for model in (
        PublicSharingSettingsRequest,
        PublicSharingHosting,
        PublicSharingSettingsResponse,
        ProjectLinkCreateRequest,
        ProjectLinkRevokeRequest,
        PublicLinkRedeemRequest,
        TranslationChange,
        PublicTranslationSubmissionRequest,
        SubmissionAcceptRequest,
        SubmissionRejectRequest,
        ProjectLink,
        ProjectLinkListResponse,
        ProjectLinkCreateResponse,
        ProjectLinkResponse,
        PublicLinkRedeemResponse,
        PublicReviewItem,
        PublicTranslationReviewResponse,
        PublicTranslationSubmissionResponse,
        LinkSubmissionSummary,
        LinkSubmissionsResponse,
        SubmissionChangeDetail,
        LinkSubmissionDetailResponse,
        ProjectLinkServiceModel,
        ProjectLinkService,
        ProjectLinkServiceWarning,
        ProjectLinkServicesResponse,
    )
}
