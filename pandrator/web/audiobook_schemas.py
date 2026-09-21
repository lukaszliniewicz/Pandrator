"""Validated HTTP request contracts for audiobook setup and preview."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from pandrator.logic.speech_performance import StrictModel


def _trim_nonblank(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("This value may not be blank.")
    return normalized


class AudiobookSetupConfigureRequest(StrictModel):
    """Revision-checked audiobook voice-mode configuration."""

    expected_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    mode: Literal["single_voice", "multi_voice"]


class SpeechPlanPreviewRequest(StrictModel):
    """Read-only compilation request for one speech-plan segment."""

    revision_id: str = Field(min_length=1, max_length=128)
    segment_id: str = Field(min_length=1, max_length=128)
    generation_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_request: bool = False

    @field_validator("revision_id", "segment_id", "generation_run_id", mode="before")
    @classmethod
    def trim_identifiers(cls, value: Any) -> Any:
        return _trim_nonblank(value) if value is not None else value


# Keep the endpoint-oriented short name available to callers that treat the
# setup body as the resource request; the OpenAPI schema retains the explicit
# configure name above.
AudiobookSetupRequest = AudiobookSetupConfigureRequest


AUDIOBOOK_SCHEMAS = {
    AudiobookSetupConfigureRequest.__name__: AudiobookSetupConfigureRequest,
    SpeechPlanPreviewRequest.__name__: SpeechPlanPreviewRequest,
}


__all__ = [
    "AUDIOBOOK_SCHEMAS",
    "AudiobookSetupRequest",
    "AudiobookSetupConfigureRequest",
    "SpeechPlanPreviewRequest",
]
