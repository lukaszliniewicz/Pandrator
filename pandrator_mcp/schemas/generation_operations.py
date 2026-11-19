"""Typed reads for generation operation lifecycle and selected-run coverage."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..waits import WAIT_DESCRIPTION
from .common import ToolInput


class GetGenerationOperationInput(ToolInput):
    operation_id: str | None = Field(default=None, min_length=1, max_length=120)
    generation_operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="Same as operation_id, under the name start and regenerate results use.",
    )
    wait_seconds: int = Field(default=0, ge=0, le=60, description=WAIT_DESCRIPTION)
    include_coverage: bool = True

    @model_validator(mode="after")
    def _one_operation_id(self) -> GetGenerationOperationInput:
        if (
            self.operation_id
            and self.generation_operation_id
            and self.operation_id != self.generation_operation_id
        ):
            raise ValueError("operation_id and generation_operation_id name different operations.")
        if not (self.operation_id or self.generation_operation_id):
            raise ValueError("Pass operation_id (or generation_operation_id).")
        return self

    @property
    def resolved_operation_id(self) -> str:
        return self.operation_id or self.generation_operation_id or ""


class GetGenerationRunCoverageInput(ToolInput):
    generation_run_id: str = Field(min_length=1, max_length=80)
    audio_version_side: Literal["before", "after"] = "after"


__all__ = ["GetGenerationOperationInput", "GetGenerationRunCoverageInput"]
