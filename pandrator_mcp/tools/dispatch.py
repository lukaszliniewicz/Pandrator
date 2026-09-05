"""Passive pull, lease, and submit handlers for subtitle dispatch runs."""

from __future__ import annotations

import re
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ClaimDispatchBatchInput,
    CreateDispatchRunInput,
    GetDispatchRunInput,
    ListDispatchRunsInput,
    ReleaseDispatchBatchInput,
    RenewDispatchBatchInput,
    SubmitDispatchBatchInput,
)

_RUN_METADATA_KEYS = (
    "id",
    "run_id",
    "session_id",
    "kind",
    "output_role",
    "status",
    "state",
    "source_artifact_id",
    "source_revision_id",
    "source_content_hash",
    "source_language",
    "target_language",
    "execution_mode",
    "max_parallel_batches",
    "char_limit",
    "max_segments_per_batch",
    "no_remove_subtitles",
    "context_before",
    "context_after",
    "timing_context_mode",
    "substantial_gap_ms",
    "batch_count",
    "total_batches",
    "completed_batch_count",
    "accepted_batch_count",
    "remaining_batch_count",
    "total_segments",
    "processed_segments",
    "created_at",
    "updated_at",
    "completed_at",
    "finalized_at",
    "final_artifact_id",
    "output_artifact_id",
    "result_artifact_id",
    "artifact_id",
    "finalized",
    "finalization_status",
    "error_code",
    "error_message",
    "message",
)
_BATCH_METADATA_KEYS = (
    "id",
    "batch_id",
    "batch_ordinal",
    "status",
    "run_status",
    "batch_status",
    "state",
    "lease_expires_at",
    "accepted_at",
)
_LIST_METADATA_KEYS = (
    "total",
    "has_more",
    "next_cursor",
    "next_before",
)
_CLAIM_KEYS = (
    "schema_version",
    "run_id",
    "batch_id",
    "batch_ordinal",
    "status",
    "lease_token",
    "lease_expires_at",
    "task",
    "batch",
    "delegation",
)
_TASK_KEYS = (
    "session_id",
    "kind",
    "output_role",
    "source_artifact_id",
    "source_language",
    "target_language",
    "instructions",
    "result_contract",
    "no_remove_subtitles",
    "known_speakers",
    "glossary",
    "timing_context_mode",
    "substantial_gap_ms",
    "quality_policy",
)
_DELEGATION_KEYS = (
    "execution_mode",
    "max_parallel_batches",
    "wave_number",
    "wave_batch_count",
)
_CONTEXT_CAPSULE_KEYS = (
    "overview",
    "terminology",
    "entities",
    "style_rules",
    "decisions",
    "notes",
)
_CLAIMED_BATCH_KEYS = (
    "id_namespace",
    "source_revision_id",
    "cue_count",
    "valid_cue_ids",
)
_CUE_KEYS = ("cue_id", "text", "speaker")
_BOUNDARY_CUE_KEYS = ("text", "speaker")
_TIMING_KEYS = (
    "start_ms",
    "end_ms",
    "gap_from_previous_ms",
    "overlap_with_previous_ms",
)
_LEASE_KEYS = (
    "batch_id",
    "run_id",
    "lease_token",
    "lease_expires_at",
    "expires_at",
    "expiry",
    "status",
    "state",
    "released",
    "renewed",
    "message",
)
_SUBMIT_KEYS = (
    "batch_id",
    "run_id",
    "output_role",
    "status",
    "state",
    "run_status",
    "accepted",
    "rejected",
    "validation_errors",
    "errors",
    "reason",
    "message",
    "remaining_batches",
    "completed_batches",
    "completed_batch_count",
    "total_batches",
    "batch_count",
    "next_batch_id",
    "result_artifact_id",
    "result_revision_id",
    "final_artifact_id",
    "output_artifact_id",
    "finalized",
    "finalization_status",
    "error_code",
    "error_message",
)
_SAFE_ACTION_ID = re.compile(r"[^A-Za-z0-9._:-]")


