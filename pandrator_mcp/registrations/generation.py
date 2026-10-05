"""Generation MCP registration boundaries share the adapter's guarded calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..native_enums import NativeNullableEnum
from ..native_text import NativeNullableString
from ..schemas import (
    AssembleGenerationRunInput,
    GenerateSpeechPlanInput,
    ListGenerationRunsInput,
    ListGenerationSegmentsInput,
    ListSpeechPlanRevisionsInput,
    PrepareSpeechPlanInput,
    RegenerateSegmentsInput,
    ReviewSpeechPlanInput,
    ReviseSpeechBlockPlanBatchInput,
    ReviseSpeechBlockPlanInput,
    SelectTakeInput,
    SpeechPlanStatusInput,
    UpdateGenerationSegmentBatchItem,
    UpdateGenerationSegmentInput,
    UpdateGenerationSegmentsInput,
)
from ..tools import (
    assemble_generation_run,
    generate_speech_plan,
    list_generation_runs,
    list_generation_segments,
    list_speech_plan_revisions,
    prepare_speech_plan,
    regenerate_segments,
    review_speech_plan,
    revise_speech_block_plan,
    revise_speech_block_plan_batch,
    select_take,
    speech_plan_status,
    update_generation_segment,
    update_generation_segments,
)


def register_generation_plan_tools(
    server: Any,
    runtime: McpRuntime,
    _call: Callable[..., dict[str, Any]],
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    """Register plan inspection/preparation at its original inventory position."""

    @server.tool(
        name="pandrator_list_generation_runs",
        title="List reviewable generation runs",
        annotations=read_only,
    )
    def generation_runs_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        include_repairs: bool = False,
    ) -> dict[str, Any]:
        """List generation runs for review or export selection.

        Use ``result_generation_run_id`` to select final audio; use the root
        run ID to select the original audio.
        """

        return _call(
            list_generation_runs,
            runtime,
            ListGenerationRunsInput(
                session_id=session_id,
                limit=limit,
                include_repairs=include_repairs,
            ),
        )

    @server.tool(
        name="pandrator_list_generation_segments",
        title="List generation segments and audio takes",
        annotations=read_only,
    )
    def generation_segments_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        cursor: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        generation_run_id: Annotated[NativeNullableString, Field(max_length=80)] = None,
        plan_revision_id: NativeNullableString = None,
        view: Literal["full", "compact", "provenance"] = "full",
        fields: list[str] | None = None,
        end_ordinal: int | None = None,
        around_ordinal: int | None = None,
        source_cue_id: NativeNullableString = None,
        radius: Annotated[int, Field(ge=0, le=25)] = 2,
    ) -> dict[str, Any]:
        """List generation segments, assigned voices, takes, and text."""

        return _call(
            list_generation_segments,
            runtime,
            ListGenerationSegmentsInput(
                session_id=session_id,
                cursor=cursor,
                limit=limit,
                generation_run_id=generation_run_id,
                plan_revision_id=plan_revision_id,
                view=view,
                fields=fields,
                end_ordinal=end_ordinal,
                around_ordinal=around_ordinal,
                source_cue_id=source_cue_id,
                radius=radius,
            ),
        )

    @server.tool(
        name="pandrator_get_speech_plan_status",
        title="Inspect speech-plan review status",
        annotations=read_only,
    )
    def speech_plan_status_tool(session_id: str) -> dict[str, Any]:
        """Inspect current text input, active plan compatibility, review state, and revision guards."""
        return _call_with_validated_input(
            speech_plan_status,
            runtime,
            SpeechPlanStatusInput,
            {
                key: value
                for key, value in locals().items()
                if key in SpeechPlanStatusInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_prepare_speech_plan",
        title="Prepare a fresh reviewable speech plan",
        annotations=write_action,
    )
    def prepare_speech_plan_tool(
        session_id: str,
        expected_revision: int,
        source_artifact_id: str,
        idempotency_key: str,
        expected_plan_revision_id: NativeNullableString = None,
    ) -> dict[str, Any]:
        """Deterministically rebuild speech blocks from the selected subtitle/text artifact without starting synthesis."""
        return _call_with_validated_input(
            prepare_speech_plan,
            runtime,
            PrepareSpeechPlanInput,
            {
                key: value
                for key, value in locals().items()
                if key in PrepareSpeechPlanInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_review_speech_plan",
        title="Mark the selected speech plan reviewed",
        annotations=write_action,
    )
    def review_speech_plan_tool(
        session_id: str, revision_id: str, content_signature: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Record review only when the active plan still has the inspected content signature."""
        return _call_with_validated_input(
            review_speech_plan,
            runtime,
            ReviewSpeechPlanInput,
            {
                key: value
                for key, value in locals().items()
                if key in ReviewSpeechPlanInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_list_speech_plan_revisions",
        title="List versioned speech plans",
        annotations=read_only,
    )
    def speech_plan_revisions_tool(
        session_id: str, limit: int = 50, before_revision_number: int | None = None
    ) -> dict[str, Any]:
        """Inspect automatic/manual revision history, ancestry and reusable/stale take counts."""
        return _call_with_validated_input(
            list_speech_plan_revisions,
            runtime,
            ListSpeechPlanRevisionsInput,
            {
                key: value
                for key, value in locals().items()
                if key in ListSpeechPlanRevisionsInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_revise_speech_block_plan_batch",
        title="Atomically revise speech-block topology",
        annotations=write_action,
    )
    def speech_plan_batch_tool(
        session_id: str,
        expected_revision_id: str,
        operations: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Apply up to 50 ordered split/merge edits atomically.

        Select a split with a nested selector such as ``segment: {ordinal: 0}``,
        and select merge inputs with nested selectors such as
        ``left: {ordinal: 1}, right: {ordinal: 2}``.
        Selectors may use ID, ordinal, source_cue_ids, or result_ref; split by
        unique text, cue, sentence, or cursor. Labels expose label.left/right
        results. Ambiguity rolls back the entire batch.
        """
        return _call_with_validated_input(
            revise_speech_block_plan_batch,
            runtime,
            ReviseSpeechBlockPlanBatchInput,
            {
                key: value
                for key, value in locals().items()
                if key in ReviseSpeechBlockPlanBatchInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_generate_speech_plan",
        title="Generate a selected speech-plan revision",
        annotations=write_action,
    )
    def generate_speech_plan_tool(
        session_id: str,
        speech_plan_revision_id: str,
        idempotency_key: str,
        stale_only: bool = False,
    ) -> dict[str, Any]:
        """Generate exactly the active selected revision without rebuilding topology. A stale revision is rejected; stale_only retains unchanged completed audio."""
        return _call_with_validated_input(
            generate_speech_plan,
            runtime,
            GenerateSpeechPlanInput,
            {
                key: value
                for key, value in locals().items()
                if key in GenerateSpeechPlanInput.model_fields
            },
        )


def register_generation_execution_tools(
    server: Any,
    runtime: McpRuntime,
    _call: Callable[..., dict[str, Any]],
    _call_with_validated_input: Callable[..., dict[str, Any]],
    *,
    write_action: Any,
    execute_action: Any,
) -> None:
    """Register segment edits/execution at its original inventory position."""

    @server.tool(
        name="pandrator_revise_speech_block_plan",
        title="Revise generation speech-block topology",
        annotations=write_action,
    )
    def generation_topology_revision_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision_id: Annotated[str, Field(min_length=1, max_length=80)],
        action: Literal["split", "merge", "restore", "resegment"],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        segment_id: Annotated[NativeNullableString, Field(min_length=1, max_length=80)] = None,
        cursor: Annotated[int | None, Field(strict=True)] = None,
        text_layer: NativeNullableEnum[Literal["display", "speech"]] = None,
        left_segment_id: Annotated[NativeNullableString, Field(min_length=1, max_length=80)] = None,
        right_segment_id: Annotated[
            NativeNullableString, Field(min_length=1, max_length=80)
        ] = None,
        target_revision_id: Annotated[
            NativeNullableString, Field(min_length=1, max_length=80)
        ] = None,
        segment_ids: list[str] | None = None,
        boundaries: list[int] | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Split/merge/restore a plan, or draft a bounded audiobook resegmentation. Select contiguous segment_ids and exact joined-text boundaries or max_chars. Inspect the returned preview, then restore the draft to adopt it."""

        return _call_with_validated_input(
            revise_speech_block_plan,
            runtime,
            ReviseSpeechBlockPlanInput,
            {
                "session_id": session_id,
                "expected_revision_id": expected_revision_id,
                "action": action,
                "idempotency_key": idempotency_key,
                "segment_id": segment_id,
                "cursor": cursor,
                "text_layer": text_layer,
                "left_segment_id": left_segment_id,
                "right_segment_id": right_segment_id,
                "target_revision_id": target_revision_id,
                "segment_ids": segment_ids,
                "boundaries": boundaries,
                "max_chars": max_chars,
            },
        )

    @server.tool(
        name="pandrator_update_generation_segment",
        title="Update generation segment text or voice overrides",
        annotations=write_action,
    )
    def generation_segment_update_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        segment_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        text: Annotated[
            NativeNullableString, Field(min_length=1, max_length=2000, pattern=r"\S")
        ] = None,
        optimized_text: Annotated[NativeNullableString, Field(max_length=2000)] = None,
        removed: bool | None = None,
        voice_id: Annotated[NativeNullableString, Field(max_length=100)] = None,
        voice: Annotated[NativeNullableString, Field(max_length=100)] = None,
        language: Annotated[NativeNullableString, Field(max_length=20)] = None,
    ) -> dict[str, Any]:
        """Update a generation segment's text or voice override with revision guard."""

        return _call(
            update_generation_segment,
            runtime,
            UpdateGenerationSegmentInput(
                session_id=session_id,
                segment_id=segment_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                text=text,
                optimized_text=optimized_text,
                removed=removed,
                voice_id=voice_id,
                voice=voice,
                language=language,
            ),
        )

    @server.tool(
        name="pandrator_update_generation_segments",
        title="Atomically update reviewed generation segments",
        annotations=write_action,
    )
    def generation_segments_update_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        idempotency_key: Annotated[str, Field(min_length=8, max_length=120)],
        updates: Annotated[
            list[UpdateGenerationSegmentBatchItem],
            Field(min_length=1, max_length=100),
        ],
    ) -> dict[str, Any]:
        """Atomically update reviewed speech text or exclude/restore blocks. All supplied segment revisions must match; retained source subtitles and audio history are unchanged."""

        return _call_with_validated_input(
            update_generation_segments,
            runtime,
            UpdateGenerationSegmentsInput,
            {
                key: value
                for key, value in locals().items()
                if key in UpdateGenerationSegmentsInput.model_fields
            },
        )

    @server.tool(
        name="pandrator_select_take",
        title="Select an alternative audio take for a generation segment",
        annotations=write_action,
    )
    def generation_select_take_tool(
        segment_id: Annotated[str, Field(min_length=1, max_length=80)],
        take_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Select an alternative synthesized take for a segment."""

        return _call(
            select_take,
            runtime,
            SelectTakeInput(
                segment_id=segment_id,
                take_id=take_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_regenerate_segments",
        title="Trigger targeted synthesis for specific generation segments",
        annotations=execute_action,
    )
    def generation_regenerate_segments_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        segment_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Trigger targeted synthesis for a specific list of segment IDs."""

        return _call(
            regenerate_segments,
            runtime,
            RegenerateSegmentsInput(
                session_id=session_id,
                segment_ids=segment_ids,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_assemble_generation_run",
        title="Assemble the session using takes current at a generation run",
        annotations=execute_action,
    )
    def generation_assemble_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        generation_run_id: Annotated[NativeNullableString, Field(max_length=80)] = None,
    ) -> dict[str, Any]:
        """Assemble the whole session, using the selected/current takes at that run. For a single review clip, download the generation take artifact exposed by pandrator_list_generation_segments instead."""

        return _call(
            assemble_generation_run,
            runtime,
            AssembleGenerationRunInput(
                session_id=session_id,
                generation_run_id=generation_run_id,
                idempotency_key=idempotency_key,
            ),
        )
