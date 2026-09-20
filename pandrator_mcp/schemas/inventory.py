"""Read-only provider, artifact, and voice-catalog arguments."""

from __future__ import annotations

from pydantic import Field

from .common import ToolInput
from .voice_lifecycle import VoiceCatalogInput


class ListArtifactsInput(ToolInput):
    session_id: str | None = Field(default=None, max_length=80)
    kind: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=50, ge=1, le=100)


class ProviderStatusInput(ToolInput):
    include_disabled: bool = True


__all__ = ["ListArtifactsInput", "ProviderStatusInput", "VoiceCatalogInput"]
