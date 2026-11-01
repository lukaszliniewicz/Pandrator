"""Public contracts for named, read-only project sharing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectViewingRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1)


class SharedProjectsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=255)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ProjectViewer(BaseModel):
    user_id: str
    login_name: str
    display_name: str
    is_active: bool
    access: Literal["view"]
    granted_at: str
    granted_by_user_id: str | None


class ProjectViewersResponse(BaseModel):
    project_id: str
    revision: int
    items: list[ProjectViewer]


class ProjectViewingCandidate(BaseModel):
    user_id: str
    login_name: str
    display_name: str


class ProjectViewingCandidatesResponse(BaseModel):
    items: list[ProjectViewingCandidate]


class SharedProjectSummary(BaseModel):
    project_id: str
    source_session_id: str
    name: str
    workflow_kind: str
    source_language: str | None
    target_language: str | None
    updated_at: str


class SharedProjectsResponse(BaseModel):
    items: list[SharedProjectSummary]


PROJECT_SHARING_SCHEMAS = {
    model.__name__: model
    for model in (
        ProjectViewingRevisionRequest,
        ProjectViewer,
        ProjectViewersResponse,
        ProjectViewingCandidate,
        ProjectViewingCandidatesResponse,
        SharedProjectSummary,
        SharedProjectsResponse,
    )
}
