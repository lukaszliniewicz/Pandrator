"""Bounded public preview, admission and status for current-project copies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class ProjectCopyPreviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=36)


class ProjectCopyConnectionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(pattern=_UUID)
    provider_model_id: str = Field(pattern=_UUID)


class ProjectCopyPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    destination_workspace_id: str = Field(min_length=1, max_length=36)
    destination_connections: list[ProjectCopyConnectionChoice] = Field(
        default_factory=list, max_length=512
    )


class ProjectCopyRequest(ProjectCopyPreviewRequest):
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str | None = Field(default=None, min_length=1, max_length=255)


class ProjectCopyBlocker(BaseModel):
    code: str
    domain: str


class ProjectCopyConnectionRequirement(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["correction", "translation", "speech_preparation"]
    session_count: int = Field(ge=1)
    session_names: list[str]
    selected_provider_id: str | None = None
    selected_provider_model_id: str | None = None


class ProjectCopyConnectionOption(BaseModel):
    provider_id: str
    provider_model_id: str
    provider_label: str
    model_name: str


class ProjectCopyPreview(BaseModel):
    source_project_id: str
    destination_workspace_id: str
    name: str
    fingerprint: str
    counts: dict[str, int]
    file_count: int
    known_bytes: int
    unknown_size_count: int
    active_job_count: int
    ready: bool
    blockers: list[ProjectCopyBlocker]
    connection_requirements: list[ProjectCopyConnectionRequirement] = Field(default_factory=list)
    connection_options: list[ProjectCopyConnectionOption] = Field(default_factory=list)


class ProjectCopyResultResponse(BaseModel):
    project_id: str
    source_session_id: str
    translation_project_id: str | None
    workspace_id: str
    name: str
    fingerprint: str


class ProjectCopyStatusError(BaseModel):
    code: str | None
    message: str


class ProjectCopyStatus(BaseModel):
    id: str
    kind: str
    state: str
    progress: float
    detail: str | None
    error: ProjectCopyStatusError | None
    result: ProjectCopyResultResponse | None


PROJECT_COPY_SCHEMAS = {
    model.__name__: model
    for model in (
        ProjectCopyConnectionChoice,
        ProjectCopyPreviewRequest,
        ProjectCopyConnectionRequirement,
        ProjectCopyConnectionOption,
        ProjectCopyRequest,
        ProjectCopyBlocker,
        ProjectCopyPreview,
        ProjectCopyResultResponse,
        ProjectCopyStatusError,
        ProjectCopyStatus,
    )
}
