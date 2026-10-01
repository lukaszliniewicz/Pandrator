"""Strict MCP arguments for passive subtitle dispatch runs and batches."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .common import ToolInput
from .delegation import (
    DelegationContextDeltaInput,
    DelegationExecutionMixin,
)

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
_SESSION_ID = Field(min_length=1, max_length=80)
_RUN_ID = Field(min_length=1, max_length=120)
_BATCH_ID = Field(min_length=1, max_length=120)


class CreateDispatchRunInput(DelegationExecutionMixin):
    """Create one server-side correction or translation dispatch run."""

    session_id: str = _SESSION_ID
    kind: Literal["correction", "translation"]
    source_artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=40,
    )
    target_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=40,
    )
    instructions: str = Field(default="", max_length=16_000)
    char_limit: int = Field(default=6_000, ge=1, le=100_000)
    max_segments_per_batch: int = Field(default=40, ge=1, le=500)
    no_remove_subtitles: bool = False
    correction_style: Literal["publishable", "faithful"] = "publishable"
    context_before: int = Field(default=8, ge=0, le=20)
    context_after: int = Field(default=2, ge=0, le=20)
    timing_context_mode: Literal["full", "overlap_only", "none"] = "full"
    substantial_gap_ms: int = Field(default=2_000, ge=0, le=60_000)
    glossary: dict[str, str] = Field(default_factory=dict, max_length=2_000)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @field_validator("glossary")
    @classmethod
    def validate_glossary(cls, value: dict[str, str]) -> dict[str, str]:
        for key, replacement in value.items():
            if not key.strip() or len(key) > 500:
                raise ValueError("Glossary terms must be 1-500 characters.")
            if not replacement.strip() or len(replacement) > 2_000:
                raise ValueError("Glossary replacements must be 1-2,000 characters.")
        return value


class ListDispatchRunsInput(ToolInput):
    """List metadata for dispatch runs belonging to one session."""

    session_id: str = _SESSION_ID
    limit: int = Field(default=50, ge=1, le=100)


class GetDispatchRunInput(ToolInput):
    """Inspect dispatch-run metadata without receiving source batch content."""

    run_id: str = _RUN_ID


class ClaimDispatchBatchInput(ToolInput):
    """Claim one available batch and receive its canonical scoped task packet."""

    run_id: str = _RUN_ID
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    packet_format: Literal["standard", "compact"] = "standard"
    known_manifest_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class InspectDispatchSplitBoundariesInput(ToolInput):
    batch_id: str = _BATCH_ID
    lease_token: str = Field(min_length=1, max_length=160)
    cue_id: int = Field(ge=1, strict=True)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=100)


class RenewDispatchBatchInput(ToolInput):
    """Renew a lease only for its matching claimed batch."""

    batch_id: str = _BATCH_ID
    lease_token: str = Field(min_length=1, max_length=160)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ReleaseDispatchBatchInput(ToolInput):
    """Release a claimed batch using its matching short-lived lease."""

    batch_id: str = _BATCH_ID
    lease_token: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class DispatchCorrectionOperationInput(ToolInput):
    split_boundary_ids: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=499
    )
    starts_new_turn: bool = Field(default=False, strict=True)
    action: Literal["edit", "delete", "merge", "split"]
    cue_ids: list[Annotated[int, Field(ge=1)]] = Field(
        min_length=1,
        max_length=500,
    )
    texts: list[Annotated[str, Field(min_length=1, max_length=16_000)]] = Field(
        default_factory=list,
        max_length=500,
    )
    speakers: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "DispatchCorrectionOperationInput":
        if self.split_boundary_ids and (
            self.action != "split" or len(self.split_boundary_ids) != len(self.texts) - 1
        ):
            raise ValueError("split_boundary_ids must match the boundaries between split texts.")
        if self.starts_new_turn and self.action not in {"edit", "split"}:
            raise ValueError("starts_new_turn requires edit or split.")
        ids = self.cue_ids
        texts = self.texts
        valid = (
            (self.action == "edit" and len(ids) == 1 and len(texts) == 1)
            or (self.action == "delete" and not texts)
            or (self.action == "merge" and len(ids) >= 2 and bool(texts))
            or (self.action == "split" and len(ids) == 1 and len(texts) >= 2)
        )
        if not valid:
            raise ValueError("Correction operation fields do not match its action.")
        if len(set(ids)) != len(ids):
            raise ValueError("Correction operation cue_ids must be unique.")
        if self.speakers and len(self.speakers) != len(texts):
            raise ValueError("speakers must be empty or match texts one-for-one.")
        return self


class DispatchCorrectionUncertaintyInput(ToolInput):
    cue_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4_000)
    evidence_ids: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list,
        max_length=20,
    )

    @field_validator("evidence_ids")
    @classmethod
    def validate_unique_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_ids must be unique.")
        return value


class DispatchCompactCorrectionEditInput(ToolInput):
    """One cue edit in the grouped correction submission form."""

    cue_id: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=16_000)
    speaker: str | None = Field(default=None, max_length=500)
    starts_new_turn: bool = Field(default=False, strict=True)


class DispatchCompactCorrectionMergeInput(ToolInput):
    """One explicit merge operation in a grouped correction submission."""

    cue_ids: list[Annotated[int, Field(ge=1, strict=True)]] = Field(
        min_length=2,
        max_length=500,
    )
    texts: list[Annotated[str, Field(min_length=1, max_length=16_000)]] = Field(
        min_length=1,
        max_length=500,
    )
    speakers: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_merge_shape(self) -> "DispatchCompactCorrectionMergeInput":
        if len(set(self.cue_ids)) != len(self.cue_ids):
            raise ValueError("Correction operation cue_ids must be unique.")
        if self.speakers and len(self.speakers) != len(self.texts):
            raise ValueError("speakers must be empty or match texts one-for-one.")
        return self


class DispatchCompactCorrectionSplitInput(ToolInput):
    """One explicit split operation in a grouped correction submission."""

    cue_id: int = Field(ge=1, strict=True)
    texts: list[Annotated[str, Field(min_length=1, max_length=16_000)]] = Field(
        min_length=2,
        max_length=500,
    )
    speakers: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=500,
    )
    split_boundary_ids: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=499
    )
    starts_new_turn: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_split_shape(self) -> "DispatchCompactCorrectionSplitInput":
        if self.split_boundary_ids and len(self.split_boundary_ids) != len(self.texts) - 1:
            raise ValueError("split_boundary_ids must match the boundaries between split texts.")
        if self.speakers and len(self.speakers) != len(self.texts):
            raise ValueError("speakers must be empty or match texts one-for-one.")
        return self


class DispatchCorrectionResultInput(ToolInput):
    kind: Literal["correction"]
    operations: list[DispatchCorrectionOperationInput] = Field(
        default_factory=list,
        max_length=500,
    )
    uncertainties: list[DispatchCorrectionUncertaintyInput] = Field(
        default_factory=list,
        max_length=500,
    )
    edits: list[DispatchCompactCorrectionEditInput] = Field(
        default_factory=list,
        max_length=500,
    )
    deletes: list[Annotated[int, Field(ge=1, strict=True)]] = Field(
        default_factory=list,
        max_length=500,
    )
    merges: list[DispatchCompactCorrectionMergeInput] = Field(
        default_factory=list,
        max_length=500,
    )
    splits: list[DispatchCompactCorrectionSplitInput] = Field(
        default_factory=list,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_submission_shape(self) -> "DispatchCorrectionResultInput":
        grouped_fields = {"edits", "deletes", "merges", "splits"}
        if (
            grouped_fields.intersection(self.model_fields_set)
            and "operations" in self.model_fields_set
        ):
            raise ValueError("Provide grouped correction fields or operations, not both.")
        if len(set(self.deletes)) != len(self.deletes):
            raise ValueError("Delete cue IDs must be unique.")
        return self


class DispatchCompactTranslationItemInput(ToolInput):
    """One cue translation in the compact one-cue-per-item form."""

    cue_id: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=16_000)
    speaker: str | None = Field(default=None, max_length=500)


class DispatchTranslationItemInput(ToolInput):
    cue_id: int | None = Field(default=None, ge=1, strict=True)
    cue_ids: list[Annotated[int, Field(ge=1, strict=True)]] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    text: str = Field(min_length=1, max_length=16_000)
    speaker: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_passage_identity(self):
        if (self.cue_id is None) == (self.cue_ids is None):
            raise ValueError("Provide cue_id for one passage or cue_ids for an adjacent merge.")
        return self


class DispatchTranslationResultInput(ToolInput):
    kind: Literal["translation"]
    translations: list[DispatchTranslationItemInput] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    items: list[DispatchCompactTranslationItemInput] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    glossary_updates: dict[str, str] = Field(default_factory=dict, max_length=2_000)

    @model_validator(mode="after")
    def validate_submission_shape(self) -> "DispatchTranslationResultInput":
        if (self.translations is None) == (self.items is None):
            raise ValueError("Provide exactly one of translations or compact items.")
        return self

    @field_validator("glossary_updates")
    @classmethod
    def validate_glossary_updates(cls, value: dict[str, str]) -> dict[str, str]:
        for term, replacement in value.items():
            if not term.strip() or len(term) > 500:
                raise ValueError("Glossary terms must be 1-500 characters.")
            if not replacement.strip() or len(replacement) > 2_000:
                raise ValueError("Glossary replacements must be 1-2,000 characters.")
        return value


DispatchStructuredResultInput = Annotated[
    DispatchCorrectionResultInput | DispatchTranslationResultInput,
    Field(discriminator="kind"),
]


class SubmitDispatchBatchInput(ToolInput):
    """Submit one typed result, or a legacy raw model response."""

    batch_id: str = _BATCH_ID
    lease_token: str = Field(min_length=1, max_length=160)
    result: DispatchStructuredResultInput | None = None
    response_text: str | None = Field(default=None, max_length=524_288)
    context_delta: DelegationContextDeltaInput = Field(default_factory=DelegationContextDeltaInput)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )

    @model_validator(mode="after")
    def validate_exactly_one_result(self):
        if (self.result is None) == (self.response_text is None):
            raise ValueError("Provide exactly one of result or response_text.")
        return self

    @field_validator("response_text")
    @classmethod
    def validate_response_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 512 * 1024:
            raise ValueError("Model response exceeds the 512 KiB limit.")
        return value
