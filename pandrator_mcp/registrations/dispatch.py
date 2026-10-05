"""Subtitle-dispatch MCP registrations borrow the adapter's guarded callbacks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..native_text import NativeNullableString, NativeResponseText
from ..schemas.delegation import DelegationContextCapsuleInput, DelegationContextDeltaInput
from ..schemas.dispatch import (
    ClaimDispatchBatchInput,
    CreateDispatchRunInput,
    DispatchStructuredResultInput,
    GetDispatchRunInput,
    InspectDispatchSplitBoundariesInput,
    ListDispatchRunsInput,
    ReleaseDispatchBatchInput,
    RenewDispatchBatchInput,
    SubmitDispatchBatchInput,
)
from ..schemas.workflow_controls import GetDispatchPreviewInput, TerminateDispatchRunInput
from ..tools.dispatch import (
    claim_dispatch_batch,
    create_dispatch_run,
    get_dispatch_run,
    inspect_dispatch_split_boundaries,
    list_dispatch_runs,
    release_dispatch_batch,
    renew_dispatch_batch,
    submit_dispatch_batch,
)
from ..tools.workflow_controls import get_dispatch_preview, terminate_dispatch_run


def register_dispatch_lifecycle_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register subtitle preview and termination at their existing inventory position."""

    @server.tool(
        name="pandrator_get_dispatch_preview",
        title="Preview an accepted subtitle batch",
        annotations=read_only,
    )
    def dispatch_preview_tool(
        run_id: str,
        batch_ordinal: Annotated[int | None, Field(ge=1)] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Inspect bounded accepted output before publication; never guesses source pairing."""
        return _call_with_validated_input(
            get_dispatch_preview,
            runtime,
            GetDispatchPreviewInput,
            {
                key: value
                for key, value in locals().items()
                if key in GetDispatchPreviewInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_terminate_dispatch_run",
        title="Cancel or supersede a passive subtitle run",
        annotations=write_action,
    )
    def dispatch_terminate_tool(
        run_id: str,
        expected_status: str,
        action: Literal["cancelled", "superseded"],
        reason: str,
        idempotency_key: str,
        replacement_run_id: NativeNullableString = None,
    ) -> dict[str, Any]:
        """Terminate an open correction/translation run, retaining accepted work. Retry with the same key."""
        return _call_with_validated_input(
            terminate_dispatch_run,
            runtime,
            TerminateDispatchRunInput,
            {
                key: value
                for key, value in locals().items()
                if key in TerminateDispatchRunInput.model_fields
            },
        )


def register_dispatch_run_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register subtitle-run tools at their existing inventory position."""

    @server.tool(
        name="pandrator_create_dispatch_run",
        title="Create a subtitle dispatch run",
        annotations=write_action,
    )
    def dispatch_create_tool(
        session_id: str,
        kind: Literal["correction", "translation"],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        instructions: Annotated[str, Field(max_length=16_000)] = "",
        source_artifact_id: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=80),
        ] = None,
        source_language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=40),
        ] = None,
        target_language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=40),
        ] = None,
        char_limit: Annotated[int, Field(ge=1, le=100_000)] = 6_000,
        max_segments_per_batch: Annotated[
            int,
            Field(ge=1, le=500),
        ] = 40,
        no_remove_subtitles: bool = False,
        correction_style: Literal["publishable", "faithful"] = "publishable",
        context_before: Annotated[int, Field(ge=0, le=20)] = 8,
        context_after: Annotated[int, Field(ge=0, le=20)] = 2,
        timing_context_mode: Literal["full", "overlap_only", "none"] = "full",
        substantial_gap_ms: Annotated[
            int,
            Field(ge=0, le=60_000),
        ] = 2_000,
        glossary: dict[str, str] | None = None,
        execution_mode: Literal["serial", "parallel"] = "serial",
        max_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 1,
        context_capsule: DelegationContextCapsuleInput | None = None,
    ) -> dict[str, Any]:
        """Create a serial or bounded-parallel correction/translation run."""

        return _call_with_validated_input(
            create_dispatch_run,
            runtime,
            CreateDispatchRunInput,
            {
                key: value
                for key, value in locals().items()
                if key in CreateDispatchRunInput.model_fields and value is not None
            },
        )

    @server.tool(
        name="pandrator_list_dispatch_runs",
        title="List subtitle dispatch runs",
        annotations=read_only,
    )
    def dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List dispatch metadata only; canonical task packets appear on claim."""

        return _call_with_validated_input(
            list_dispatch_runs,
            runtime,
            ListDispatchRunsInput,
            {"session_id": session_id, "limit": limit},
        )

    @server.tool(
        name="pandrator_get_dispatch_run",
        title="Inspect a subtitle dispatch run",
        annotations=read_only,
    )
    def dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect run metadata and final artifact state without batch content."""

        return _call_with_validated_input(
            get_dispatch_run,
            runtime,
            GetDispatchRunInput,
            {"run_id": run_id},
        )


