"""Compact lifecycle polling and selected-run audio coverage tools."""

from __future__ import annotations

import inspect
import math
import time
from typing import Annotated, Any, Literal, Protocol, cast

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas.common import WorkReference
from ..schemas.generation_operations import (
    GetGenerationOperationInput,
    GetGenerationRunCoverageInput,
)

_WORK_STATES = frozenset({"queued", "running", "waiting", "succeeded", "failed", "cancelled"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_COVERAGE_COUNTS = (
    "included_count",
    "effective_audio_count",
    "run_audio_count",
    "inherited_audio_count",
    "generated_audio_count",
    "missing_audio_count",
    "unavailable_artifact_count",
    "missing_file_count",
)


class _GenerationOperationApplication(Protocol):
    def get_generation_operation(
        self,
        operation_id: str,
        include_coverage: bool = True,
    ) -> dict[str, Any]: ...

    def get_generation_run_coverage(
        self,
        generation_run_id: str,
        audio_version_side: Literal["before", "after"] = "after",
    ) -> dict[str, Any]: ...


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:maximum] or None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _error_summary(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    code = _bounded_text(value.get("code"), 160)
    message = _bounded_text(value.get("message"), 2_000)
    if code is None and message is None:
        return None
    return {"code": code, "message": message}


def _coverage_summary(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    run_id = _bounded_text(value.get("run_id"), 80)
    plan_revision_id = _bounded_text(value.get("plan_revision_id"), 80)
    side = value.get("audio_version_side")
    if run_id is None or plan_revision_id is None or side not in {"before", "after"}:
        return None
    result: dict[str, Any] = {
        "run_id": run_id,
        "plan_revision_id": plan_revision_id,
        "audio_version_side": side,
    }
    for key in _COVERAGE_COUNTS:
        count = _nonnegative_int(value.get(key))
        if count is None:
            return None
        result[key] = count
    ordinals = value.get("missing_segment_ordinals")
    result["missing_segment_ordinals"] = (
        [item for item in ordinals[:20] if _nonnegative_int(item) is not None]
        if isinstance(ordinals, list)
        else []
    )
    result["export_ready"] = value.get("export_ready") is True
    return result


def _operation_summary(value: object, requested_id: str) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    operation_id = _bounded_text(payload.get("id"), 120) or requested_id
    session_id = _bounded_text(payload.get("session_id"), 80)
    phase = payload.get("phase")
    if phase not in {"preparation", "synthesis", "complete"}:
        phase = "preparation"
    state = payload.get("state")
    if state not in _WORK_STATES:
        state = "waiting"
    poll_after_ms = _nonnegative_int(payload.get("poll_after_ms"))
    poll_after_ms = min(poll_after_ms if poll_after_ms is not None else 1_000, 60_000)

    current_work: dict[str, Any] | None = None
    raw_work = payload.get("work")
    if isinstance(raw_work, dict):
        work_id = _bounded_text(raw_work.get("id"), 120)
        work_kind = _bounded_text(raw_work.get("kind"), 120)
        work_state = raw_work.get("state")
        if work_id and work_kind and work_state in _WORK_STATES:
            progress = raw_work.get("progress")
            if isinstance(progress, (int, float)) and not isinstance(progress, bool):
                progress = (
                    max(0.0, min(float(progress), 1.0)) if math.isfinite(float(progress)) else None
                )
            else:
                progress = None
            current_work = {
                "id": work_id,
                "kind": work_kind,
                "state": work_state,
                "progress": progress,
                "detail": _bounded_text(raw_work.get("detail"), 2_000),
                "error": _error_summary(raw_work.get("error")),
                "poll_after_ms": min(
                    _nonnegative_int(raw_work.get("poll_after_ms")) or 0,
                    60_000,
                ),
            }

    coverage = _coverage_summary(payload.get("coverage"))
    return {
        "schema_version": 1,
        "id": operation_id,
        "session_id": session_id,
        "phase": phase,
        "state": state,
        "cancellable": payload.get("cancellable") is True,
        "poll_after_ms": poll_after_ms,
        "preparation_work_id": _bounded_text(payload.get("preparation_work_id"), 120),
        "synthesis_work_id": _bounded_text(payload.get("synthesis_work_id"), 120),
        "generation_run_id": _bounded_text(payload.get("generation_run_id"), 80),
        "work": current_work,
        "preparation_error": _error_summary(payload.get("preparation_error")),
        "linkage_pending": payload.get("linkage_pending") is True,
        "linkage_diagnostic": _bounded_text(payload.get("linkage_diagnostic"), 300),
        "coverage": coverage,
    }


def _next_actions(
    summary: dict[str, Any],
    *,
    include_coverage: bool,
    wait_seconds: int,
) -> list[NextAction]:
    state = summary["state"]
    operation_id = summary["id"]
    if state not in _TERMINAL_STATES:
        return [
            NextAction(
                tool="pandrator_get_generation_operation",
                arguments={
                    "operation_id": operation_id,
                    "wait_seconds": max(1, min(wait_seconds or 30, 60)),
                    "include_coverage": include_coverage,
                },
                reason="Continue polling the same stable generation operation until it reaches a terminal state.",
            )
        ]

    coverage = summary.get("coverage")
    run_id = summary.get("generation_run_id")
    session_id = summary.get("session_id")
    if not include_coverage or not isinstance(coverage, dict) or not run_id or not session_id:
        return []
    side = coverage["audio_version_side"]
    if coverage["export_ready"]:
        return [
            NextAction(
                tool="pandrator_plan_export_variant",
                arguments={
                    "session_id": session_id,
                    "generation_run_id": run_id,
                    "audio_version_side": side,
                },
                reason="The selected run has current audio artifacts with stored files for every included segment; plan the desired export variant.",
            )
        ]
    return [
        NextAction(
            tool="pandrator_list_generation_segments",
            arguments={
                "session_id": session_id,
                "generation_run_id": run_id,
                "audio_version_side": side,
                "view": "compact",
            },
            reason="Inspect the selected run's compact segment coverage and locate rows with missing or unavailable audio.",
        )
    ]


def get_generation_operation(
    runtime: McpRuntime,
    arguments: GetGenerationOperationInput,
) -> ToolOutcome:
    """Poll one stable operation ID for at most the requested interval."""

    application = cast(
        _GenerationOperationApplication,
        runtime.require_application(),
    )
    requested_seconds = arguments.wait_seconds
    started_at = time.monotonic()
    deadline = started_at + requested_seconds
    poll_count = 0
    timed_out = False

    while True:
        payload = application.get_generation_operation(
            arguments.operation_id,
            include_coverage=arguments.include_coverage,
        )
        poll_count += 1
        summary = _operation_summary(payload, arguments.operation_id)
        if summary["state"] in _TERMINAL_STATES:
            break
        now = time.monotonic()
        if requested_seconds == 0 or now >= deadline:
            timed_out = requested_seconds > 0
            break
        delay = min(
            max(summary["poll_after_ms"] / 1_000.0, 0.1),
            10.0,
            max(0.0, deadline - now),
        )
        if delay <= 0:
            timed_out = True
            break
        time.sleep(delay)

    elapsed_seconds = max(0.0, time.monotonic() - started_at)
    summary.update(
        {
            "wait_requested_seconds": requested_seconds,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "poll_count": poll_count,
            "timed_out": timed_out,
        }
    )

    current_work = summary.get("work")
    work_reference = None
    if isinstance(current_work, dict):
        work_reference = WorkReference(
            type="job",
            id=current_work["id"],
            state=current_work["state"],
            progress=current_work["progress"],
            detail=current_work["detail"],
            cancellable=summary["cancellable"],
            poll_after_ms=current_work["poll_after_ms"],
        )

    return ToolOutcome(
        result=summary,
        work=work_reference,
        next_actions=_next_actions(
            summary,
            include_coverage=arguments.include_coverage,
            wait_seconds=requested_seconds,
        ),
    )


def get_generation_run_coverage(
    runtime: McpRuntime,
    arguments: GetGenerationRunCoverageInput,
) -> ToolOutcome:
    application = cast(
        _GenerationOperationApplication,
        runtime.require_application(),
    )
    payload = application.get_generation_run_coverage(
        arguments.generation_run_id,
        audio_version_side=arguments.audio_version_side,
    )
    coverage = _coverage_summary(payload)
    if coverage is None:
        raise PandratorMcpError(
            "internal_error",
            "The application returned an invalid generation coverage response.",
            details={"generation_run_id": arguments.generation_run_id},
        )
    return ToolOutcome(result={"schema_version": 1, **coverage})


def register_generation_operation_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    response,
    *,
    read_only,
) -> None:
    """Register the two fixed, read-only typed generation tools."""

    def register(model, handler, name: str, title: str, description: str) -> None:
        def invoke(**values) -> dict[str, Any]:
            response_mode = values.pop("response_mode", "standard")
            envelope = validated_call(handler, runtime, model, values)
            return response(envelope, response_mode)

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
        response_annotation = Literal["standard", "structured"]
        annotations["response_mode"] = response_annotation
        parameters.append(
            inspect.Parameter(
                "response_mode",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=response_annotation,
                default="standard",
            )
        )
        invoke.__name__ = name
        invoke.__doc__ = description
        invoke.__annotations__ = annotations
        invoke.__signature__ = inspect.Signature(
            parameters,
            return_annotation=dict[str, Any],
        )
        server.tool(name=name, title=title, annotations=read_only)(invoke)

    register(
        GetGenerationOperationInput,
        get_generation_operation,
        "pandrator_get_generation_operation",
        "Get a generation operation",
        "Poll a stable generation operation and return only its safe current phase projection and optional terminal audio coverage.",
    )
    register(
        GetGenerationRunCoverageInput,
        get_generation_run_coverage,
        "pandrator_get_generation_run_coverage",
        "Get generation run audio coverage",
        "Inspect selected-run audio availability without returning speech text or file paths.",
    )


__all__ = [
    "get_generation_operation",
    "get_generation_run_coverage",
    "register_generation_operation_tools",
]
