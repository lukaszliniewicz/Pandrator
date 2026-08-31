"""Strict MCP arguments for passive PDF/EPUB source cleaning."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .common import ToolInput

_SAFE_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"


class CreateSourceCleaningDispatchRunInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    source_artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=16_000)
    evidence_limit: int = Field(
        default=500,
        ge=20,
        le=2_000,
        description=(
            "Per-phase evidence transport bound. This is not a model token or iteration budget."
        ),
    )
    remove_footnotes: bool | None = None
    filter_citations: bool | None = None
    pdf_ocr_mode: Literal["auto", "off", "force"] | None = None
    pdf_ocr_language: str | None = Field(default=None, min_length=2, max_length=80)
    pdf_ocr_dpi: int | None = Field(default=None, ge=120, le=400)
    pdf_remove_toc: bool | None = None
    pdf_remove_repeated_marginals: bool | None = None
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ListSourceCleaningDispatchRunsInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=50, ge=1, le=100)


class GetSourceCleaningDispatchRunInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)


class ClaimSourceCleaningDispatchBatchInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=120)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class RenewSourceCleaningDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    lease_seconds: int = Field(default=900, ge=30, le=3_600)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class ReleaseSourceCleaningDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class InspectSourceCleaningDispatchExtractionInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    action: Literal[
        "batch",
        "inspect_document_structure",
        "inspect_navigation",
        "search",
        "regex_search",
        "preview",
        "inspect_block",
        "get_epub_markup_for_text",
        "preview_raw_markup_range",
        "list_epub_selectors",
        "preview_selector",
        "list_repeated_lines",
        "find_heading_candidates",
        "analyze_chapter_structure",
        "analyze_cleanup_structure",
        "find_footnote_candidates",
        "find_metadata_candidates",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=100)
    view: Literal["working", "baseline", "source"] = "working"
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )


class SourceCleaningDispatchDecisionInput(ToolInput):
    operation_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["accept", "reject"]


class SourceCleaningDispatchOperationInput(ToolInput):
    op: Literal[
        "set_metadata",
        "delete_blocks",
        "mark_chapter",
        "unmark_chapter",
        "replace_block",
    ]
    metadata: dict[
        Annotated[str, Field(min_length=1, max_length=40)],
        Annotated[str, Field(min_length=1, max_length=2_000)],
    ] = Field(default_factory=dict, max_length=20)
    block_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=2_000,
    )
    block_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=2_000)
    replacement: str = Field(default="", max_length=50_000)
    reason: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def validate_operation_shape(self):
        if self.op == "set_metadata":
            valid = (
                bool(self.metadata)
                and not self.block_ids
                and self.block_id is None
                and not self.replacement
            )
        elif self.op == "delete_blocks":
            valid = (
                bool(self.block_ids)
                and not self.metadata
                and self.block_id is None
                and not self.replacement
            )
        elif self.op == "mark_chapter":
            valid = (
                self.block_id is not None
                and not self.metadata
                and not self.block_ids
                and not self.replacement
            )
        elif self.op == "unmark_chapter":
            valid = (
                self.block_id is not None
                and not self.metadata
                and not self.block_ids
                and self.title is None
                and not self.replacement
            )
        else:
            valid = (
                self.block_id is not None
                and bool(self.replacement.strip())
                and not self.metadata
                and not self.block_ids
                and self.title is None
            )
        if not valid:
            raise ValueError("Source-cleaning operation fields do not match its op.")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("delete_blocks block_ids must be unique.")
        return self


class SourceCleaningDispatchResultInput(ToolInput):
    kind: Literal["source_cleaning"] = "source_cleaning"
    phase: Literal[
        "metadata",
        "navigation",
        "boilerplate",
        "repeated_elements",
        "chapter_marking",
        "text_repair",
    ]
    decisions: list[SourceCleaningDispatchDecisionInput] = Field(
        default_factory=list,
        max_length=5_000,
    )
    operations: list[SourceCleaningDispatchOperationInput] = Field(
        default_factory=list,
        max_length=2_000,
    )
    summary: str = Field(default="", max_length=8_000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SubmitSourceCleaningDispatchBatchInput(ToolInput):
    batch_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1, max_length=160)
    result: SourceCleaningDispatchResultInput
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=_SAFE_KEY,
    )
