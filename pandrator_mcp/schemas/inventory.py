"""Read-only provider, artifact, and voice-catalog arguments."""

from __future__ import annotations

from pydantic import Field

from .common import ToolInput


class ListArtifactsInput(ToolInput):
    session_id: str | None = Field(default=None, max_length=80)
    kind: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=50, ge=1, le=100)


class ProviderStatusInput(ToolInput):
    include_disabled: bool = True


class VoiceCatalogInput(ToolInput):
    language: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=100, ge=1, le=200)