def _project_fields(
    payload: dict[str, Any],
    keys: tuple[str, ...],
) -> dict[str, Any]:
    return {key: payload[key] for key in keys if key in payload}


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a run response without instructions, glossary, or batch text."""

    result: dict[str, Any] = {"schema_version": "1"}
    result.update(_project_fields(payload, _RUN_METADATA_KEYS))
    batches = payload.get("batches")
    if isinstance(batches, list):
        result["batches"] = [
            _project_fields(item, _BATCH_METADATA_KEYS)
            for item in batches[:500]
            if isinstance(item, dict)
        ]
    return result


def _list_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    result: dict[str, Any] = {
        "schema_version": "1",
        "items": [_metadata(item) for item in items[:100] if isinstance(item, dict)]
        if isinstance(items, list)
        else [],
    }
    result.update(_project_fields(payload, _LIST_METADATA_KEYS))
    return result


def _claim(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the exact canonical claim packet and nothing else."""

    result: dict[str, Any] = {"schema_version": "1"}
    for key in _CLAIM_KEYS:
        if key not in payload or key in {"task", "batch", "delegation"}:
            continue
        result[key] = payload[key]
    task = payload.get("task")
    if isinstance(task, dict):
        result["task"] = _project_fields(task, _TASK_KEYS)
    batch = payload.get("batch")
    if isinstance(batch, dict):
        projected_batch = _project_fields(batch, _CLAIMED_BATCH_KEYS)
        cues = batch.get("cues")
        if isinstance(cues, list):
            projected_cues: list[dict[str, Any]] = []
            for cue in cues[:500]:
                if not isinstance(cue, dict):
                    continue
                projected_cue = _project_fields(cue, _CUE_KEYS)
                timing = cue.get("timing")
                if isinstance(timing, dict):
                    projected_cue["timing"] = _project_fields(timing, _TIMING_KEYS)
                projected_cues.append(projected_cue)
            projected_batch["cues"] = projected_cues
        context = batch.get("context")
        if isinstance(context, dict):
            projected_context: dict[str, list[dict[str, Any]]] = {}
            for context_key in (
                "previous_output",
                "previous_source",
                "following_source",
            ):
                values = context.get(context_key)
                projected_context[context_key] = (
                    [
                        _project_fields(item, _BOUNDARY_CUE_KEYS)
                        for item in values[:20]
                        if isinstance(item, dict)
                    ]
                    if isinstance(values, list)
                    else []
                )
            projected_batch["context"] = projected_context
        result["batch"] = projected_batch
    delegation = payload.get("delegation")
    if isinstance(delegation, dict):
        projected_delegation = _project_fields(delegation, _DELEGATION_KEYS)
        capsule = delegation.get("context_capsule")
        if isinstance(capsule, dict):
            projected_delegation["context_capsule"] = _project_fields(
                capsule,
                _CONTEXT_CAPSULE_KEYS,
            )
        result["delegation"] = projected_delegation
    return result


def _lease(payload: dict[str, Any]) -> dict[str, Any]:
    result = {"schema_version": "1"}
    result.update(_project_fields(payload, _LEASE_KEYS))
    return result


def _submission(payload: dict[str, Any]) -> dict[str, Any]:
    result = {"schema_version": "1"}
    result.update(_project_fields(payload, _SUBMIT_KEYS))
    return result


def _run_id(payload: dict[str, Any], fallback: str | None = None) -> str:
    value = payload.get("run_id") or payload.get("id") or fallback or ""
    return str(value).strip()


def _action_key(prefix: str, identifier: str) -> str:
    safe = _SAFE_ACTION_ID.sub("-", identifier).strip("-") or "run"
    return f"dispatch-{prefix}:{safe}"


def _claim_next_action(
    run_id: str,
    *,
    sequence: str = "next",
) -> NextAction:
    return NextAction(
        tool="pandrator_claim_dispatch_batch",
        arguments={
            "run_id": run_id,
            "lease_seconds": 900,
            "idempotency_key": _action_key(f"claim:{sequence}", run_id),
        },
        reason=(
            "Pull the next available batch. Keep its lease token scoped to "
            "the matching batch renew, release, or submit call."
        ),
    )


def _get_next_action(run_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_dispatch_run",
        arguments={"run_id": run_id},
        reason="Inspect the completed run metadata and final artifact.",
    )


def _status(payload: dict[str, Any]) -> str:
    value = payload.get("status") or payload.get("state") or ""
    return str(value).strip().lower().replace("-", "_")


def _is_completed(payload: dict[str, Any]) -> bool:
    state = (
        payload.get("run_status")
        if payload.get("run_status") is not None
        else payload.get("status") or payload.get("state")
    )
    return bool(payload.get("completed") or payload.get("finalized")) or str(
        state or ""
    ).strip().lower().replace("-", "_") in {
        "complete",
        "completed",
        "finalized",
        "finished",
    }


