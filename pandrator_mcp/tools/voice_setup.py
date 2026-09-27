"""Portable MCP tools for a session's single- or multi-voice mode."""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from ..context import McpRuntime
from ..results import ToolOutcome
from ..schemas.voice_setup import ConfigureVoiceSetupInput, GetVoiceSetupInput


def get_voice_setup(
    runtime: McpRuntime,
    arguments: GetVoiceSetupInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().get_voice_setup(arguments.session_id)
    )


def configure_voice_setup(
    runtime: McpRuntime,
    arguments: ConfigureVoiceSetupInput,
) -> ToolOutcome:
    result = runtime.require_application().configure_voice_setup(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        mode=arguments.mode,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result=result)


def _register_tool(server, runtime, validated_call, *, model, handler, name, title,
                   description, annotations) -> None:
    def invoke(**values) -> dict[str, Any]:
        return validated_call(handler, runtime, model, values)

    parameters = []
    annotations_map: dict[str, object] = {"return": dict[str, Any]}
    for field_name, field in model.model_fields.items():
        annotations_map[field_name] = Annotated[field.annotation, field]
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotations_map[field_name],
                default=(
                    inspect.Parameter.empty
                    if field.is_required()
                    else field.default
                ),
            )
        )
    invoke.__name__ = name
    invoke.__doc__ = description
    invoke.__annotations__ = annotations_map
    invoke.__signature__ = inspect.Signature(
        parameters,
        return_annotation=dict[str, Any],
    )
    server.tool(name=name, title=title, annotations=annotations)(invoke)


def register_voice_setup_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    *,
    read_only,
    write_action,
) -> None:
    """Register flat, typed voice-setup tools backed by the HTTP client."""

    _register_tool(
        server,
        runtime,
        validated_call,
        model=GetVoiceSetupInput,
        handler=get_voice_setup,
        name="pandrator_get_voice_setup",
        title="Inspect voice setup",
        description="Inspect the session's current single- or multi-voice mode.",
        annotations=read_only,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=ConfigureVoiceSetupInput,
        handler=configure_voice_setup,
        name="pandrator_configure_voice_setup",
        title="Configure voice setup",
        description=(
            "Set the session's single- or multi-voice mode using its current "
            "64-character revision and an idempotency key. This does not start work."
        ),
        annotations=write_action,
    )


__all__ = [
    "configure_voice_setup",
    "get_voice_setup",
    "register_voice_setup_tools",
]
