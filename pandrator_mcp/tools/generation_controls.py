"""Portable MCP tools for session characters and voice casting."""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.generation_controls import (
    GetGenerationControlsInput,
    UpdateGenerationControlsInput,
)

GENERATION_CONTROLS_ACTIONS = (
    (
        "get",
        "pandrator_get_generation_controls",
        "Inspect characters and voice cast",
        GetGenerationControlsInput,
        "read",
        "Inspect the revisioned character dictionary and voice cast for the selected session.",
    ),
    (
        "update",
        "pandrator_update_generation_controls",
        "Update characters and voice cast",
        UpdateGenerationControlsInput,
        "write",
        "Update the revisioned character dictionary and voice cast for the selected session.",
    ),
)


def _portable_arguments(
    arguments: GetGenerationControlsInput | UpdateGenerationControlsInput,
) -> dict[str, Any]:
    """Dump top-level optional fields while retaining nested JSON nulls."""

    values = arguments.model_dump(mode="json", exclude_none=False)
    return {key: value for key, value in values.items() if value is not None}


def generation_controls_action(
    runtime: McpRuntime,
    action: str,
    arguments: GetGenerationControlsInput | UpdateGenerationControlsInput,
) -> ToolOutcome:
    payload = runtime.require_application().generation_controls_request(
        action, _portable_arguments(arguments)
    )
    next_actions: list[NextAction] = []
    if action == "update":
        next_actions.append(
            NextAction(
                tool="pandrator_get_generation_controls",
                arguments={"session_id": arguments.session_id},
                reason="Inspect the current revisioned character dictionary and voice cast after the update.",
            )
        )
    return ToolOutcome(result=payload, next_actions=next_actions)


def register_generation_controls_tools(
    server, runtime: McpRuntime, validated_call, *, read_only, write_action
) -> None:
    """Register flat, typed signatures backed by the portable input models."""

    def make_tool(action, model, name, title, annotation):
        def handle(current_runtime, arguments):
            return generation_controls_action(current_runtime, action, arguments)

        def invoke(**values) -> dict[str, Any]:
            return validated_call(handle, runtime, model, values)

        parameters = []
        annotations: dict[str, Any] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            field_annotation = Annotated[field.annotation, field]
            annotations[field_name] = field_annotation
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=field_annotation,
                    default=(inspect.Parameter.empty if field.is_required() else field.default),
                )
            )
        invoke.__name__ = name
        invoke.__doc__ = annotation
        invoke.__annotations__ = annotations
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        return invoke

    for action, name, title, model, risk, description in GENERATION_CONTROLS_ACTIONS:
        server.tool(
            name=name,
            title=title,
            annotations=read_only if risk == "read" else write_action,
        )(
            make_tool(
                action,
                model,
                name,
                title,
                description,
            )
        )


__all__ = [
    "GENERATION_CONTROLS_ACTIONS",
    "generation_controls_action",
    "register_generation_controls_tools",
]
