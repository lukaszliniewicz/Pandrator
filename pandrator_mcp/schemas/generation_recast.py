"""Portable, revision-fenced casting and audio-version requests."""

from pydantic import Field

from .common import ToolInput


class PreviewRecastInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    selector_keys: list[str] | None = Field(default=None, max_length=1000)
    speech_plan_revision_id: str | None = Field(default=None, min_length=1, max_length=80)
    force: bool = False
    include_missing: bool = False


class RecastVoicesInput(PreviewRecastInput):
    expected_selection_hash: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
    )


class PreviewUndoRecastInput(ToolInput):
    generation_run_id: str = Field(min_length=1, max_length=80)


class UndoRecastInput(PreviewUndoRecastInput):
    expected_selection_hash: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
    )


GENERATION_RECAST_INPUT_MODELS = (
    PreviewRecastInput,
    RecastVoicesInput,
    PreviewUndoRecastInput,
    UndoRecastInput,
)
