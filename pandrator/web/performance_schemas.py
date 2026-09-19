"""Strict API and worker contracts for contextual performance planning."""

from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator
from pandrator.logic.speech_performance import PerformanceAnnotation, StrictModel


class PerformancePlanCreateRequest(StrictModel):
    expected_plan_revision_id: str = Field(min_length=1, max_length=80)
    mode: Literal["manual", "passive", "llm"] = "manual"
    annotation_format: Literal["pssml", "xml"] = "pssml"
    model_name: str = Field(default="", max_length=255)
    instructions: str = Field(default="", max_length=6000)
    context_before: int = Field(default=2, ge=0, le=20)
    context_after: int = Field(default=1, ge=0, le=20)
    context_max_chars: int = Field(default=4000, ge=0, le=16000)
    batch_size: int = Field(default=12, ge=1, le=32)
    allow_vocalizations: bool = False
    copy_from_id: str | None = Field(default=None, min_length=1, max_length=80)


class PerformanceItem(StrictModel):
    segment_id: str = Field(min_length=1, max_length=80)
    annotation: PerformanceAnnotation | None = None
    speech_xml: str | None = Field(default=None, max_length=256 * 1024)
    locked: bool = False
    reason: str = Field(default="", max_length=1600)

    @model_validator(mode="after")
    def exactly_one_format(self):
        if (self.annotation is None) == (self.speech_xml is None):
            raise ValueError("Supply exactly one of annotation or speech_xml.")
        return self


class PerformanceResult(StrictModel):
    items: list[PerformanceItem] = Field(min_length=1, max_length=32)


class PerformanceEditRequest(StrictModel):
    expected_version: int = Field(ge=1)
    items: list[PerformanceItem] = Field(min_length=1, max_length=100)
    unlock_locked: bool = False


class PerformanceAdoptRequest(StrictModel):
    expected_version: int = Field(ge=1)
    accept_unanalysed: bool = False
    enable: bool = True


class PerformanceLeaseRequest(StrictModel):
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class PerformanceRenewRequest(PerformanceLeaseRequest):
    lease_token: str = Field(min_length=16, max_length=64)


class PerformanceSubmitRequest(PerformanceResult):
    lease_token: str = Field(min_length=16, max_length=64)


class PerformancePreviewRequest(StrictModel):
    segment_id: str = Field(min_length=1, max_length=80)
    annotation: PerformanceAnnotation | None = None
    speech_xml: str | None = Field(default=None, max_length=256 * 1024)
    # Only public per-request choices, never URLs or credentials.
    service: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=255)
    generation_prompt: str | None = Field(default=None, max_length=4000)
    context_mode: Literal["off", "before", "both"] | None = None
    context_before: int | None = Field(default=None, ge=0, le=20)
    context_after: int | None = Field(default=None, ge=0, le=20)
    context_max_chars: int | None = Field(default=None, ge=0, le=16000)
    casting_enabled: bool | None = None
    performance_enabled: bool | None = None
    allow_vocalizations: bool | None = None

    @model_validator(mode="after")
    def at_most_one_format(self):
        if self.annotation is not None and self.speech_xml is not None:
            raise ValueError("Supply annotation or speech_xml, not both.")
        return self
