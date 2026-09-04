"""Strict MCP arguments for passive speech-text optimization."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ToolInput
from .delegation import DelegationContextDeltaInput, DelegationExecutionMixin

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"


class CreateSpeechOptimizationDispatchRunInput(DelegationExecutionMixin):
    session_id: str = Field(min_length=1, max_length=80)
    source_artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    language: str | None = Field(default=None, min_length=1, max_length=40)
    voice_language: str | None = Field(default=None, min_length=1, max_length=40)
    tts_service: str | None = Field(default=None, min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=16_000)
    char_limit: int = Field(
        default=20_000,
        ge=1,
        le=1_000_000,
        description=(
            "Target source characters per transport batch. A single source unit "
            "is never split. This is not a model token or iteration budget."
        ),
    )
    max_units_per_batch: int = Field(default=100, ge=1, le=500)
    context_before: int = Field(default=4, ge=0, le=20)
    context_after: int = Field(default=2, ge=0, le=20)
    include_timing: bool = True
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ListSpeechOptimizationDispatchRunsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=50, ge=1, le=100)


class GetSpeechOptimizationDispatchRunInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)


class ClaimSpeechOptimizationDispatchBatchInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class RenewSpeechOptimizationDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ReleaseSpeechOptimizationDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class SpeechOptimizationDispatchItemInput(ToolInput):
    unit_id: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4 * 1024 * 1024)


class SpeechOptimizationDispatchResultInput(ToolInput):
    kind: Literal["speech_optimization"] = "speech_optimization"
    items: list[SpeechOptimizationDispatchItemInput] = Field(
        min_length=1,
        max_length=500,
    )


class SubmitSpeechOptimizationDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    result: SpeechOptimizationDispatchResultInput
    context_delta: DelegationContextDeltaInput = Field(default_factory=DelegationContextDeltaInput)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )
