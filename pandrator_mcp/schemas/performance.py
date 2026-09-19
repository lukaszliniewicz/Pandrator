"""Portable MCP contracts. The target supplies the authoritative pSSML schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import ToolInput


class PerformanceSessionInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


class ListPerformancePlansInput(PerformanceSessionInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=100)
    plan_revision_id: str | None = Field(default=None, min_length=1, max_length=80)


class PerformancePlanInput(PerformanceSessionInput):
    plan_id: str = Field(min_length=1, max_length=80)


class GetPerformancePlanInput(PerformancePlanInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=100)


class PerformanceWriteInput(PerformancePlanInput):
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
    )


class CreatePerformancePlanInput(PerformanceSessionInput):
    expected_plan_revision_id: str = Field(min_length=1, max_length=80)
    mode: Literal["manual", "passive", "llm"] = "passive"
    model_name: str = Field(default="", max_length=255)
    instructions: str = Field(default="", max_length=6000)
    context_before: int = Field(default=2, ge=0, le=20)
    context_after: int = Field(default=1, ge=0, le=20)
    context_max_chars: int = Field(default=4000, ge=0, le=16000)
    batch_size: int = Field(default=12, ge=1, le=32)
    allow_vocalizations: bool = False
    copy_from_id: str | None = Field(default=None, min_length=1, max_length=80)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
    )


class PerformanceItemInput(ToolInput):
    segment_id: str = Field(min_length=1, max_length=80)
    annotation: dict[str, Any] = Field(
        description="pandrator.performance/v1 annotation; use the exact annotation_schema returned by claim. Never include transcript edits or provider markup."
    )


class EditPerformancePlanInput(PerformanceWriteInput):
    expected_version: int = Field(ge=1)
    items: list[PerformanceItemInput] = Field(min_length=1, max_length=100)
    unlock_locked: bool = False


class AdoptPerformancePlanInput(PerformanceWriteInput):
    expected_version: int = Field(ge=1)
    accept_unanalysed: bool = False
    enable: bool = True


class PreviewPerformancePlanInput(PerformancePlanInput):
    segment_id: str = Field(min_length=1, max_length=80)
    annotation: dict[str, Any] | None = None
    service: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=255)
    generation_prompt: str | None = Field(default=None, max_length=4000)
    context_mode: Literal["off", "before", "both"] | None = None
    allow_vocalizations: bool | None = None


class AnalysePerformancePlanInput(PerformanceWriteInput):
    pass


class ClaimPerformanceBatchInput(PerformanceWriteInput):
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class PerformanceBatchInput(PerformanceWriteInput):
    batch_id: str = Field(min_length=1, max_length=80)
    lease_token: str = Field(min_length=16, max_length=64)


class SubmitPerformanceBatchInput(PerformanceBatchInput):
    items: list[PerformanceItemInput] = Field(min_length=1, max_length=32)


class RenewPerformanceBatchInput(PerformanceBatchInput):
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class ReleasePerformanceBatchInput(PerformanceBatchInput):
    pass


PERFORMANCE_INPUT_MODELS = (
    ListPerformancePlansInput,
    GetPerformancePlanInput,
    CreatePerformancePlanInput,
    EditPerformancePlanInput,
    AdoptPerformancePlanInput,
    PreviewPerformancePlanInput,
    AnalysePerformancePlanInput,
    ClaimPerformanceBatchInput,
    SubmitPerformanceBatchInput,
    RenewPerformanceBatchInput,
    ReleasePerformanceBatchInput,
)
