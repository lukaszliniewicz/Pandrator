"""Read-only durable-work tool handlers."""

from __future__ import annotations

import time
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

_TERMINAL_WORK_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _work_is_terminal(work: Any) -> bool:
    return bool(work is not None and work.state in _TERMINAL_WORK_STATES)


def _poll_delay_seconds(work: Any) -> float:
    poll_after_ms = getattr(work, "poll_after_ms", 0)
    return max(0.25, min(float(poll_after_ms) / 1_000.0, 10.0))


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
    requested_seconds = arguments.wait_seconds
    started = time.monotonic() if requested_seconds else 0.0
    deadline = started + requested_seconds
    application = runtime.require_application() if arguments.work_type == "job" else None

    if arguments.work_type == "manager_operation":
        result = manager_work_projection(runtime.manager.operation(arguments.work_id))
        work = manager_work_reference(result)

        def inspect() -> dict[str, Any]:
            return manager_work_projection(runtime.manager.operation(arguments.work_id))

    else:
        assert application is not None
        result = application.get_work(arguments.work_id)
        work = application_work_reference(result)

        def inspect() -> dict[str, Any]:
            return application.get_work(arguments.work_id)

    poll_count = 0
    timed_out = False
    no_clock_progress = False
    while requested_seconds and not _work_is_terminal(work):
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            timed_out = True
            break
        sleep_seconds = min(_poll_delay_seconds(work), remaining)
        before_sleep = now
        time.sleep(sleep_seconds)
        after_sleep = time.monotonic()
        no_clock_progress = after_sleep <= before_sleep
        result = inspect()
        poll_count += 1
        if arguments.work_type == "manager_operation":
            work = manager_work_reference(result)
        else:
            work = application_work_reference(result)
        if _work_is_terminal(work):
            break
        if no_clock_progress:
            timed_out = True
            break

    if requested_seconds:
        elapsed_seconds = max(0.0, time.monotonic() - started)
        if timed_out:
            elapsed_seconds = min(float(requested_seconds), elapsed_seconds)
        elapsed_seconds = round(elapsed_seconds, 3)
    else:
        elapsed_seconds = 0.0
    result = dict(result)
    result["wait"] = {
        "requested_seconds": requested_seconds,
        "elapsed_seconds": elapsed_seconds,
        "poll_count": poll_count,
        "timed_out": timed_out,
    }
    if arguments.include_events:
        if arguments.work_type == "manager_operation":
            result["events"] = manager_task_log(runtime.manager.operation_tasks(arguments.work_id))
        else:
            assert application is not None
            result["events"] = application.get_work_events(
                arguments.work_id,
                limit=arguments.event_limit,
            )
    next_actions: list[NextAction] = []
    if not _work_is_terminal(work):
        next_actions.append(
            NextAction(
                tool="pandrator_get_work",
                arguments=arguments.model_dump(mode="json"),
                reason="Continue monitoring this durable work item until terminal.",
            )
        )
    return ToolOutcome(
        result=result,
        work=work,
        next_actions=next_actions,
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
                    result.get("status") == "cancellation_requested"
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
                    reason=("Confirm the durable Manager cancellation state and task boundary."),
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
