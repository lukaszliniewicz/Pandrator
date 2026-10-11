"""Typed reads for generation operation lifecycle and selected-run coverage."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ToolInput


class GetGenerationOperationInput(ToolInput):
    operation_id: str = Field(min_length=1, max_length=120)
    wait_seconds: int = Field(default=0, ge=0, le=60)
    include_coverage: bool = True


class GetGenerationRunCoverageInput(ToolInput):
    generation_run_id: str = Field(min_length=1, max_length=80)
    audio_version_side: Literal["before", "after"] = "after"


__all__ = ["GetGenerationOperationInput", "GetGenerationRunCoverageInput"]