def register_dispatch_batch_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    _response: Callable[[dict[str, Any], str], Any],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register subtitle-batch tools at their existing inventory position."""

    @server.tool(
        name="pandrator_inspect_dispatch_split_boundaries",
        title="Inspect verified subtitle split boundaries",
        annotations=read_only,
    )
    def dispatch_split_boundaries_tool(
        batch_id: str,
        lease_token: str,
        cue_id: Annotated[int, Field(ge=1, strict=True)],
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        """Inspect bounded source-word anchors for one actionable passage under its current lease."""
        return _call_with_validated_input(
            inspect_dispatch_split_boundaries,
            runtime,
            InspectDispatchSplitBoundariesInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "cue_id": cue_id,
                "offset": offset,
                "limit": limit,
            },
        )

    @server.tool(
        name="pandrator_claim_dispatch_batch",
        title="Claim a subtitle dispatch batch",
        annotations=write_action,
    )
    def dispatch_claim_tool(
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
        packet_format: Literal["standard", "compact"] = "standard",
        known_manifest_hash: Annotated[
            NativeNullableString, Field(pattern=r"^[a-f0-9]{64}$")
        ] = None,
        response_mode: Literal["standard", "structured"] = "standard",
    ) -> dict[str, Any]:
        """Claim one canonical task packet; each cue and timing value appears once."""

        envelope = _call_with_validated_input(
            claim_dispatch_batch,
            runtime,
            ClaimDispatchBatchInput,
            {
                key: value
                for key, value in locals().items()
                if key in ClaimDispatchBatchInput.model_fields and value is not None
            },
        )
        return _response(envelope, response_mode)

    @server.tool(
        name="pandrator_renew_dispatch_batch",
        title="Renew a subtitle dispatch lease",
        annotations=write_action,
    )
    def dispatch_renew_tool(
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
        """Renew only the matching batch lease; keep lease_token scoped to this batch."""

        return _call_with_validated_input(
            renew_dispatch_batch,
            runtime,
            RenewDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_release_dispatch_batch",
        title="Release a subtitle dispatch lease",
        annotations=write_action,
    )
    def dispatch_release_tool(
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
        """Release a claimed batch with its matching lease_token before retrying later."""

        return _call_with_validated_input(
            release_dispatch_batch,
            runtime,
            ReleaseDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_submit_dispatch_batch",
        title="Submit a subtitle dispatch batch",
        annotations=write_action,
    )
    def dispatch_submit_tool(
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
        result: DispatchStructuredResultInput | None = None,
        context_delta: DelegationContextDeltaInput | None = None,
        response_text: NativeResponseText = None,
    ) -> dict[str, Any]:
        """Submit one typed result; response_text is a legacy compatibility path."""

        return _call_with_validated_input(
            submit_dispatch_batch,
            runtime,
            SubmitDispatchBatchInput,
            {
                key: value
                for key, value in locals().items()
                if key in SubmitDispatchBatchInput.model_fields and value is not None
            },
        )
