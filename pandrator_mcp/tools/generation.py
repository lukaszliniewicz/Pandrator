"""Fine-grained segment inspection, cue editing, take selection, and audio assembly."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.generation import (
    AssembleGenerationRunInput,
    AdoptSubtitleSourceInput,
    GenerateSpeechPlanInput,
    ListSpeechPlanRevisionsInput,
    PrepareSpeechPlanInput,
    ReviewSpeechPlanInput,
    SpeechPlanStatusInput,
    ReviseSpeechBlockPlanBatchInput,
    ListGenerationSegmentsInput,
    RegenerateSegmentsInput,
    ReviseSpeechBlockPlanInput,
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
            "artifact_id": t.get("artifact_id"),
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
        "node_kind": segment.get("node_kind"),
        "source_segment_ids": segment.get("source_segment_ids") or [],
        "alignment_group": segment.get("alignment_group"),
        "text": segment.get("text"),
        "optimized_text": segment.get("optimized_text"),
        "speech_block_provenance": segment.get("speech_block_provenance") or {},
        "speech_plan": segment.get("speech_plan") or {},
        "optimization_status": segment.get("optimization_status"),
        "optimization_model": segment.get("optimization_model"),
        "optimization_reviewed": segment.get("optimization_reviewed"),
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
    options: dict[str, Any] = {}
    for key in ("plan_revision_id", "fields", "end_ordinal", "around_ordinal", "source_cue_id"):
        value = getattr(arguments, key)
        if value is not None:
            options[key] = value
    if arguments.view != "full":
        options["view"] = arguments.view
    if arguments.around_ordinal is not None or arguments.source_cue_id is not None:
        options["radius"] = arguments.radius
    payload = application.list_generation_segments(
        arguments.session_id,
        cursor=arguments.cursor,
        limit=arguments.limit,
        generation_run_id=arguments.generation_run_id,
        **options,
    )
    items = payload.get("items") or []
    return {
        "schema_version": "1",
        "session_id": arguments.session_id,
        "items": [(_segment_projection(item) if arguments.view == "full" and arguments.fields is None else item) for item in items if isinstance(item, dict)],
        "view": arguments.view,
        "fields": payload.get("fields"),
        "active_plan_revision_id": payload.get("active_plan_revision_id"),
        "is_active_revision": payload.get("is_active_revision"),
        "next_cursor": payload.get("next_cursor"),
        "total": payload.get("total"),
        "plan_revision_id": payload.get("plan_revision_id"),
        "plan_revision_number": payload.get("plan_revision_number"),
        "parent_revision_id": payload.get("parent_revision_id"),
        "operation_json": (payload.get("operation_json") or {}) if arguments.view == "full" else {key: value for key, value in (payload.get("operation_json") or {}).items() if key in {"action", "batch", "target_revision_id"}},
        "speech_block_settings": payload.get("speech_block_settings") or {},
    }


def revise_speech_block_plan(
    runtime: McpRuntime,
    arguments: ReviseSpeechBlockPlanInput,
) -> ToolOutcome:
    application = runtime.require_application()
    result = application.revise_generation_plan_topology(
        arguments.session_id,
        expected_revision_id=arguments.expected_revision_id,
        action=arguments.action,
        segment_id=arguments.segment_id,
        cursor=arguments.cursor,
        text_layer=arguments.text_layer,
        left_segment_id=arguments.left_segment_id,
        right_segment_id=arguments.right_segment_id,
        target_revision_id=arguments.target_revision_id,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result=result,
        next_actions=[
            NextAction(
                tool="pandrator_list_generation_segments",
                arguments={"session_id": arguments.session_id},
                reason="Re-list segments and inspect the new immutable plan revision.",
            )
        ],
    )


def speech_plan_status(runtime: McpRuntime, arguments: SpeechPlanStatusInput) -> dict[str, Any]:
    payload = runtime.require_application().get_speech_plan_status(arguments.session_id)
    return {"schema_version": "1", **payload}


def prepare_speech_plan(runtime: McpRuntime, arguments: PrepareSpeechPlanInput) -> ToolOutcome:
    result = runtime.require_application().prepare_speech_plan(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        expected_plan_revision_id=arguments.expected_plan_revision_id,
        source_artifact_id=arguments.source_artifact_id,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"schema_version": "1", **result}, next_actions=[NextAction(
        tool="pandrator_list_generation_segments",
        arguments={"session_id": arguments.session_id, "plan_revision_id": result.get("selected_revision_id"), "view": "provenance"},
        reason="Review every block and boundary in the newly prepared speech plan before marking it reviewed or generating audio.",
    )])


def review_speech_plan(runtime: McpRuntime, arguments: ReviewSpeechPlanInput) -> ToolOutcome:
    result = runtime.require_application().review_speech_plan(
        arguments.session_id, revision_id=arguments.revision_id,
        content_signature=arguments.content_signature, idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def list_speech_plan_revisions(runtime: McpRuntime, arguments: ListSpeechPlanRevisionsInput) -> dict[str, Any]:
    payload = runtime.require_application().list_speech_plan_revisions(
        arguments.session_id, limit=arguments.limit, before_revision_number=arguments.before_revision_number,
    )
    return {"schema_version": "1", "session_id": arguments.session_id, **payload}


def revise_speech_block_plan_batch(runtime: McpRuntime, arguments: ReviseSpeechBlockPlanBatchInput) -> ToolOutcome:
    result = runtime.require_application().revise_generation_plan_topology_batch(
        arguments.session_id, expected_revision_id=arguments.expected_revision_id,
        operations=[operation.model_dump(exclude_none=True) for operation in arguments.operations],
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result=result, next_actions=[NextAction(
        tool="pandrator_list_generation_segments",
        arguments={"session_id": arguments.session_id, "plan_revision_id": result.get("plan_revision_id"), "view": "compact"},
        reason="Review the final revision; original-to-final block IDs and named results are in the batch receipt.",
    )])


def generate_speech_plan(runtime: McpRuntime, arguments: GenerateSpeechPlanInput) -> ToolOutcome:
    result = runtime.require_application().start_generation_run(
        arguments.session_id, speech_plan_revision_id=arguments.speech_plan_revision_id,
        stale_only=arguments.stale_only, idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result={"schema_version": "1", "session_id": arguments.session_id, **result},
        work=application_work_reference(result),
        next_actions=[NextAction(
            tool="pandrator_list_generation_segments",
            arguments={"session_id": arguments.session_id, "plan_revision_id": arguments.speech_plan_revision_id, "view": "compact"},
            reason="Review synthesis progress for the pinned speech-plan revision before assembling its output.",
        )],
    )


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
        operation="regenerate",
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
            "run_id": (
                result.get("run_id") or result.get("id") if isinstance(result, dict) else None
            ),
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


def adopt_subtitle_source(runtime: McpRuntime, arguments: AdoptSubtitleSourceInput) -> ToolOutcome:
    result = runtime.require_application().adopt_subtitle_source(
        arguments.session_id, source_asset_id=arguments.source_asset_id,
        expected_revision=arguments.expected_revision, idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"session_id": arguments.session_id, **result}, next_actions=[NextAction(
        tool="pandrator_get_session", arguments={"session_id": arguments.session_id},
        reason="The existing managed subtitles now have a timed revision; inspect readiness before planning correction or translation.",
    )])
