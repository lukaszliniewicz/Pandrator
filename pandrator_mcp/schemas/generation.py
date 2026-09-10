"""Input schemas for fine-grained generation segments, takes, and assembly."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from .common import ToolInput


class ListGenerationSegmentsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    generation_run_id: str | None = Field(default=None, max_length=80)
    plan_revision_id: str | None = Field(default=None, max_length=80)
    view: Literal["full", "compact", "provenance"] = "full"
    fields: list[str] | None = Field(default=None, min_length=1, max_length=40)
    end_ordinal: int | None = Field(default=None, ge=0)
    around_ordinal: int | None = Field(default=None, ge=0)
    source_cue_id: str | None = Field(default=None, min_length=1, max_length=80)
    radius: int = Field(default=2, ge=0, le=25)


class UpdateGenerationSegmentInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    segment_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    optimized_text: str | None = Field(default=None, max_length=2000)
    voice_id: str | None = Field(default=None, max_length=100)
    voice: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=120)


class SelectTakeInput(ToolInput):
    segment_id: str = Field(min_length=1, max_length=80)
    take_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=120)


class RegenerateSegmentsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    segment_ids: list[str] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=120)


class AssembleGenerationRunInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    generation_run_id: str | None = Field(default=None, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ReviseSpeechBlockPlanInput(ToolInput):
    """Strict typed immutable split, merge, or restore operation."""

    session_id: str = Field(min_length=1, max_length=80)
    expected_revision_id: str = Field(min_length=1, max_length=80)
    action: Literal["split", "merge", "restore"]
    segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    cursor: StrictInt | None = None
    text_layer: Literal["display", "speech"] | None = None
    left_segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    right_segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_revision_id: str | None = Field(default=None, min_length=1, max_length=80)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )

    @model_validator(mode="after")
    def validate_action_shape(self) -> "ReviseSpeechBlockPlanInput":
        values = {
            "segment_id": self.segment_id,
            "cursor": self.cursor,
            "text_layer": self.text_layer,
            "left_segment_id": self.left_segment_id,
            "right_segment_id": self.right_segment_id,
            "target_revision_id": self.target_revision_id,
        }
        required = {
            "split": {"segment_id", "cursor", "text_layer"},
            "merge": {"left_segment_id", "right_segment_id"},
            "restore": {"target_revision_id"},
        }[self.action]
        missing = sorted(key for key in required if values[key] is None)
        forbidden = sorted(
            key for key, value in values.items() if key not in required and value is not None
        )
        if missing or forbidden:
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if forbidden:
                detail.append(f"forbidden {', '.join(forbidden)}")
            raise ValueError(f"Invalid {self.action} topology operation ({'; '.join(detail)}).")
        return self


# Kept independent of the application package for standalone MCP deployments.
class GenerationSegmentSelector(ToolInput):
    segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_cue_ids: list[str | StrictInt] | None = Field(default=None, min_length=1, max_length=1000)
    ordinal: StrictInt | None = Field(default=None, ge=0)
    result_ref: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> GenerationSegmentSelector:
        if sum(value is not None for value in (self.segment_id, self.source_cue_ids, self.ordinal, self.result_ref)) != 1:
            raise ValueError("Choose exactly one segment selector.")
        return self


class GenerationSplitBoundary(ToolInput):
    cursor: StrictInt | None = Field(default=None, ge=1)
    before_text: str | None = Field(default=None, min_length=1, max_length=8000)
    after_text: str | None = Field(default=None, min_length=1, max_length=8000)
    before_source_cue_id: str | StrictInt | None = None
    after_source_cue_id: str | StrictInt | None = None
    after_sentence: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exactly_one_boundary(self) -> GenerationSplitBoundary:
        if sum(value is not None for value in self.model_dump().values()) != 1:
            raise ValueError("Choose exactly one split boundary.")
        return self


class GenerationTopologyEdit(ToolInput):
    action: Literal["split", "merge"]
    label: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
    segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    segment: GenerationSegmentSelector | None = None
    cursor: StrictInt | None = Field(default=None, ge=1)
    boundary: GenerationSplitBoundary | None = None
    text_layer: Literal["display", "speech"] = "display"
    left_segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    right_segment_id: str | None = Field(default=None, min_length=1, max_length=80)
    left: GenerationSegmentSelector | None = None
    right: GenerationSegmentSelector | None = None

    @model_validator(mode="after")
    def validate_edit_shape(self) -> GenerationTopologyEdit:
        if self.action == "split":
            if (self.segment_id is None) == (self.segment is None) or (self.cursor is None) == (self.boundary is None):
                raise ValueError("A split needs exactly one segment selector and one boundary selector.")
            if any(value is not None for value in (self.left_segment_id, self.right_segment_id, self.left, self.right)):
                raise ValueError("A split cannot contain merge selectors.")
        else:
            if (self.left_segment_id is None) == (self.left is None) or (self.right_segment_id is None) == (self.right is None):
                raise ValueError("A merge needs exactly one left selector and one right selector.")
            if any(value is not None for value in (self.segment_id, self.segment, self.cursor, self.boundary)) or self.text_layer != "display":
                raise ValueError("A merge cannot contain split selectors.")
        return self


class GenerationPlanBatchRequest(ToolInput):
    expected_revision_id: str = Field(min_length=1, max_length=80)
    operations: list[GenerationTopologyEdit] = Field(min_length=1, max_length=50)


class ReviseSpeechBlockPlanBatchInput(GenerationPlanBatchRequest):
    session_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


class ListSpeechPlanRevisionsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=50, ge=1, le=100)
    before_revision_number: int | None = Field(default=None, ge=1)


class GenerateSpeechPlanInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    speech_plan_revision_id: str = Field(min_length=1, max_length=80)
    stale_only: bool = False
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


class AdoptSubtitleSourceInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    source_asset_id: str = Field(min_length=1, max_length=80)
    expected_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
