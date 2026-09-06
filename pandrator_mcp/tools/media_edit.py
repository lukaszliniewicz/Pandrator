"""Revision-safe media-edit MCP handlers."""

from __future__ import annotations

from typing import Any, Callable

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas.media_edit import (
    GetMediaEditArguments,
    PrepareMediaEditArguments,
    ProposeMediaEditArguments,
    RenderMediaEditArguments,
    UpdateMediaEditArguments,
)
from ..work_mapping import application_work_reference

_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled", "canceled"})


def _job_id(payload: dict[str, Any]) -> str:
    value = payload.get("id") or payload.get("job_id") or payload.get("work_id")
    normalized = str(value or "").strip()
    if not normalized:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned a media-edit job without an identifier.",
        )
    return normalized


def _is_terminal(payload: dict[str, Any]) -> bool:
    state = str(payload.get("state") or payload.get("status") or "").strip().lower()
    return state in _TERMINAL_JOB_STATES


def _job_outcome(
    runtime: McpRuntime,
    job: dict[str, Any],
    *,
    wait: bool,
    timeout_seconds: int,
    tool_name: str,
) -> ToolOutcome:
    application = runtime.require_application()
    job_id = _job_id(job)
    result = (
        application.wait_for_job(job_id, timeout_seconds=timeout_seconds)
        if wait
        else job
    )
    next_actions: list[NextAction] = []
    if not _is_terminal(result):
        next_actions.append(
            NextAction(
                tool="pandrator_get_work",
                arguments={
                    "work_id": job_id,
                    "wait_seconds": timeout_seconds,
                },
                reason=(
                    f"Continue monitoring the durable {tool_name} job until it reaches a terminal state."
                ),
            )
        )
    return ToolOutcome(
        result=result,
        work=application_work_reference(result),
        next_actions=next_actions,
    )


def get_media_edit(
    runtime: McpRuntime,
    arguments: GetMediaEditArguments,
) -> dict[str, Any]:
    """Inspect the current media-edit readiness state and active revision."""

    return runtime.require_application().get_media_edit(arguments.session_id)


def prepare_media_edit(
    runtime: McpRuntime,
    arguments: PrepareMediaEditArguments,
) -> dict[str, Any]:
    """Prepare or refresh the media-edit plan for one session."""

    return runtime.require_application().prepare_media_edit(
        arguments.session_id,
        force=arguments.force,
        idempotency_key=arguments.idempotency_key,
    )


def update_media_edit(
    runtime: McpRuntime,
    arguments: UpdateMediaEditArguments,
) -> dict[str, Any]:
    """Apply one exact revision-guarded media-edit plan update."""

    optional = {
        "instructions": arguments.instructions,
        "reviewed": arguments.reviewed,
    }
    return runtime.require_application().update_media_edit(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
        keep_ranges=[
            item.model_dump(mode="json", exclude_none=True)
            for item in arguments.keep_ranges
        ],
        **{key: value for key, value in optional.items() if value is not None},
    )


def _queue_job(
    runtime: McpRuntime,
    *,
    arguments: ProposeMediaEditArguments | RenderMediaEditArguments,
    enqueue: Callable[[], dict[str, Any]],
    tool_name: str,
) -> ToolOutcome:
    job = enqueue()
    return _job_outcome(
        runtime,
        job,
        wait=arguments.wait,
        timeout_seconds=arguments.timeout_seconds,
        tool_name=tool_name,
    )


def propose_media_edit(
    runtime: McpRuntime,
    arguments: ProposeMediaEditArguments,
) -> ToolOutcome:
    """Queue an instruction-driven edit proposal and optionally wait for it."""

    application = runtime.require_application()
    proposal_kwargs: dict[str, Any] = {
        "revision": arguments.revision,
        "instructions": arguments.instructions,
        "idempotency_key": arguments.idempotency_key,
    }
    if arguments.model is not None:
        proposal_kwargs["model"] = arguments.model
    return _queue_job(
        runtime,
        arguments=arguments,
        enqueue=lambda: application.propose_media_edit(
            arguments.session_id,
            **proposal_kwargs,
        ),
        tool_name="media-edit proposal",
    )


def render_media_edit(
    runtime: McpRuntime,
    arguments: RenderMediaEditArguments,
) -> ToolOutcome:
    """Queue a reviewed media-edit render and optionally wait for it."""

    application = runtime.require_application()
    return _queue_job(
        runtime,
        arguments=arguments,
        enqueue=lambda: application.render_media_edit(
            arguments.session_id,
            revision=arguments.revision,
            idempotency_key=arguments.idempotency_key,
        ),
        tool_name="media-edit render",
    )
