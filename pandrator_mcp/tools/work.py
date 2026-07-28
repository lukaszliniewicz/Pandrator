"""Read-only durable-work tool handlers."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    CancelWorkInput,
    GetWorkInput,
    GetWorkLogInput,
    ListWorkInput,
)
from ..work_mapping import (
    application_work_reference,
    manager_task_log,
    manager_work_projection,
    manager_work_reference,
)


def list_work(
    runtime: McpRuntime,
    arguments: ListWorkInput,
) -> dict[str, Any]:
    return runtime.require_application().list_work(
        session_id=arguments.session_id,
        kinds=arguments.kinds,
        states=arguments.states,
        limit=arguments.limit,
    )


def get_work(
    runtime: McpRuntime,
    arguments: GetWorkInput,
) -> ToolOutcome:
    if arguments.work_type == "manager_operation":
        result = manager_work_projection(runtime.manager.operation(arguments.work_id))
        if arguments.include_events:
            result["events"] = manager_task_log(runtime.manager.operation_tasks(arguments.work_id))
        return ToolOutcome(
            result=result,
            work=manager_work_reference(result),
        )
    application = runtime.require_application()
    result = application.get_work(arguments.work_id)
    if arguments.include_events:
        result = dict(result)
        result["events"] = application.get_work_events(
            arguments.work_id,
            limit=arguments.event_limit,
        )
    return ToolOutcome(
        result=result,
        work=application_work_reference(result),
    )


def get_work_log(
    runtime: McpRuntime,
    arguments: GetWorkLogInput,
) -> dict[str, Any]:
    if arguments.work_type == "manager_operation":
        return manager_task_log(runtime.manager.operation_tasks(arguments.work_id))
    return runtime.require_application().get_work_events(
        arguments.work_id,
        after=arguments.after,
        limit=arguments.limit,
    )


def cancel_work(
    runtime: McpRuntime,
    arguments: CancelWorkInput,
) -> ToolOutcome:
    if arguments.work_type == "manager_operation":
        result = runtime.manager.cancel_operation(
            arguments.work_id,
            idempotency_key=arguments.idempotency_key,
        )
        work = manager_work_reference(
            {
                "id": arguments.work_id,
                "state": "running",
                "progress": 0.0,
            }
        )
        return ToolOutcome(
            result={
                "schema_version": "1",
                "type": "manager_operation",
                "id": arguments.work_id,
                "cancellation_requested": (
                    result.get("status")
                    == "cancellation_requested"
                    or bool(result.get("cancellation_requested"))
                ),
            },
            work=work,
            next_actions=[
                NextAction(
                    tool="pandrator_get_work",
                    arguments={
                        "work_type": "manager_operation",
                        "work_id": work.id,
                        "include_events": True,
                    },
                    reason=(
                        "Confirm the durable Manager cancellation "
                        "state and task boundary."
                    ),
                )
            ],
        )
    result = runtime.require_application().cancel_work(
        arguments.work_id,
        idempotency_key=arguments.idempotency_key,
    )
    work = application_work_reference(result)
    return ToolOutcome(
        result=result,
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={
                    "work_type": "job",
                    "work_id": work.id,
                    "include_events": True,
                },
                reason="Confirm the durable cancellation state and event.",
            )
        ],
    )
