"""Strict, bounded inputs for copying the current project."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import ToolInput

_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class ProjectCopyConnectionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(pattern=_UUID)
    provider_model_id: str = Field(pattern=_UUID)


class ProjectCopyToolInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class PreviewProjectCopyInput(ProjectCopyToolInput):
    project_id: str = Field(min_length=1, max_length=36)
    destination_workspace_id: str = Field(min_length=1, max_length=36)
    destination_connections: list[ProjectCopyConnectionChoice] | None = Field(default=None, max_length=512)


class CopyCurrentProjectInput(ProjectCopyToolInput):
    project_id: str = Field(min_length=1, max_length=36)
    destination_workspace_id: str = Field(min_length=1, max_length=36)
    destination_connections: list[ProjectCopyConnectionChoice] | None = Field(default=None, max_length=512)
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_IDEMPOTENCY_KEY_PATTERN,
    )


class GetProjectCopyInput(ProjectCopyToolInput):
    copy_id: str = Field(min_length=1, max_length=36)


PROJECT_COPY_INPUT_MODELS = (
    ProjectCopyToolInput,
    PreviewProjectCopyInput,
    CopyCurrentProjectInput,
    GetProjectCopyInput,
)


__all__ = [
    "CopyCurrentProjectInput",
    "GetProjectCopyInput",
    "PreviewProjectCopyInput",
    "PROJECT_COPY_INPUT_MODELS",
    "ProjectCopyToolInput",
    "ProjectCopyConnectionChoice",
]
