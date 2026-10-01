"""Durable subtitle run lifecycle controls."""

from typing import Literal

from pydantic import Field, model_validator

from .common import ToolInput


class TerminateDispatchRunInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)
    expected_status: str = Field(min_length=1, max_length=40)
    action: Literal["cancelled", "superseded"]
    replacement_run_id: str | None = Field(default=None, min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")

    @model_validator(mode="after")
    def check_replacement(self):
        if (self.action == "superseded") != bool(self.replacement_run_id):
            raise ValueError("Only supersession requires replacement_run_id.")
        return self


class GetDispatchPreviewInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)
    batch_ordinal: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
