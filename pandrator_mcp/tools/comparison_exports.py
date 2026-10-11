"""Portable MCP tool for exporting an ordered selected-take comparison."""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.comparison_exports import ComparisonExportInput
from ..work_mapping import application_work_reference


def create_comparison_export(
    runtime: McpRuntime,
    arguments: ComparisonExportInput,
) -> ToolOutcome:
    """Queue a reusable comparison without including separately assembled SFX."""

    if arguments.clips is not None:
        clips = [
            {
                **clip.model_dump(mode="json", exclude_unset=True),
                # Make the backend's default explicit in the canonical body.
                "audio_version_side": clip.audio_version_side,
            }
            for clip in arguments.clips
        ]
    else:
        assert arguments.generation_run_ids is not None
        assert arguments.segment_ids is not None
        clips = [
            {
                "generation_run_id": generation_run_id,
                "segment_id": segment_id,
                "audio_version_side": "after",
            }
            for generation_run_id in arguments.generation_run_ids
            for segment_id in arguments.segment_ids
        ]
    body = {
        "clips": clips,
        "gap_ms": arguments.gap_ms,
        "output_format": arguments.output_format,
    }
    payload = runtime.require_application().create_comparison_export(
        arguments.session_id,
        body=body,
        idempotency_key=arguments.idempotency_key,
    )
    work = (
        application_work_reference(payload)
        if any(payload.get(field) for field in ("job_id", "work_id", "id"))
        else None
    )
    next_actions = (
        [
            NextAction(
                tool="pandrator_get_work",
                arguments={"work_id": work.id},
                reason="Inspect this comparison-export job and its published audio and manifest artifacts.",
            )
        ]
        if work is not None and work.id
        else []
    )
    return ToolOutcome(result=payload, work=work, next_actions=next_actions)


def register_comparison_export_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    response,
    *,
    execute_action,
) -> None:
    """Register the strict typed comparison-export admission tool."""

    def invoke(**values) -> dict[str, Any]:
        response_mode = values.pop("response_mode", "standard")
        envelope = validated_call(
            create_comparison_export,
            runtime,
            ComparisonExportInput,
            values,
        )
        return response(envelope, response_mode)

    parameters = []
    annotations_map: dict[str, Any] = {"return": dict[str, Any]}
    for field_name, field in ComparisonExportInput.model_fields.items():
        field_annotation = Annotated[field.annotation, field]
        annotations_map[field_name] = field_annotation
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=field_annotation,
                default=(
                    inspect.Parameter.empty if field.is_required() else field.default
                ),
            )
        )
    response_annotation = Literal["standard", "structured"]
    annotations_map["response_mode"] = response_annotation
    parameters.append(
        inspect.Parameter(
            "response_mode",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=response_annotation,
            default="standard",
        )
    )
    invoke.__name__ = "pandrator_create_comparison_export"
    invoke.__doc__ = (
        "Queue a reusable speech-take comparison from canonical clips or two ordered runs and selected segments."
    )
    invoke.__annotations__ = annotations_map
    invoke.__signature__ = inspect.Signature(
        parameters, return_annotation=dict[str, Any]
    )
    server.tool(
        name="pandrator_create_comparison_export",
        title="Create a speech-take comparison export",
        annotations=execute_action,
    )(invoke)


__all__ = ["create_comparison_export", "register_comparison_export_tools"]
