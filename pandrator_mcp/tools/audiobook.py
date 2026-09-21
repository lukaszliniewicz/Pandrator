"""Portable MCP tools for audiobook setup and speech-plan previews."""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from ..context import McpRuntime
from ..results import ToolOutcome
from ..schemas.audiobook import (
    ConfigureAudiobookInput,
    GetAudiobookSetupInput,
    PreviewSpeechSegmentInput,
)

AUDIOBOOK_ACTIONS = (
    (
        "get",
        "pandrator_get_audiobook_setup",
        "Inspect audiobook setup",
        GetAudiobookSetupInput,
        "read",
    ),
    (
        "configure",
        "pandrator_configure_audiobook",
        "Configure audiobook voice mode",
        ConfigureAudiobookInput,
        "write",
    ),
    (
        "preview",
        "pandrator_preview_speech_segment",
        "Preview a speech-plan segment",
        PreviewSpeechSegmentInput,
        "read",
    ),
)


def audiobook_action(
    runtime: McpRuntime,
    action: str,
    arguments: GetAudiobookSetupInput | ConfigureAudiobookInput | PreviewSpeechSegmentInput,
) -> ToolOutcome:
    """Dispatch one allowlisted audiobook operation to the HTTP client."""

    application = runtime.require_application()
    if action == "get":
        payload = application.get_audiobook_setup(arguments.session_id)
    elif action == "configure":
        if not isinstance(arguments, ConfigureAudiobookInput):
            raise ValueError("The configure action requires configure input.")
        payload = application.configure_audiobook(
            arguments.session_id,
            expected_revision=arguments.expected_revision,
            mode=arguments.mode,
            idempotency_key=arguments.idempotency_key,
        )
    elif action == "preview":
        if not isinstance(arguments, PreviewSpeechSegmentInput):
            raise ValueError("The preview action requires preview input.")
        payload = application.preview_speech_segment(
            arguments.session_id,
            revision_id=arguments.revision_id,
            segment_id=arguments.segment_id,
            generation_run_id=arguments.generation_run_id,
            include_request=arguments.include_request,
        )
    else:
        raise ValueError("Unknown audiobook action.")
    return ToolOutcome(result=payload)


def get_audiobook_setup(
    runtime: McpRuntime,
    arguments: GetAudiobookSetupInput,
) -> ToolOutcome:
    """Read compact audiobook mode, engine, status and revision state."""

    return audiobook_action(runtime, "get", arguments)


def configure_audiobook(
    runtime: McpRuntime,
    arguments: ConfigureAudiobookInput,
) -> ToolOutcome:
    """Atomically configure audiobook voice mode without starting work."""

    return audiobook_action(runtime, "configure", arguments)


def preview_speech_segment(
    runtime: McpRuntime,
    arguments: PreviewSpeechSegmentInput,
) -> ToolOutcome:
    """Compile current or frozen speech directions without creating audio."""

    return audiobook_action(runtime, "preview", arguments)


def _register_tool(
    server,
    runtime: McpRuntime,
    validated_call,
    *,
    model,
    name: str,
    title: str,
    description: str,
    annotations,
) -> None:
    def handle(**values) -> dict[str, Any]:
        return validated_call(_handler, runtime, model, values)

    # The selected handler is assigned per registration below. Keeping the
    # protocol signature flat avoids exposing a generic ``arguments`` object.
    _handler = {
        GetAudiobookSetupInput: get_audiobook_setup,
        ConfigureAudiobookInput: configure_audiobook,
        PreviewSpeechSegmentInput: preview_speech_segment,
    }[model]
    parameters = []
    annotations_map: dict[str, Any] = {"return": dict[str, Any]}
    for field_name, field in model.model_fields.items():
        field_annotation = Annotated[field.annotation, field]
        annotations_map[field_name] = field_annotation
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=field_annotation,
                default=(
                    inspect.Parameter.empty
                    if field.is_required()
                    else field.default
                ),
            )
        )
    handle.__name__ = name
    handle.__doc__ = description
    handle.__annotations__ = annotations_map
    handle.__signature__ = inspect.Signature(
        parameters,
        return_annotation=dict[str, Any],
    )
    server.tool(name=name, title=title, annotations=annotations)(handle)


def register_audiobook_tools(
    server, runtime: McpRuntime, validated_call, *, read_only, write_action
) -> None:
    """Register flat, typed audiobook tools backed by HTTP client methods."""

    _register_tool(
        server,
        runtime,
        validated_call,
        model=GetAudiobookSetupInput,
        name="pandrator_get_audiobook_setup",
        title="Inspect audiobook setup",
        description=(
            "Inspect compact audiobook mode, engine, status and current "
            "configuration revision without refreshing the catalog."
        ),
        annotations=read_only,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=ConfigureAudiobookInput,
        name="pandrator_configure_audiobook",
        title="Configure audiobook voice mode",
        description=(
            "Atomically configure audiobook annotation/casting and document "
            "optimization flags while preserving engine, references and directions; "
            "this does not start work."
        ),
        annotations=write_action,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=PreviewSpeechSegmentInput,
        name="pandrator_preview_speech_segment",
        title="Preview a speech-plan segment",
        description=(
            "Compile current or frozen casts and speech directions for one segment. "
            "This creates no performance plan and no audio; include_request opts "
            "into provider request details."
        ),
        annotations=read_only,
    )


__all__ = [
    "AUDIOBOOK_ACTIONS",
    "audiobook_action",
    "configure_audiobook",
    "get_audiobook_setup",
    "preview_speech_segment",
    "register_audiobook_tools",
]
