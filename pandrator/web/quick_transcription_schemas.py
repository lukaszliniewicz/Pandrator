"""Bounded public settings for quick transcription (no credentials or paths)."""

from pathlib import PurePath
from typing import Literal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SOURCE_BYTES = 256 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024
INLINE_BYTES = 32768
TranscriptFormat = Literal["txt", "srt", "json"]


class TranscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=MAX_SOURCE_BYTES)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    format: TranscriptFormat = "txt"
    language: str | None = Field(
        default=None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$"
    )
    engine: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    model_quantization: str | None = Field(
        default=None, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    compute_backend: Literal["auto", "cpu", "cuda", "vulkan", "metal"] | None = None

    @field_validator("filename")
    @classmethod
    def media_filename(_cls, value: str) -> str:
        if "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("Supply a filename, not a filesystem path.")
        if PurePath(value).suffix.lower() not in {
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
        }:
            raise ValueError("Supply a supported audio or video file.")
        return value


class TranscriptionWait(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wait_seconds: float = Field(default=0, ge=0, le=30)


class TranscriptionResult(BaseModel):
    format: TranscriptFormat
    mime_type: str
    content: str | dict[str, Any]
    size_bytes: int


class TranscriptionSnapshot(BaseModel):
    id: str
    job_id: str | None
    status: str
    progress: float
    progress_detail: str | None
    expires_at: str
    format: TranscriptFormat
    chunk_size: int
    next_chunk_index: int
    uploaded_bytes: int
    size_bytes: int
    result_available: bool
    inline_result: bool
    result_url: str
    result: TranscriptionResult | None = None
    error: dict[str, str] | None = None


class TranscriptionResultPage(BaseModel):
    format: TranscriptFormat
    content: str
    offset: int
    total_chars: int
    next_offset: int | None
