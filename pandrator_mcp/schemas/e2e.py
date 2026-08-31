"""Typed arguments for source import, TTS selection, export, and delivery."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ToolInput

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"


class BrowseLocalSourcesInput(ToolInput):
    root: str | None = Field(
        default=None,
        max_length=80,
        description="Configured root name. Omit to list available root names.",
    )
    directory: str = Field(
        default="",
        max_length=1024,
        description="POSIX-style relative directory inside the configured root.",
    )
    query: str | None = Field(
        default=None,
        max_length=160,
        description="Optional case-insensitive filename substring.",
    )
    recursive: bool = Field(
        default=False,
        description="Search descendants up to five levels deep.",
    )
    sort: Literal["modified_desc", "name_asc"] = "modified_desc"
    limit: int = Field(default=50, ge=1, le=200)


class ImportLocalSourceInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    root: str = Field(
        min_length=1,
        max_length=80,
        description="Human-configured root name returned by local source browsing.",
    )
    relative_path: str = Field(
        min_length=1,
        max_length=2048,
        description="Relative file path returned by local source browsing.",
    )
    role: Literal["primary", "reference"] = "primary"
    expected_session_revision: int = Field(
        ge=1,
        description="Current session revision used to prevent attaching to stale state.",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
        description="Stable retry identity for this exact import and attachment.",
    )


class TtsCatalogInput(ToolInput):
    service_id: str | None = Field(default=None, max_length=160)
    refresh: bool = Field(
        default=False,
        description="Probe configured services for current availability and dynamic catalogs.",
    )


class ConfigureTtsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    service_id: str = Field(
        min_length=1,
        max_length=160,
        description="Exact service ID from pandrator_get_tts_catalog.",
    )
    model: str | None = Field(
        default=None,
        max_length=300,
        description="Exact advertised model; omit to use the service default.",
    )
    voice: str | None = Field(
        default=None,
        max_length=300,
        description="Provider voice ID or managed voice ID/name from the TTS catalog.",
    )
    language: str | None = Field(default=None, min_length=2, max_length=40)
    style_instructions: str | None = Field(
        default=None,
        max_length=12_000,
        description="Natural-language delivery/style instructions, when supported.",
    )
    expected_revision: int = Field(
        ge=0,
        description="Current revision of the session's tts settings section.",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ListGenerationRunsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=20, ge=1, le=100)


class PlanExportVariantInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    generation_run_id: str | None = Field(default=None, max_length=80)
    export_mode: Literal["media", "subtitles", "text"] = "media"
    audio_mode: Literal["preserve", "mixed", "dubbing_only"] = "mixed"
    subtitle_mode: Literal["none", "soft", "burned"] = "none"
    subtitle_selection: Literal["source", "translation", "dual"] = "translation"
    subtitle_format: Literal["srt", "vtt"] = "srt"
    expires_in_minutes: int = Field(default=30, ge=1, le=60)


class DownloadArtifactInput(ToolInput):
    artifact_id: str = Field(min_length=1, max_length=80)
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Optional local filename inside the configured output root.",
    )
