"""Portable performance tools using the same authoritative target API as the UI."""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from ..context import McpRuntime
from ..errors import NextAction
from ..performance_actions import PERFORMANCE_ACTIONS
from ..results import ToolOutcome
from ..schemas import performance as schemas
from ..work_mapping import application_work_reference


def performance_action(runtime: McpRuntime, action: str, arguments) -> ToolOutcome:
    payload = runtime.require_application().performance_plan_request(
        action, arguments.model_dump(mode="json", exclude_none=True)
    )
    next_actions = []
    plan_id = payload.get("id") or payload.get("plan_id") or getattr(arguments, "plan_id", None)
    if action == "create" and arguments.mode == "passive":
        next_actions.append(
            NextAction(
                tool="pandrator_claim_performance_batch",
                arguments={"session_id": arguments.session_id, "plan_id": plan_id},
                reason="Claim a bounded batch. The target supplies the pSSML schema, read-only context and immutable actionable text.",
            )
        )
    elif action in {"create", "edit", "submit", "analyse"} and plan_id:
        next_actions.append(
            NextAction(
                tool="pandrator_get_performance_plan",
                arguments={"session_id": arguments.session_id, "plan_id": plan_id},
                reason="Inspect the completed annotations and current version before previewing or adopting them.",
            )
        )
    work = None
    if payload.get("job_id") and action in {"create", "analyse"}:
        work = application_work_reference({"job_id": payload["job_id"], "state": "queued"})
    return ToolOutcome(
        result={"schema_version": "1", **payload}, work=work, next_actions=next_actions
    )


def register_performance_tools(
    server, runtime: McpRuntime, validated_call, *, read_only, write_action
) -> None:
    """Generate flat, typed signatures from the same models the tools validate.

    No eval or arbitrary call routing: actions come from the static manifest.
    This avoids eleven hand-maintained copies of each field's MCP constraints.
    """

    def make_tool(action, model, name, title):
        def handle(current_runtime, arguments):
            return performance_action(current_runtime, action, arguments)

        def invoke(**values) -> dict[str, Any]:
            return validated_call(handle, runtime, model, values)

        parameters = []
        annotations = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            annotation = Annotated[field.annotation, field]
            annotations[field_name] = annotation
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=inspect.Parameter.empty if field.is_required() else field.default,
                )
            )
        invoke.__name__ = name
        invoke.__doc__ = (
            title
            + ". Source/context content is read-only. Generation text and boundaries never change. Adopt only reviewed saved annotations."
        )
        invoke.__annotations__ = annotations
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        return invoke

    for (
        action,
        name,
        title,
        model_name,
        risk,
        _scope,
        _operation,
        _method,
        _suffix,
    ) in PERFORMANCE_ACTIONS:
        model = getattr(schemas, model_name)
        server.tool(
            name=name, title=title, annotations=read_only if risk == "read" else write_action
        )(make_tool(action, model, name, title))
