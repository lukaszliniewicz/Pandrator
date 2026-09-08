"""Strict arguments for the resumable transcription adapter."""

from __future__ import annotations

from pathlib import PurePath
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .common import ToolInput

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
MAX_BASE64_BYTES = 8 * 1024 * 1024
MAX_BASE64_CHARS = ((MAX_BASE64_BYTES + 2) // 3) * 4
SUPPORTED_TRANSCRIPTION_SUFFIXES = frozenset(
    {
        ".wav",
        ".mp3",
        ".m4a",
        ".mp4",
        ".webm",
        ".ogg",
        ".opus",
        ".flac",
        ".aac",
        ".aiff",
        ".aif",
        ".wma",
        ".mkv",
        ".mov",
        ".avi",
        ".mpeg",
        ".mpg",
    }
)


def validate_transcription_filename(value: str) -> str:
    """Validate a source filename before opening or decoding source bytes."""

    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or not value.strip()
    ):
        raise ValueError("Transcription filenames must be plain filenames.")
    if PurePath(value).suffix.lower() not in SUPPORTED_TRANSCRIPTION_SUFFIXES:
        raise ValueError("Supply a supported audio or video file.")
    return value


class LocalFileTranscriptionSource(ToolInput):
    """A file selected beneath an operator-approved named root."""

    kind: Literal["local_file"] = "local_file"
    root: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=2048)


class Base64TranscriptionSource(ToolInput):
    """A bounded base64 encoded upload supplied by the MCP caller."""

    kind: Literal["base64"] = "base64"
    data: str = Field(min_length=1, max_length=MAX_BASE64_CHARS)
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def validate_filename(_cls, value: str) -> str:
        return validate_transcription_filename(value)


TranscriptionSource = Annotated[
    LocalFileTranscriptionSource | Base64TranscriptionSource,
    Field(discriminator="kind"),
]


class TranscribeInput(ToolInput):
    source: TranscriptionSource
    format: Literal["txt", "srt", "json"] = "txt"
    language: str = Field(
        default="auto",
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    engine: str | None = Field(
        default=None,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    model_quantization: str | None = Field(
        default=None,
        max_length=40,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    compute_backend: Literal["auto", "cpu", "cuda", "vulkan", "metal"] | None = None
    wait_seconds: int = Field(default=30, ge=0, le=30)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class GetTranscriptionInput(ToolInput):
    id: str = Field(min_length=1, max_length=120)
    format: Literal["txt", "srt", "json"] | None = None
    wait_seconds: int = Field(default=0, ge=0, le=30)


class GetTranscriptionResultInput(ToolInput):
    id: str = Field(min_length=1, max_length=120)
    format: Literal["txt", "srt", "json"] = "txt"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16_000, ge=1, le=32_768)


class CancelTranscriptionInput(ToolInput):
    id: str = Field(min_length=1, max_length=120)


class DeleteTranscriptionInput(ToolInput):
    id: str = Field(min_length=1, max_length=120)
