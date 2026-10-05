"""Source-cleaning dispatch registrations borrow the adapter's validation guard."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..native_text import NativeNullableString
from ..schemas.source_cleaning_dispatch import (
    ClaimSourceCleaningDispatchBatchInput,
    CreateSourceCleaningDispatchRunInput,
    GetSourceCleaningDispatchRunInput,
    InspectSourceCleaningDispatchExtractionInput,
    ListSourceCleaningDispatchRunsInput,
    ReleaseSourceCleaningDispatchBatchInput,
    RenewSourceCleaningDispatchBatchInput,
    SourceCleaningDispatchResultInput,
    SubmitSourceCleaningDispatchBatchInput,
)
from ..tools.source_cleaning_dispatch import (
    claim_source_cleaning_dispatch_batch,
    create_source_cleaning_dispatch_run,
    get_source_cleaning_dispatch_run,
    inspect_source_cleaning_dispatch_extraction,
    list_source_cleaning_dispatch_runs,
    release_source_cleaning_dispatch_batch,
    renew_source_cleaning_dispatch_batch,
    submit_source_cleaning_dispatch_batch,
)


def register_source_cleaning_dispatch_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register source-cleaning dispatch tools at their existing inventory position."""

    @server.tool(
        name="pandrator_create_source_cleaning_dispatch_run",
        title="Create a passive PDF/EPUB cleanup run",
        annotations=write_action,
    )
    def source_cleaning_dispatch_create_tool(
        session_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        source_artifact_id: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=80),
        ] = None,
        instructions: Annotated[str, Field(max_length=16_000)] = "",
        evidence_limit: Annotated[int, Field(ge=20, le=2_000)] = 500,
        remove_footnotes: bool | None = None,
        filter_citations: bool | None = None,
        pdf_ocr_mode: Literal["auto", "off", "force"] | None = None,
        pdf_ocr_language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=80),
        ] = None,
        pdf_ocr_dpi: Annotated[int | None, Field(ge=120, le=400)] = None,
        pdf_remove_toc: bool | None = None,
        pdf_remove_repeated_marginals: bool | None = None,
    ) -> dict[str, Any]:
        """Queue deterministic preparation; no model provider or token budget is used."""

        return _call_with_validated_input(
            create_source_cleaning_dispatch_run,
            runtime,
            CreateSourceCleaningDispatchRunInput,
            {
                "session_id": session_id,
                "source_artifact_id": source_artifact_id,
                "instructions": instructions,
                "evidence_limit": evidence_limit,
                "remove_footnotes": remove_footnotes,
                "filter_citations": filter_citations,
                "pdf_ocr_mode": pdf_ocr_mode,
                "pdf_ocr_language": pdf_ocr_language,
                "pdf_ocr_dpi": pdf_ocr_dpi,
                "pdf_remove_toc": pdf_remove_toc,
                "pdf_remove_repeated_marginals": pdf_remove_repeated_marginals,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_list_source_cleaning_dispatch_runs",
        title="List passive PDF/EPUB cleanup runs",
        annotations=read_only,
    )
    def source_cleaning_dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List run metadata without exposing book text or phase evidence."""

        return _call_with_validated_input(
            list_source_cleaning_dispatch_runs,
            runtime,
            ListSourceCleaningDispatchRunsInput,
            {
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        name="pandrator_get_source_cleaning_dispatch_run",
        title="Inspect a passive PDF/EPUB cleanup run",
        annotations=read_only,
    )
    def source_cleaning_dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect preparation, progress, validation, and final artifact metadata."""

        return _call_with_validated_input(
            get_source_cleaning_dispatch_run,
            runtime,
            GetSourceCleaningDispatchRunInput,
            {
                "run_id": run_id,
            },
        )

    @server.tool(
        name="pandrator_claim_source_cleaning_dispatch_batch",
        title="Claim a PDF/EPUB cleanup phase",
        annotations=write_action,
    )
    def source_cleaning_dispatch_claim_tool(
        run_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Claim one rich editorial packet with bounded book evidence."""

        return _call_with_validated_input(
            claim_source_cleaning_dispatch_batch,
            runtime,
            ClaimSourceCleaningDispatchBatchInput,
            {
                "run_id": run_id,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_renew_source_cleaning_dispatch_batch",
        title="Renew a PDF/EPUB cleanup lease",
        annotations=write_action,
    )
    def source_cleaning_dispatch_renew_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Renew only the matching editorial phase lease."""

        return _call_with_validated_input(
            renew_source_cleaning_dispatch_batch,
            runtime,
            RenewSourceCleaningDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_release_source_cleaning_dispatch_batch",
        title="Release a PDF/EPUB cleanup lease",
        annotations=write_action,
    )
    def source_cleaning_dispatch_release_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Return an unfinished editorial phase to the ready queue."""

        return _call_with_validated_input(
            release_source_cleaning_dispatch_batch,
            runtime,
            ReleaseSourceCleaningDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_inspect_source_cleaning_dispatch_extraction",
        title="Inspect a leased PDF/EPUB extraction",
        annotations=write_action,
    )
    def source_cleaning_dispatch_inspect_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
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
        ],
        arguments: Annotated[dict[str, Any], Field(max_length=100)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        view: Literal["working", "baseline", "source"] = "working",
    ) -> dict[str, Any]:
        """Browse/search the full pinned extraction and authorize returned blocks."""

        return _call_with_validated_input(
            inspect_source_cleaning_dispatch_extraction,
            runtime,
            InspectSourceCleaningDispatchExtractionInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "action": action,
                "arguments": arguments,
                "view": view,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_submit_source_cleaning_dispatch_batch",
        title="Submit a PDF/EPUB cleanup phase",
        annotations=write_action,
    )
    def source_cleaning_dispatch_submit_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        result: SourceCleaningDispatchResultInput,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Submit typed proposal decisions and optional phase-scoped operations."""

        return _call_with_validated_input(
            submit_source_cleaning_dispatch_batch,
            runtime,
            SubmitSourceCleaningDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "result": result,
                "idempotency_key": idempotency_key,
            },
        )
