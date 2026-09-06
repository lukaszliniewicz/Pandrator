"""Revision-safe media-edit tool arguments."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from .common import ToolInput

_MAX_JOB_WAIT_SECONDS = 3_600
_DEFAULT_JOB_WAIT_SECONDS = 60
_IDEMPOTENCY_KEY = {
    "min_length": 8,
    "max_length": 200,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
}


class MediaEditKeepRange(ToolInput):
    """One start-inclusive, end-exclusive media interval to preserve."""

    id: str | None = Field(default=None, min_length=1, max_length=120)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    label: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_interval(self) -> "MediaEditKeepRange":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class GetMediaEditArguments(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


class PrepareMediaEditArguments(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    force: bool = False
    idempotency_key: str = Field(**_IDEMPOTENCY_KEY)


class UpdateMediaEditArguments(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    keep_ranges: list[MediaEditKeepRange] = Field(min_length=1, max_length=1000)
    instructions: str | None = Field(default=None, max_length=10_000)
    reviewed: bool | None = None
    idempotency_key: str = Field(**_IDEMPOTENCY_KEY)


class ProposeMediaEditArguments(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    instructions: str = Field(min_length=1, max_length=10_000)
    wait: bool = True
    timeout_seconds: int = Field(
        default=_DEFAULT_JOB_WAIT_SECONDS,
        ge=0,
        le=_MAX_JOB_WAIT_SECONDS,
    )
    idempotency_key: str = Field(**_IDEMPOTENCY_KEY)

    @field_validator("instructions")
    @classmethod
    def strip_instructions(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Instructions must not be blank.")
        return normalized


class RenderMediaEditArguments(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    wait: bool = True
    timeout_seconds: int = Field(
        default=_DEFAULT_JOB_WAIT_SECONDS,
        ge=0,
        le=_MAX_JOB_WAIT_SECONDS,
    )
    idempotency_key: str = Field(**_IDEMPOTENCY_KEY)
