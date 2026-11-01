"""Strict, bounded inputs for project viewing tools."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from .common import ToolInput


class ProjectSharingToolInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListSharedProjectsInput(ProjectSharingToolInput):
    query: str = Field(default="", max_length=255)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class GetProjectViewersInput(ProjectSharingToolInput):
    project_id: str = Field(min_length=1, max_length=36)


class SearchProjectViewersInput(GetProjectViewersInput):
    query: str = Field(min_length=2, max_length=64)


class ProjectViewerMutationInput(GetProjectViewersInput):
    user_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=1)


PROJECT_SHARING_INPUT_MODELS = (
    ProjectSharingToolInput,
    ListSharedProjectsInput,
    GetProjectViewersInput,
    SearchProjectViewersInput,
    ProjectViewerMutationInput,
)


__all__ = [
    "GetProjectViewersInput",
    "ListSharedProjectsInput",
    "PROJECT_SHARING_INPUT_MODELS",
    "ProjectSharingToolInput",
    "ProjectViewerMutationInput",
    "SearchProjectViewersInput",
]
