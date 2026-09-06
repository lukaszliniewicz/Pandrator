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