def _is_accepted(payload: dict[str, Any]) -> bool:
    return bool(payload.get("accepted")) or _status(payload) in {
        "accepted",
        "submitted",
        "completed",
    }


def create_dispatch_run(
    runtime: McpRuntime,
    arguments: CreateDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().create_dispatch_run(
        arguments.session_id,
        kind=arguments.kind,
        source_artifact_id=arguments.source_artifact_id,
        source_language=arguments.source_language,
        target_language=arguments.target_language,
        instructions=arguments.instructions,
        char_limit=arguments.char_limit,
        max_segments_per_batch=arguments.max_segments_per_batch,
        no_remove_subtitles=arguments.no_remove_subtitles,
        context_before=arguments.context_before,
        context_after=arguments.context_after,
        timing_context_mode=arguments.timing_context_mode,
        substantial_gap_ms=arguments.substantial_gap_ms,
        glossary=arguments.glossary,
        execution_mode=arguments.execution_mode,
        max_parallel_batches=arguments.max_parallel_batches,
        context_capsule=arguments.context_capsule.model_dump(mode="json"),
        idempotency_key=arguments.idempotency_key,
    )
    run_id = _run_id(result)
    next_actions = [_claim_next_action(run_id, sequence="first")] if run_id else []
    return ToolOutcome(
        result=_metadata(result),
        next_actions=next_actions,
    )


def list_dispatch_runs(
    runtime: McpRuntime,
    arguments: ListDispatchRunsInput,
) -> dict[str, Any]:
    return _list_metadata(
        runtime.require_application().list_dispatch_runs(
            arguments.session_id,
            limit=arguments.limit,
        )
    )


def get_dispatch_run(
    runtime: McpRuntime,
    arguments: GetDispatchRunInput,
) -> dict[str, Any]:
    return _metadata(runtime.require_application().get_dispatch_run(arguments.run_id))


def claim_dispatch_batch(
    runtime: McpRuntime,
    arguments: ClaimDispatchBatchInput,
) -> ToolOutcome:
    result = runtime.require_application().claim_dispatch_batch(
        arguments.run_id,
        lease_seconds=arguments.lease_seconds,
        idempotency_key=arguments.idempotency_key,
    )
    projected = _claim(result)
    next_actions = []
    if _is_completed(result):
        next_actions.append(_get_next_action(arguments.run_id))
    elif _status({"status": result.get("batch_status")}) == "completed":
        next_actions.append(
            _claim_next_action(
                arguments.run_id,
                sequence=str(result.get("batch_id") or "completed"),
            )
        )
    return ToolOutcome(result=projected, next_actions=next_actions)


def renew_dispatch_batch(
    runtime: McpRuntime,
    arguments: RenewDispatchBatchInput,
) -> dict[str, Any]:
    return _lease(
        runtime.require_application().renew_dispatch_batch(
            arguments.batch_id,
            lease_token=arguments.lease_token,
            lease_seconds=arguments.lease_seconds,
            idempotency_key=arguments.idempotency_key,
        )
    )


def release_dispatch_batch(
    runtime: McpRuntime,
    arguments: ReleaseDispatchBatchInput,
) -> dict[str, Any]:
    return _lease(
        runtime.require_application().release_dispatch_batch(
            arguments.batch_id,
            lease_token=arguments.lease_token,
            idempotency_key=arguments.idempotency_key,
        )
    )


def submit_dispatch_batch(
    runtime: McpRuntime,
    arguments: SubmitDispatchBatchInput,
) -> ToolOutcome:
    result = runtime.require_application().submit_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        result=(arguments.result.model_dump(mode="json") if arguments.result is not None else None),
        response_text=arguments.response_text,
        context_delta=arguments.context_delta.model_dump(mode="json"),
        idempotency_key=arguments.idempotency_key,
    )
    projected = _submission(result)
    run_id = _run_id(result)
    next_actions: list[NextAction] = []
    if _is_completed(result) and run_id:
        next_actions.append(_get_next_action(run_id))
    elif _is_accepted(result) and run_id:
        next_actions.append(
            _claim_next_action(
                run_id,
                sequence=arguments.batch_id,
            )
        )
    return ToolOutcome(result=projected, next_actions=next_actions)
