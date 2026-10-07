"""Compact, revision-fenced requests for casting and audio-version actions."""

from pydantic import Field

from .schemas import StrictModel


class GenerationRecastPreviewRequest(StrictModel):
    speech_plan_revision_id: str | None = Field(default=None, min_length=1, max_length=80)
    selector_keys: list[str] | None = Field(default=None, max_length=1000)
    force: bool = False
    include_missing: bool = False


class GenerationRecastStartRequest(GenerationRecastPreviewRequest):
    expected_selection_hash: str = Field(min_length=1, max_length=128)


class AudioVersionUndoRequest(StrictModel):
    expected_selection_hash: str = Field(min_length=1, max_length=128)
