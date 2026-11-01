"""MCP tools for previewing, requesting and inspecting a current-project copy."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, Literal

from ..context import McpRuntime
from ..project_copy_actions import PROJECT_COPY_OPERATIONS, ProjectCopyOperation
from ..results import ToolOutcome
from ..schemas.project_copy import PROJECT_COPY_INPUT_MODELS

_INPUT_MODELS_BY_NAME = {model.__name__: model for model in PROJECT_COPY_INPUT_MODELS}


def _register(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any],
    operation: ProjectCopyOperation,
    model: type[Any],
    annotations: Any,
) -> None:
    def handler(current: McpRuntime, arguments: Any) -> ToolOutcome:
        result = current.require_application().project_copy_request(
            operation.action,
            arguments.model_dump(mode="json"),
        )
        return ToolOutcome(result=result)

    def invoke(**values: Any) -> Any:
        mode = values.pop("response_mode", "standard")
        if mode not in {"standard", "structured"}:
            raise ValueError("response_mode must be standard or structured.")
        return response(validated_call(handler, runtime, model, values), mode)

    parameters: list[inspect.Parameter] = []
    annotations_map: dict[str, Any] = {"return": dict[str, Any]}
    for name, field in model.model_fields.items():
        annotation = Annotated[field.annotation, field]
        annotations_map[name] = annotation
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=inspect.Parameter.empty if field.is_required() else field.default,
            )
        )
    mode_type = Literal["standard", "structured"]
    annotations_map["response_mode"] = mode_type
    parameters.append(
        inspect.Parameter(
            "response_mode",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=mode_type,
            default="standard",
        )
    )
    invoke.__name__ = operation.name
    invoke.__doc__ = operation.description
    invoke.__annotations__ = annotations_map
    invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
    server.tool(name=operation.name, title=operation.title, annotations=annotations)(invoke)


def register_project_copy_tools(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    for operation in PROJECT_COPY_OPERATIONS:
        model = _INPUT_MODELS_BY_NAME.get(operation.model_name)
        if model is None:
            raise RuntimeError("Project copy operation has no registered strict input model.")
        annotations = read_only if operation.risk == "read" else write_action
        _register(server, runtime, validated_call, response, operation, model, annotations)


__all__ = ["register_project_copy_tools"]
