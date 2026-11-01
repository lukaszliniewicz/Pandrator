"""Register fixed public-link and submission-review MCP tools."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, Literal

from ..context import McpRuntime
from ..public_link_actions import PUBLIC_LINK_OPERATIONS, PublicLinkOperation
from ..results import ToolOutcome


def _register(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any],
    operation: PublicLinkOperation,
    annotations: Any,
) -> None:
    def handler(current: McpRuntime, arguments: Any) -> ToolOutcome:
        result = current.require_application().public_link_request(
            operation.action, arguments.model_dump(mode="json")
        )
        return ToolOutcome(result=result)

    def invoke(**values: Any) -> Any:
        mode = values.pop("response_mode", "standard")
        if mode not in {"standard", "structured"}:
            raise ValueError("response_mode must be standard or structured.")
        return response(validated_call(handler, runtime, operation.model, values), mode)

    parameters: list[inspect.Parameter] = []
    annotations_map: dict[str, Any] = {"return": dict[str, Any]}
    for name, field in operation.model.model_fields.items():
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


def register_public_link_tools(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any],
    *,
    read_only: Any,
    write_action: Any,
) -> None:
    for operation in PUBLIC_LINK_OPERATIONS:
        annotations = read_only if operation.method == "GET" else write_action
        _register(server, runtime, validated_call, response, operation, annotations)


__all__ = ["register_public_link_tools"]
