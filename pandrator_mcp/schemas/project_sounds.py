"""Strict MCP contracts for project sound-plan operations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from .common import ToolInput

_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_IdempotencyKey = Annotated[
    StrictStr,
    Field(min_length=8, max_length=200, pattern=_IDEMPOTENCY_PATTERN),
]


class _StrictInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _BeforeSegmentAnchor(_StrictInput):
    kind: Literal["before_segment"]
    segment_id: StrictStr = Field(min_length=1, max_length=36)


class _AfterSegmentAnchor(_StrictInput):
    kind: Literal["after_segment"]
    segment_id: StrictStr = Field(min_length=1, max_length=36)


class _SourceTimeAnchor(_StrictInput):
    kind: Literal["source_time"]
    time_seconds: StrictFloat = Field(ge=0.0)


class _QuoteAnchor(_StrictInput):
    kind: Literal["after_word", "next_pause"]
    segment_id: StrictStr = Field(min_length=1, max_length=36)
    quote: StrictStr = Field(min_length=1, max_length=500)
    occurrence: StrictInt = Field(ge=1)
    start: StrictInt | None = Field(default=None, ge=0)
    end: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_codepoint_range(self) -> _QuoteAnchor:
        if ("start" in self.model_fields_set) != ("end" in self.model_fields_set):
            raise ValueError("start and end codepoints must be supplied together.")
        if not self.quote.strip():
            raise ValueError("quote may not be blank.")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must precede end.")
        return self


SoundAnchorInput = Annotated[
    _BeforeSegmentAnchor | _AfterSegmentAnchor | _SourceTimeAnchor | _QuoteAnchor,
    Field(discriminator="kind"),
]


class SoundPlaybackInput(_StrictInput):
    mode: Literal["insert", "overlay"] | None = None
    offset_ms: StrictInt = Field(default=0, ge=-10000, le=10000)
    gain_db: StrictFloat = Field(default=0.0, ge=-60.0, le=12.0)
    trim_start_seconds: StrictFloat = Field(default=0.0, ge=0.0)
    trim_end_seconds: StrictFloat | None = None
    fade_in_ms: StrictInt = Field(default=5, ge=0, le=1000)
    fade_out_ms: StrictInt = Field(default=5, ge=0, le=1000)

    @model_validator(mode="after")
    def validate_trim(self) -> SoundPlaybackInput:
        if self.trim_end_seconds is not None and self.trim_end_seconds <= self.trim_start_seconds:
            raise ValueError("trim_end_seconds must be greater than trim_start_seconds.")
        if self.mode == "insert" and self.offset_ms < 0:
            raise ValueError("insert offset_ms must be nonnegative.")
        return self


class ProjectSoundsInput(_StrictInput):
    action: Literal["get", "status", "add", "update", "remove", "restore", "render", "resolve"]
    session_id: StrictStr = Field(min_length=1, max_length=80)
    segment_ids: list[Annotated[StrictStr, Field(min_length=1, max_length=36)]] | None = Field(
        default=None, max_length=100
    )
    cursor: StrictStr | None = Field(default=None, max_length=36)
    limit: StrictInt = Field(default=40, ge=1, le=100)

    logical_cue_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)
    expected_revision: StrictInt | None = Field(default=None, ge=0)
    variant_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)
    anchor: SoundAnchorInput | None = None
    playback: SoundPlaybackInput | None = None
    included: StrictBool | None = None
    source_plan_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)
    base_artifact_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    timeline_artifact_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    cue_ids: list[Annotated[StrictStr, Field(min_length=1, max_length=36)]] | None = Field(
        default=None, min_length=1, max_length=20
    )
    idempotency_key: _IdempotencyKey | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> ProjectSoundsInput:
        action_fields = {
            "get": {"session_id", "segment_ids", "cursor", "limit"},
            "status": {"session_id", "base_artifact_id"},
            "add": {
                "session_id",
                "expected_revision",
                "variant_id",
                "anchor",
                "playback",
                "included",
                "idempotency_key",
            },
            "update": {
                "session_id",
                "logical_cue_id",
                "expected_revision",
                "variant_id",
                "anchor",
                "playback",
                "included",
                "idempotency_key",
            },
            "remove": {"session_id", "logical_cue_id", "expected_revision", "idempotency_key"},
            "restore": {"session_id", "expected_revision", "source_plan_id", "idempotency_key"},
            "render": {
                "session_id",
                "expected_revision",
                "base_artifact_id",
                "timeline_artifact_id",
                "idempotency_key",
            },
            "resolve": {"session_id", "expected_revision", "cue_ids", "idempotency_key"},
        }
        supplied = self.model_fields_set - {"action"}
        unsupported = supplied - action_fields[self.action]
        if unsupported:
            raise ValueError(
                f"Fields {sorted(unsupported)} are not valid for project-sounds action {self.action}."
            )

        if not self.session_id.strip():
            raise ValueError("session_id may not be blank.")
        if self.action == "add":
            self._require("expected_revision", "variant_id", "anchor", "idempotency_key")
        elif self.action == "update":
            self._require("logical_cue_id", "expected_revision", "idempotency_key")
            if self.expected_revision is None or self.expected_revision < 1:
                raise ValueError("Updating a cue requires expected_revision >= 1.")
            if not (supplied & {"variant_id", "anchor", "playback", "included"}):
                raise ValueError("Updating a cue requires at least one cue change.")
            if any(
                getattr(self, field) is None
                for field in supplied & {"variant_id", "anchor", "playback", "included"}
            ):
                raise ValueError("Cue changes must be non-null when provided.")
        elif self.action == "remove":
            if self.expected_revision is None or self.expected_revision < 1:
                raise ValueError("Removing a cue requires expected_revision >= 1.")
            self._require("logical_cue_id", "idempotency_key")
        elif self.action == "restore":
            self._require("expected_revision", "source_plan_id", "idempotency_key")
        elif self.action == "render":
            self._require("expected_revision", "base_artifact_id", "idempotency_key")
        elif self.action == "resolve":
            self._require("expected_revision", "idempotency_key")
            if self.expected_revision is None or self.expected_revision < 1:
                raise ValueError("Resolving a sound plan requires expected_revision >= 1.")
        return self

    def _require(self, *fields: str) -> None:
        missing = [field for field in fields if getattr(self, field) is None]
        if missing:
            raise ValueError(f"Project-sounds action {self.action} requires {', '.join(missing)}.")


__all__ = ["ProjectSoundsInput", "SoundAnchorInput", "SoundPlaybackInput"]
