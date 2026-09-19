"""Portable MCP tool for editing managed voice metadata."""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.voice_metadata import UpdateVoiceMetadataInput


def update_voice_metadata(
    runtime: McpRuntime,
    arguments: UpdateVoiceMetadataInput,
) -> ToolOutcome:
    """Apply the supplied managed-voice metadata fields once."""

    payload = runtime.require_application().voice_metadata_request(
        "update",
        {
            "voice_id": arguments.voice_id,
            "expected_revision": arguments.expected_revision,
            "changes": arguments.changes.model_dump(mode="json", exclude_unset=True),
        },
    )
    return ToolOutcome(
        result=payload,
        next_actions=[
            NextAction(
                tool="pandrator_get_voice_catalog",
                arguments={},
                reason="Inspect the updated managed voice details in the current voice catalog.",
            )
        ],
    )


def register_voice_metadata_tools(
    server, runtime: McpRuntime, validated_call, *, write_action
) -> None:
    """Register the flat typed MCP signature from the portable input model."""

    model = UpdateVoiceMetadataInput

    def invoke(**values) -> dict[str, Any]:
        return validated_call(update_voice_metadata, runtime, model, values)

    parameters = []
    annotations: dict[str, Any] = {"return": dict[str, Any]}
    for field_name, field in model.model_fields.items():
        annotation = Annotated[field.annotation, field]
        annotations[field_name] = annotation
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=(inspect.Parameter.empty if field.is_required() else field.default),
            )
        )
    invoke.__name__ = "pandrator_update_voice_metadata"
    invoke.__doc__ = "Update managed voice details."
    invoke.__annotations__ = annotations
    invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])

    server.tool(
        name="pandrator_update_voice_metadata",
        title="Update managed voice details",
        annotations=write_action,
    )(invoke)


__all__ = ["register_voice_metadata_tools", "update_voice_metadata"]
