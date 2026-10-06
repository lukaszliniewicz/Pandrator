"""Media-edit MCP registrations borrow the adapter's guarded callbacks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..native_text import NativeNullableString
from ..schemas import (
    ClaimMediaEditDispatchBatchInput,
    CreateMediaEditDispatchRunInput,
    GetMediaEditArguments,
    GetMediaEditDispatchRunInput,
    InspectMediaEditBoundaryArguments,
    ListMediaEditCutsArguments,
    ListMediaEditDispatchRunsInput,
    MediaEditDispatchResultInput,
    MediaEditKeepRange,
    MediaEditSourceReference,
    PlanMediaEditWorkflowInput,
    PrepareMediaEditArguments,
    ProposeMediaEditArguments,
    RefineMediaEditBoundaryArguments,
    ReleaseMediaEditDispatchBatchInput,
    RenderMediaEditArguments,
    RenewMediaEditDispatchBatchInput,
    SubmitMediaEditDispatchBatchInput,
    UpdateMediaEditArguments,
)
from ..tools import (
    claim_media_edit_dispatch_batch,
    create_media_edit_dispatch_run,
    get_media_edit,
    get_media_edit_dispatch_run,
    inspect_media_edit_boundary,
    list_media_edit_cuts,
    list_media_edit_dispatch_runs,
    plan_media_edit_workflow,
    prepare_media_edit,
    propose_media_edit,
    refine_media_edit_boundary,
    release_media_edit_dispatch_batch,
    render_media_edit,
    renew_media_edit_dispatch_batch,
    submit_media_edit_dispatch_batch,
    update_media_edit,
)


def register_media_edit_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    _response: Callable[[dict[str, Any], str], Any],
    *,
    read_only: Any,
    write_action: Any,
    execute_action: Any,
) -> None:
    """Register media-edit inspection and actions at their original position."""

    @server.tool(
        name="pandrator_get_media_edit",
        title="Inspect media-edit plan state",
        annotations=read_only,
    )
    def media_edit_get_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        view: Literal["summary", "full"] = "summary",
        response_mode: Literal["standard", "structured"] = "standard",
    ) -> dict[str, Any]:
        """Inspect media-edit readiness and the active immutable revision."""

        envelope = _call_with_validated_input(
            get_media_edit,
            runtime,
            GetMediaEditArguments,
            {"session_id": session_id, "view": view},
        )
        return _response(envelope, response_mode)

    @server.tool(
        name="pandrator_list_media_edit_cuts",
        title="List bounded media-edit cuts",
        annotations=read_only,
    )
    def media_edit_cuts_list_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        revision: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict[str, Any]:
        """List the current removal cuts without exposing the full cue array."""

        return _call_with_validated_input(
            list_media_edit_cuts,
            runtime,
            ListMediaEditCutsArguments,
            {"session_id": session_id, "revision": revision},
        )

    @server.tool(
        name="pandrator_inspect_media_edit_boundary",
        title="Inspect a media-edit boundary",
        annotations=read_only,
    )
    def media_edit_boundary_inspect_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        cut_index: Annotated[int, Field(ge=1)],
        edge: Literal["start", "end"],
        revision: Annotated[int | None, Field(ge=1)] = None,
        context_ms: Annotated[int, Field(ge=250, le=30_000)] = 5_000,
        cue_limit: Annotated[int, Field(ge=1, le=100)] = 40,
    ) -> dict[str, Any]:
        """Inspect bounded cue, word, and speech-gap evidence around one edge."""

        return _call_with_validated_input(
            inspect_media_edit_boundary,
            runtime,
            InspectMediaEditBoundaryArguments,
            {
                "session_id": session_id,
                "cut_index": cut_index,
                "edge": edge,
                "revision": revision,
                "context_ms": context_ms,
                "cue_limit": cue_limit,
            },
        )

    @server.tool(
        name="pandrator_plan_media_edit_workflow",
        title="Plan a media-edit workflow procedure",
        annotations=read_only,
    )
    def media_edit_workflow_plan_tool(
        session_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=80,
                description="Existing media_edit session to inspect and advance.",
            ),
        ],
        instructions: Annotated[
            str,
            Field(
                min_length=1,
                max_length=10_000,
                description="Whole-recording editorial instructions for the passive cut proposal.",
            ),
        ],
        transcript_mode: Annotated[
            Literal["auto", "captions", "asr"],
            Field(
                description="Prefer attached captions automatically, require captions, or require generated ASR."
            ),
        ] = "auto",
        recording_source: Annotated[
            MediaEditSourceReference | None,
            Field(description="Optional primary recording to attach when none is current."),
        ] = None,
        transcript_source: Annotated[
            MediaEditSourceReference | None,
            Field(description="Optional authoritative Zoom/SRT/VTT transcript source."),
        ] = None,
        caption_alignment_method: Annotated[
            Literal["ctc", "ctc_asr_fallback", "asr"],
            Field(description="Timing method used only when authoritative captions are present."),
        ] = "ctc",
        caption_alignment_ctc_model: Annotated[
            Literal[
                "auto",
                "canary-ctc-aligner",
                "canary-ctc-aligner-q4_k.gguf",
                "qwen3-forced-aligner",
            ],
            Field(
                description="Forced aligner: auto selects Qwen for Japanese/Chinese/Korean/Cantonese and Canary otherwise. Qwen uses audio.cpp and a verified 1.13 GB model cache.",
            ),
        ] = "auto",
        caption_alignment_padding_ms: Annotated[int, Field(ge=250, le=5_000)] = 2_000,
        caption_alignment_batch_seconds: Annotated[int, Field(ge=5, le=60)] = 30,
        caption_alignment_min_confidence: Annotated[float, Field(ge=0.5, le=1.0)] = 0.5,
        caption_alignment_fallback_coverage: Annotated[float, Field(ge=0.0, le=1.0)] = 0.9,
        stt_overrides: Annotated[
            dict[str, Any] | None,
            Field(
                description="Safe STT/VAD setting overrides merged into the current STT section."
            ),
        ] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=3_600)] = 0,
        expires_in_minutes: Annotated[int, Field(ge=1, le=60)] = 30,
        materialize: Annotated[
            bool,
            Field(
                description="After a successful render, download it to the approved output root."
            ),
        ] = False,
        filename: Annotated[
            NativeNullableString,
            Field(
                max_length=255,
                description="Optional plain output filename; requires materialize=true.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Inspect live state and return a review-first media-edit procedure."""

        values: dict[str, Any] = {
            "session_id": session_id,
            "instructions": instructions,
            "transcript_mode": transcript_mode,
            "recording_source": recording_source,
            "transcript_source": transcript_source,
            "caption_alignment_method": caption_alignment_method,
            "caption_alignment_ctc_model": caption_alignment_ctc_model,
            "caption_alignment_padding_ms": caption_alignment_padding_ms,
            "caption_alignment_batch_seconds": caption_alignment_batch_seconds,
            "caption_alignment_min_confidence": caption_alignment_min_confidence,
            "caption_alignment_fallback_coverage": caption_alignment_fallback_coverage,
            "stt_overrides": stt_overrides or {},
            "wait_seconds": wait_seconds,
            "expires_in_minutes": expires_in_minutes,
            "materialize": materialize,
            "filename": filename,
        }
        return _call_with_validated_input(
            plan_media_edit_workflow,
            runtime,
            PlanMediaEditWorkflowInput,
            values,
        )

    @server.tool(
        name="pandrator_prepare_media_edit",
        title="Prepare a media-edit plan",
        annotations=write_action,
    )
    def media_edit_prepare_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        force: bool = False,
    ) -> dict[str, Any]:
        """Prepare or explicitly refresh the media-edit plan for a session."""

        return _call_with_validated_input(
            prepare_media_edit,
            runtime,
            PrepareMediaEditArguments,
            {
                "session_id": session_id,
                "force": force,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_update_media_edit",
        title="Update media-edit keep ranges",
        annotations=write_action,
    )
    def media_edit_update_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision: Annotated[int, Field(ge=1)],
        keep_ranges: list[MediaEditKeepRange],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        instructions: Annotated[NativeNullableString, Field(max_length=10_000)] = None,
        reviewed: bool | None = None,
    ) -> dict[str, Any]:
        """Apply keep ranges only when the supplied media-edit revision is current."""

        return _call_with_validated_input(
            update_media_edit,
            runtime,
            UpdateMediaEditArguments,
            {
                "session_id": session_id,
                "expected_revision": expected_revision,
                "keep_ranges": keep_ranges,
                "idempotency_key": idempotency_key,
                "instructions": instructions,
                "reviewed": reviewed,
            },
        )

    @server.tool(
        name="pandrator_refine_media_edit_boundary",
        title="Refine a media-edit boundary",
        annotations=write_action,
    )
    def media_edit_boundary_refine_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision: Annotated[int, Field(ge=1)],
        cut_index: Annotated[int, Field(ge=1)],
        edge: Literal["start", "end"],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        position_ms: Annotated[int | None, Field(ge=0)] = None,
        delta_ms: int | None = None,
    ) -> dict[str, Any]:
        """Move one cut edge atomically against the expected active revision."""

        return _call_with_validated_input(
            refine_media_edit_boundary,
            runtime,
            RefineMediaEditBoundaryArguments,
            {
                "session_id": session_id,
                "expected_revision": expected_revision,
                "cut_index": cut_index,
                "edge": edge,
                "position_ms": position_ms,
                "delta_ms": delta_ms,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_propose_media_edit",
        title="Propose a media edit",
        annotations=write_action,
    )
    def media_edit_propose_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        revision: Annotated[int, Field(ge=1)],
        instructions: Annotated[str, Field(min_length=1, max_length=10_000)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        model: Annotated[NativeNullableString, Field(max_length=512)] = None,
        wait: bool = True,
        timeout_seconds: Annotated[int, Field(ge=0, le=3_600)] = 60,
    ) -> dict[str, Any]:
        """Queue an instruction-driven proposal and optionally wait for its job."""

        return _call_with_validated_input(
            propose_media_edit,
            runtime,
            ProposeMediaEditArguments,
            {
                "session_id": session_id,
                "revision": revision,
                "instructions": instructions,
                "model": model,
                "idempotency_key": idempotency_key,
                "wait": wait,
                "timeout_seconds": timeout_seconds,
            },
        )

    @server.tool(
        name="pandrator_render_media_edit",
        title="Render a reviewed media edit",
        annotations=execute_action,
    )
    def media_edit_render_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        revision: Annotated[int, Field(ge=1)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        wait: bool = True,
        timeout_seconds: Annotated[int, Field(ge=0, le=3_600)] = 60,
        subtitles_only: bool = False,
    ) -> dict[str, Any]:
        """Render an edit, or resegment its subtitles only, and optionally wait."""

        return _call_with_validated_input(
            render_media_edit,
            runtime,
            RenderMediaEditArguments,
            {
                "session_id": session_id,
                "revision": revision,
                "idempotency_key": idempotency_key,
                "wait": wait,
                "timeout_seconds": timeout_seconds,
                "subtitles_only": subtitles_only,
            },
        )


def register_media_edit_dispatch_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_validated_input: Callable[..., dict[str, Any]],
    _response: Callable[..., Any],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register passive media-edit dispatch at its original position."""

    @server.tool(
        name="pandrator_create_media_edit_dispatch_run",
        title="Create a passive media-edit run",
        annotations=write_action,
    )
    def media_edit_dispatch_create_tool(
        session_id: str,
        revision: Annotated[int, Field(ge=1)],
        idempotency_key: Annotated[
            str,
            Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"),
        ],
        instructions: Annotated[str, Field(min_length=1, max_length=16_000)],
    ) -> dict[str, Any]:
        """Create one pinned whole-recording cue-evidence batch."""

        return _call_with_validated_input(
            create_media_edit_dispatch_run,
            runtime,
            CreateMediaEditDispatchRunInput,
            {
                "session_id": session_id,
                "revision": revision,
                "instructions": instructions,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_list_media_edit_dispatch_runs",
        title="List passive media-edit runs",
        annotations=read_only,
    )
    def media_edit_dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List media-edit dispatch metadata without exposing cue evidence."""

        return _call_with_validated_input(
            list_media_edit_dispatch_runs,
            runtime,
            ListMediaEditDispatchRunsInput,
            {
                "session_id": session_id,
                "limit": limit,
            },
        )

    @server.tool(
        name="pandrator_get_media_edit_dispatch_run",
        title="Inspect a passive media-edit run",
        annotations=read_only,
    )
    def media_edit_dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect media-edit dispatch status and result revision metadata."""

        return _call_with_validated_input(
            get_media_edit_dispatch_run,
            runtime,
            GetMediaEditDispatchRunInput,
            {
                "run_id": run_id,
            },
        )

    @server.tool(
        name="pandrator_claim_media_edit_dispatch_batch",
        title="Claim a passive media-edit batch",
        annotations=write_action,
    )
    def media_edit_dispatch_claim_tool(
        run_id: str,
        idempotency_key: Annotated[
            str,
            Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
        response_mode: Literal["standard", "structured"] = "standard",
    ) -> dict[str, Any]:
        """Claim the single global cue-evidence batch with a short lease."""

        envelope = _call_with_validated_input(
            claim_media_edit_dispatch_batch,
            runtime,
            ClaimMediaEditDispatchBatchInput,
            {
                "run_id": run_id,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )
        return _response(envelope, response_mode)

    @server.tool(
        name="pandrator_renew_media_edit_dispatch_batch",
        title="Renew a media-edit lease",
        annotations=write_action,
    )
    def media_edit_dispatch_renew_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Renew only the matching media-edit batch lease."""

        return _call_with_validated_input(
            renew_media_edit_dispatch_batch,
            runtime,
            RenewMediaEditDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_release_media_edit_dispatch_batch",
        title="Release a media-edit lease",
        annotations=write_action,
    )
    def media_edit_dispatch_release_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"),
        ],
    ) -> dict[str, Any]:
        """Release an unfinished media-edit batch back to ready."""

        return _call_with_validated_input(
            release_media_edit_dispatch_batch,
            runtime,
            ReleaseMediaEditDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(
        name="pandrator_submit_media_edit_dispatch_batch",
        title="Submit a passive media-edit batch",
        annotations=write_action,
    )
    def media_edit_dispatch_submit_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        result: MediaEditDispatchResultInput,
        idempotency_key: Annotated[
            str,
            Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"),
        ],
    ) -> dict[str, Any]:
        """Submit whole-cue removal spans, including an explicit empty result."""

        return _call_with_validated_input(
            submit_media_edit_dispatch_batch,
            runtime,
            SubmitMediaEditDispatchBatchInput,
            {
                "batch_id": batch_id,
                "lease_token": lease_token,
                "result": result,
                "idempotency_key": idempotency_key,
            },
        )
