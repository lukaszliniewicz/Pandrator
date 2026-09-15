"""Strict request schemas for revision-safe repair undo."""

from pydantic import BaseModel, ConfigDict, Field


class RepairBatchUndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision_id: str = Field(min_length=1, max_length=128)
    expected_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
