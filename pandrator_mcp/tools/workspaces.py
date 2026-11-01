"""Workspace tools reuse the application's authoritative commands."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, Literal

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..workspace_actions import WORKSPACE_OPERATIONS, WorkspaceOperation


def _register(
    server: Any, runtime: McpRuntime, validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any], operation: WorkspaceOperation,
    annotations: Any,
) -> None:
    def handler(current: McpRuntime, arguments: Any) -> ToolOutcome:
        try:
            result = current.require_application().workspace_request(
                operation.action, arguments.model_dump(mode="json")
            )
        except PandratorMcpError as error:
            if error.code != "scope_denied":
                raise
            # The backend remains authoritative. Disclose prerequisites on the
            # existing failure path without adding an auth-status round trip.
            raise PandratorMcpError(
                "scope_denied",
                str(error),
                details={**error.details, "required_scopes": list(operation.required_scopes)},
                retryable=False,
                next_actions=[NextAction(
                    tool="pandrator_explain_system",
                    arguments={"topic": "collaboration", "detail": "full"},
                    reason="Review account, workspace approval and enrollment scopes before retrying.",
                )],
            ) from error
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
        parameters.append(inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation,
            default=inspect.Parameter.empty if field.is_required() else field.default,
        ))
    mode_type = Literal["standard", "structured"]
    annotations_map["response_mode"] = mode_type
    parameters.append(inspect.Parameter("response_mode", inspect.Parameter.KEYWORD_ONLY, annotation=mode_type, default="standard"))
    invoke.__name__ = operation.name
    invoke.__doc__ = (
        f"{operation.description} Required application scopes: "
        + ", ".join(operation.required_scopes) + "."
    )
    invoke.__annotations__ = annotations_map
    invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
    server.tool(
        name=operation.name, title=operation.title, annotations=annotations,
        meta={"pandrator/requiredScopes": list(operation.required_scopes)},
    )(invoke)


def register_workspace_tools(
    server: Any, runtime: McpRuntime, validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any], *, read_only: Any, write_action: Any,
) -> None:
    for operation in WORKSPACE_OPERATIONS:
        _register(server, runtime, validated_call, response, operation, read_only if operation.method == "GET" else write_action)
