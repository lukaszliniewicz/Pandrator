"""Arguments for the read-only media-edit workflow procedure planner."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput
from .sessions import _guard_settings


def _validate_relative_path(value: str) -> str:
    raw = value.strip()
    if not raw or "\\" in raw or "\x00" in raw:
        raise ValueError("Source paths must use safe POSIX-style relative components.")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Source paths must stay inside their configured root.")
    return relative.as_posix()


class MediaEditSourceReference(ToolInput):
    """An existing source asset or a file below one approved named root."""

    source_asset_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Reusable source asset ID; mutually exclusive with root and relative_path.",
    )
    root: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="Approved named local root returned by pandrator_browse_local_sources.",
    )
    relative_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_048,
        description="POSIX-style file path below root; must be supplied together with root.",
    )

    @field_validator("root")
    @classmethod
    def strip_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Source root must not be blank.")
        return normalized

    @field_validator("source_asset_id")
    @classmethod
    def strip_source_asset_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Source asset ID must not be blank.")
        return normalized

    @field_validator("relative_path")
    @classmethod
    def normalize_relative_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def require_exact_reference(self) -> "MediaEditSourceReference":
        has_asset = self.source_asset_id is not None
        has_local = self.root is not None or self.relative_path is not None
        if has_asset and has_local:
            raise ValueError("Choose source_asset_id or root plus relative_path, not both.")
        if has_asset:
            return self
        if self.root is None or self.relative_path is None:
            raise ValueError("Provide source_asset_id or both root and relative_path.")
        return self


class PlanMediaEditWorkflowInput(ToolInput):
    """Inputs for a live, read-only source-to-render media-edit procedure."""

    session_id: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=10_000)
    transcript_mode: Literal["auto", "captions", "asr"] = "auto"
    recording_source: MediaEditSourceReference | None = None
    transcript_source: MediaEditSourceReference | None = None
    caption_alignment_method: Literal["ctc", "ctc_asr_fallback", "asr"] = "ctc"
    caption_alignment_ctc_model: Literal[
        "auto",
        "canary-ctc-aligner",
        "canary-ctc-aligner-q4_k.gguf",
    ] = "auto"
    caption_alignment_padding_ms: int = Field(default=2_000, ge=250, le=5_000)
    caption_alignment_batch_seconds: int = Field(default=30, ge=5, le=60)
    caption_alignment_min_confidence: float = Field(default=0.5, ge=0.5, le=1.0)
    caption_alignment_fallback_coverage: float = Field(default=0.9, ge=0.0, le=1.0)
    stt_overrides: dict[str, Any] = Field(default_factory=dict)
    wait_seconds: int = Field(default=0, ge=0, le=3_600)
    expires_in_minutes: int = Field(default=30, ge=1, le=60)
    materialize: bool = False
    filename: str | None = Field(default=None, max_length=255)

    @field_validator("instructions")
    @classmethod
    def strip_instructions(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Instructions must not be blank.")
        return normalized

    @field_validator("stt_overrides")
    @classmethod
    def validate_stt_overrides(cls, value: dict[str, Any]) -> dict[str, Any]:
        # These values are persisted through pandrator_update_session_settings,
        # so apply that tool's stricter path/connection/credential boundary here
        # instead of producing a next action that its own schema would reject.
        _guard_settings(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("STT overrides must be finite JSON values.") from error
        if len(encoded) > 128 * 1024:
            raise ValueError("STT overrides exceed the MCP size limit.")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise ValueError("Filename must be a plain filename without path components.")
        return normalized

    @model_validator(mode="after")
    def validate_materialization(self) -> "PlanMediaEditWorkflowInput":
        if self.transcript_mode == "asr" and self.transcript_source is not None:
            raise ValueError("transcript_source cannot be supplied when transcript_mode is asr.")
        if self.filename is not None and not self.materialize:
            raise ValueError("A filename requires materialize=true.")
        return self
