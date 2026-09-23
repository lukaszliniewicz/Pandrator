"""Typed arguments for source import, TTS selection, export, and delivery."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, field_validator

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
    role: Literal["primary", "reference", "transcript", "media"] = "primary"
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


class CreateTextSourceInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    text: str = Field(
        min_length=1,
        max_length=1_000_000,
        description="UTF-8 plain text to store as a managed source. The text is not echoed in the tool result.",
    )
    filename: str = Field(
        default="inline.txt",
        min_length=1,
        max_length=255,
        description="Plain filename for the managed text source.",
    )
    role: Literal["primary", "reference", "transcript", "media"] = "primary"
    expected_session_revision: int = Field(
        ge=1,
        description="Current session revision used to prevent attaching to stale state.",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
        description="Stable retry identity for this exact source creation and attachment.",
    )


class TtsCatalogInput(ToolInput):
    service_id: str | None = Field(default=None, max_length=160)
    include_compatibility: bool = Field(
        default=False,
        description="Include services retained for compatibility with older configurations.",
    )
    model: str | None = Field(
        default=None,
        max_length=300,
        description="Optional case-insensitive model filter for service discovery.",
    )
    query: str | None = Field(
        default=None,
        max_length=160,
        description="Optional case-insensitive service/model search term.",
    )
    available_only: bool = Field(
        default=False,
        description="Return only services currently advertised as available.",
    )
    detail: Literal["summary", "full"] = Field(
        default="summary",
        description="Summary omits large voice catalogues and metadata; full returns exact voice choices.",
    )
    refresh: bool = Field(
        default=False,
        description="Probe configured services for current availability and dynamic catalogs.",
    )


class AudioCppCatalogueInput(ToolInput):
    category: str = Field(default="", max_length=160)
    family: str = Field(default="", max_length=160)
    query: str = Field(default="", max_length=160)
    language: str = Field(default="", max_length=160)
    capability: str = Field(default="", max_length=160)
    commercial_use: Literal[
        "",
        "permitted",
        "noncommercial",
        "conditional",
        "unknown",
    ] = ""
    recommended_only: bool = False
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10_000)


class ElevenLabsVoiceSettingsInput(ToolInput):
    stability: float | None = Field(default=None, ge=0, le=1)
    similarity_boost: float | None = Field(default=None, ge=0, le=1)
    style: float | None = Field(default=None, ge=0, le=1)
    speed: float | None = Field(default=None, ge=0.25, le=4)
    use_speaker_boost: bool | None = Field(default=None, strict=True)

    @field_validator("stability", "similarity_boost", "style", "speed", mode="before")
    @classmethod
    def finite_numeric(cls, value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Voice settings require finite numeric values.")
        return value

    @field_validator("use_speaker_boost", mode="before")
    @classmethod
    def strict_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("use_speaker_boost requires a boolean.")
        return value


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
    tts_context_mode: Literal["off", "before", "both"] | None = None
    performance_context_before: int | None = Field(default=None, ge=0, le=20)
    performance_context_after: int | None = Field(default=None, ge=0, le=20)
    performance_context_max_chars: int | None = Field(default=None, ge=0, le=16_000)
    performance_allow_vocalizations: bool | None = None
    elevenlabs_voice_settings: ElevenLabsVoiceSettingsInput | None = None
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
    include_repairs: bool = Field(
        default=False,
        description="Include raw early-timing repair child runs in the result.",
    )


class PlanExportVariantInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    generation_run_id: str | None = Field(default=None, max_length=80)
    export_mode: Literal["media", "audio", "subtitles", "text"] = "media"
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
