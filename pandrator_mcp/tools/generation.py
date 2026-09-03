"""Fine-grained segment inspection, cue editing, take selection, and audio assembly."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.generation import (
    AssembleGenerationRunInput,
    ListGenerationSegmentsInput,
    RegenerateSegmentsInput,
    SelectTakeInput,
    UpdateGenerationSegmentInput,
)
from ..work_mapping import application_work_reference


def _segment_projection(segment: dict[str, Any]) -> dict[str, Any]:
    takes = segment.get("takes") or []
    safe_takes = [
        {
            "id": str(t.get("id") or ""),
            "take_number": t.get("take_number"),
            "status": t.get("status"),
            "duration_ms": t.get("duration_ms"),
            "created_at": t.get("created_at"),
        }
        for t in takes
        if isinstance(t, dict)
    ]
    return {
        "id": str(segment.get("id") or ""),
        "ordinal": segment.get("ordinal"),
        "revision": segment.get("revision"),
        "status": segment.get("status"),
        "start_ms": segment.get("start_ms"),
        "end_ms": segment.get("end_ms"),
        "speaker": segment.get("speaker"),
        "text": segment.get("text"),
        "optimized_text": segment.get("optimized_text"),
        "voice_id": segment.get("voice_id"),
        "voice": segment.get("voice"),
        "language": segment.get("language"),
        "selected_take_id": segment.get("selected_take_id"),
        "takes": safe_takes,
    }


def list_generation_segments(
    runtime: McpRuntime,
    arguments: ListGenerationSegmentsInput,
) -> dict[str, Any]:
    application = runtime.require_application()
    payload = application.list_generation_segments(
        arguments.session_id,
        cursor=arguments.cursor,
        limit=arguments.limit,
        generation_run_id=arguments.generation_run_id,
    )
    items = payload.get("items") or []
    return {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "items": [
            _segment_projection(item)
            for item in items
            if isinstance(item, dict)
        ],
        "next_cursor": payload.get("next_cursor"),
        "total": payload.get("total"),
    }


def update_generation_segment(
    runtime: McpRuntime,
    arguments: UpdateGenerationSegmentInput,
) -> ToolOutcome:
    application = runtime.require_application()
    changes: dict[str, Any] = {}
    if arguments.optimized_text is not None:
        changes["optimized_text"] = arguments.optimized_text
    if arguments.voice_id is not None:
        changes["voice_id"] = arguments.voice_id
    if arguments.voice is not None:
        changes["voice"] = arguments.voice
    if arguments.language is not None:
        changes["language"] = arguments.language

    result = application.update_generation_segment(
        arguments.segment_id,
        changes=changes,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    safe_result = _segment_projection(result) if isinstance(result, dict) else result
    return ToolOutcome(
        result=safe_result,
        next_actions=[
            NextAction(
                tool="pandrator_list_generation_segments",
                arguments={"session_id": arguments.session_id},
                reason="Review updated segments and their current revisions.",
            )
        ],
    )


def select_take(
    runtime: McpRuntime,
    arguments: SelectTakeInput,
) -> ToolOutcome:
    application = runtime.require_application()
    result = application.select_generation_take(
        arguments.segment_id,
        arguments.take_id,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    safe_result = _segment_projection(result) if isinstance(result, dict) else result
    return ToolOutcome(result=safe_result)


def regenerate_segments(
    runtime: McpRuntime,
    arguments: RegenerateSegmentsInput,
) -> ToolOutcome:
    application = runtime.require_application()
    result = application.start_generation_run(
        arguments.session_id,
        segment_ids=arguments.segment_ids,
        operation="generate",
        idempotency_key=arguments.idempotency_key,
    )
    work = (
        application_work_reference(result)
        if isinstance(result, dict) and (result.get("id") or result.get("work_id"))
        else None
    )
    return ToolOutcome(
        result={
            "schema_version": "1",
            "session_id": arguments.session_id,
            "run_id": result.get("run_id") if isinstance(result, dict) else None,
            "segment_count": len(arguments.segment_ids),
            "status": "queued",
        },
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_list_generation_segments",
                arguments={"session_id": arguments.session_id},
                reason="Monitor segment synthesis progress and generated takes.",
            )
        ],
    )


def assemble_generation_run(
    runtime: McpRuntime,
    arguments: AssembleGenerationRunInput,
) -> ToolOutcome:
    application = runtime.require_application()
    result = application.create_output_assembly(
        arguments.session_id,
        generation_run_id=arguments.generation_run_id,
        idempotency_key=arguments.idempotency_key,
    )
    work = (
        application_work_reference(result)
        if isinstance(result, dict) and (result.get("id") or result.get("work_id"))
        else None
    )
    return ToolOutcome(
        result={
            "schema_version": "1",
            "session_id": arguments.session_id,
            "assembly_id": result.get("id") if isinstance(result, dict) else None,
            "status": "queued",
        },
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_list_artifacts",
                arguments={"session_id": arguments.session_id},
                reason="Inspect the newly assembled output artifacts once completed.",
            )
        ],
    )
