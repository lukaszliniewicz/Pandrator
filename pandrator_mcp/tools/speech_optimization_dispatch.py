"""Passive speech-text optimization dispatch handlers."""

from __future__ import annotations

import re
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ClaimSpeechOptimizationDispatchBatchInput,
    CreateSpeechOptimizationDispatchRunInput,
    GetSpeechOptimizationDispatchRunInput,
    ListSpeechOptimizationDispatchRunsInput,
    ReleaseSpeechOptimizationDispatchBatchInput,
    RenewSpeechOptimizationDispatchBatchInput,
    SubmitSpeechOptimizationDispatchBatchInput,
)

_SAFE_ACTION_ID = re.compile(r"[^A-Za-z0-9._:-]")
_RUN_KEYS = (
    "id",
    "run_id",
    "session_id",
    "kind",
    "output_role",
    "source_artifact_id",
    "source_format",
    "source_content_hash",
    "language",
    "voice_language",
    "tts_service",
    "instructions",
    "char_limit",
    "max_units_per_batch",
    "context_before",
    "context_after",
    "include_timing",
    "status",
    "batch_count",
    "total_batches",
    "completed_batch_count",
    "accepted_batch_count",
    "remaining_batch_count",
    "result_artifact_id",
    "final_artifact_id",
    "result_revision_id",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
)
_BATCH_KEYS = (
    "id",
    "batch_id",
    "batch_ordinal",
    "status",
    "lease_expires_at",
    "accepted_at",
)
_SUBMIT_KEYS = (
    "run_id",
    "batch_id",
    "output_role",
    "status",
    "run_status",
    "batch_status",
    "accepted",
    "completed_batch_count",
    "completed_batches",
    "batch_count",
    "total_batches",
    "remaining_batches",
    "result_artifact_id",
    "final_artifact_id",
    "finalized",
    "result_revision_id",
    "error_code",
    "error_message",
)


def _fields(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload[key] for key in keys if key in payload}


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": "1"}
    result.update(_fields(payload, _RUN_KEYS))
    batches = payload.get("batches")
    if isinstance(batches, list):
        result["batches"] = [
            _fields(item, _BATCH_KEYS) for item in batches[:500] if isinstance(item, dict)
        ]
    return result


def _run_id(payload: dict[str, Any], fallback: str = "") -> str:
    return str(payload.get("run_id") or payload.get("id") or fallback).strip()


def _status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or payload.get("run_status") or "").strip().lower()


def _action_key(prefix: str, identifier: str) -> str:
    safe = _SAFE_ACTION_ID.sub("-", identifier).strip("-") or "run"
    return f"speech-optimization-{prefix}:{safe}"[:200]


def _get_action(run_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_speech_optimization_dispatch_run",
        arguments={"run_id": run_id},
        reason="Inspect durable passive speech-optimization and finalization state.",
    )


def _claim_action(run_id: str, sequence: str) -> NextAction:
    return NextAction(
        tool="pandrator_claim_speech_optimization_dispatch_batch",
        arguments={
            "run_id": run_id,
            "lease_seconds": 900,
            "idempotency_key": _action_key(f"claim:{sequence}", run_id),
        },
        reason=(
            "Claim the next sequential speech-text batch, optimize every actionable "
            "unit, and retain the lease token only for that batch."
        ),
    )


def _workflow_action(session_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_workflow",
        arguments={"session_id": session_id},
        reason="Inspect the materialized tts_optimized artifact before generation.",
    )


def create_speech_optimization_dispatch_run(
    runtime: McpRuntime,
    arguments: CreateSpeechOptimizationDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().create_speech_optimization_dispatch_run(
        arguments.session_id,
        source_artifact_id=arguments.source_artifact_id,
        language=arguments.language,
        voice_language=arguments.voice_language,
        tts_service=arguments.tts_service,
        instructions=arguments.instructions,
        char_limit=arguments.char_limit,
        max_units_per_batch=arguments.max_units_per_batch,
        context_before=arguments.context_before,
        context_after=arguments.context_after,
        include_timing=arguments.include_timing,
        idempotency_key=arguments.idempotency_key,
    )
    run_id = _run_id(result)
    return ToolOutcome(
        result=_metadata(result),
        next_actions=[_claim_action(run_id, "0")] if run_id else [],
    )


def list_speech_optimization_dispatch_runs(
    runtime: McpRuntime,
    arguments: ListSpeechOptimizationDispatchRunsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_speech_optimization_dispatch_runs(
        arguments.session_id,
        limit=arguments.limit,
    )
    items = payload.get("items")
    return {
        "schema_version": "1",
        "items": [_metadata(item) for item in items[:100] if isinstance(item, dict)]
        if isinstance(items, list)
        else [],
    }


def get_speech_optimization_dispatch_run(
    runtime: McpRuntime,
    arguments: GetSpeechOptimizationDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().get_speech_optimization_dispatch_run(arguments.run_id)
    state = _status(result)
    next_actions: list[NextAction] = []
    if state in {"ready", "running"}:
        next_actions.append(
            _claim_action(
                arguments.run_id,
                str(result.get("completed_batch_count") or 0),
            )
        )
    elif state == "finalizing":
        next_actions.append(_get_action(arguments.run_id))
    elif state == "completed" and str(result.get("session_id") or ""):
        next_actions.append(_workflow_action(str(result["session_id"])))
    return ToolOutcome(result=_metadata(result), next_actions=next_actions)


def claim_speech_optimization_dispatch_batch(
    runtime: McpRuntime,
    arguments: ClaimSpeechOptimizationDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().claim_speech_optimization_dispatch_batch(
        arguments.run_id,
        lease_seconds=arguments.lease_seconds,
        idempotency_key=arguments.idempotency_key,
    )
    return {
        "schema_version": "1",
        **_fields(
            payload,
            (
                "run_id",
                "batch_id",
                "batch_ordinal",
                "status",
                "run_status",
                "batch_status",
                "lease_token",
                "lease_expires_at",
            ),
        ),
        "task": dict(payload.get("task") or {}),
        "batch": dict(payload.get("batch") or {}),
    }


def renew_speech_optimization_dispatch_batch(
    runtime: McpRuntime,
    arguments: RenewSpeechOptimizationDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().renew_speech_optimization_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        lease_seconds=arguments.lease_seconds,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def release_speech_optimization_dispatch_batch(
    runtime: McpRuntime,
    arguments: ReleaseSpeechOptimizationDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().release_speech_optimization_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def submit_speech_optimization_dispatch_batch(
    runtime: McpRuntime,
    arguments: SubmitSpeechOptimizationDispatchBatchInput,
) -> ToolOutcome:
    result = runtime.require_application().submit_speech_optimization_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        result=arguments.result.model_dump(mode="json"),
        idempotency_key=arguments.idempotency_key,
    )
    projected = {"schema_version": "1", **_fields(result, _SUBMIT_KEYS)}
    run_id = _run_id(result)
    state = _status(result)
    next_actions: list[NextAction] = []
    if run_id and (
        result.get("finalized")
        or state in {"completed", "failed", "finalizing"}
        or result.get("error_code")
    ):
        next_actions.append(_get_action(run_id))
    elif run_id and result.get("accepted"):
        next_actions.append(_claim_action(run_id, arguments.batch_id))
    return ToolOutcome(result=projected, next_actions=next_actions)
