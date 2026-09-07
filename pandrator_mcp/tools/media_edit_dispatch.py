"""Passive whole-recording media-edit dispatch handlers."""

from __future__ import annotations

import re
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ClaimMediaEditDispatchBatchInput,
    CreateMediaEditDispatchRunInput,
    GetMediaEditDispatchRunInput,
    ListMediaEditDispatchRunsInput,
    ReleaseMediaEditDispatchBatchInput,
    RenewMediaEditDispatchBatchInput,
    SubmitMediaEditDispatchBatchInput,
)

_SAFE_ACTION_ID = re.compile(r"[^A-Za-z0-9._:-]")


def _action_key(prefix: str, identifier: str) -> str:
    safe = _SAFE_ACTION_ID.sub("-", identifier).strip("-") or "run"
    return f"media-edit-{prefix}:{safe}"[:200]


def _get_action(run_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_media_edit_dispatch_run",
        arguments={"run_id": run_id},
        reason="Inspect durable passive media-edit finalization state.",
    )


def _claim_action(run_id: str, sequence: str) -> NextAction:
    return NextAction(
        tool="pandrator_claim_media_edit_dispatch_batch",
        arguments={
            "run_id": run_id,
            "lease_seconds": 900,
            "idempotency_key": _action_key(f"claim:{sequence}", run_id),
        },
        reason="Claim the single whole-recording cue-evidence batch.",
    )


def _retry_submit_action(
    batch_id: str,
    lease_token: str,
    result: dict[str, Any],
    submission_key: str,
) -> NextAction:
    return NextAction(
        tool="pandrator_submit_media_edit_dispatch_batch",
        arguments={
            "batch_id": batch_id,
            "lease_token": lease_token,
            "result": result,
            "idempotency_key": submission_key,
        },
        reason=(
            "The proposal was accepted but its revision was not materialized. "
            "Retry this exact submission to resume finalization."
        ),
    )


def _renew_action(batch_id: str, lease_token: str) -> NextAction:
    return NextAction(
        tool="pandrator_renew_media_edit_dispatch_batch",
        arguments={
            "batch_id": batch_id,
            "lease_token": lease_token,
            "lease_seconds": 900,
            "idempotency_key": _action_key("renew", batch_id),
        },
        reason="Renew the lease if global media-edit reasoning needs more time.",
    )


def _release_action(batch_id: str, lease_token: str) -> NextAction:
    return NextAction(
        tool="pandrator_release_media_edit_dispatch_batch",
        arguments={
            "batch_id": batch_id,
            "lease_token": lease_token,
            "idempotency_key": _action_key("release", batch_id),
        },
        reason="Release the batch when the agent cannot complete its global reasoning.",
    )


def _run_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "run_id",
        "session_id",
        "kind",
        "source_revision_id",
        "source_revision",
        "source_revision_number",
        "source_content_hash",
        "instructions",
        "input_hash",
        "status",
        "batch_count",
        "total_batches",
        "completed_batch_count",
        "completed_batches",
        "accepted_batch_count",
        "remaining_batch_count",
        "result_revision_id",
        "result_revision",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
    )
    return {"schema_version": "1", **{key: payload[key] for key in keys if key in payload}}


def _claim(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": "1"}
    for key in (
        "run_id",
        "batch_id",
        "batch_ordinal",
        "status",
        "run_status",
        "batch_status",
        "source_revision",
        "lease_token",
        "lease_expires_at",
        "task",
        "batch",
    ):
        if key in payload:
            result[key] = payload[key]
    return result


def create_media_edit_dispatch_run(
    runtime: McpRuntime,
    arguments: CreateMediaEditDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().create_media_edit_dispatch_run(
        arguments.session_id,
        revision=arguments.revision,
        instructions=arguments.instructions,
        idempotency_key=arguments.idempotency_key,
    )
    run_id = str(result.get("id") or result.get("run_id") or "")
    return ToolOutcome(
        result=_run_metadata(result),
        next_actions=[_claim_action(run_id, "0")] if run_id else [],
    )


def list_media_edit_dispatch_runs(
    runtime: McpRuntime,
    arguments: ListMediaEditDispatchRunsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_media_edit_dispatch_runs(
        arguments.session_id, limit=arguments.limit
    )
    items = payload.get("items")
    return {
        "schema_version": "1",
        "items": [_run_metadata(item) for item in items if isinstance(item, dict)]
        if isinstance(items, list)
        else [],
    }


def get_media_edit_dispatch_run(
    runtime: McpRuntime,
    arguments: GetMediaEditDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().get_media_edit_dispatch_run(arguments.run_id)
    state = str(result.get("status") or "").lower()
    actions: list[NextAction] = []
    if state in {"ready", "running"}:
        actions.append(_claim_action(arguments.run_id, "0"))
    elif state == "finalizing":
        actions.append(_get_action(arguments.run_id))
    elif state == "completed" and result.get("result_revision_id"):
        result_revision = result.get("result_revision")
        if result_revision is None:
            try:
                result_revision = (
                    int(result.get("source_revision_number") or result.get("source_revision")) + 1
                )
            except (TypeError, ValueError):
                result_revision = None
        if result_revision is not None:
            actions.append(
                NextAction(
                    tool="pandrator_list_media_edit_cuts",
                    arguments={
                        "session_id": result.get("session_id"),
                        "revision": result_revision,
                    },
                    reason="List the newly materialized unreviewed cut topology before boundary inspection and approval.",
                )
            )
        else:
            actions.append(_get_action(arguments.run_id))
    return ToolOutcome(result=_run_metadata(result), next_actions=actions)


def claim_media_edit_dispatch_batch(
    runtime: McpRuntime,
    arguments: ClaimMediaEditDispatchBatchInput,
) -> ToolOutcome:
    result = _claim(
        runtime.require_application().claim_media_edit_dispatch_batch(
            arguments.run_id,
            lease_seconds=arguments.lease_seconds,
            idempotency_key=arguments.idempotency_key,
        )
    )
    batch_id = str(result.get("batch_id") or "")
    lease_token = str(result.get("lease_token") or "")
    actions = (
        [
            _renew_action(batch_id, lease_token),
            _release_action(batch_id, lease_token),
        ]
        if batch_id and lease_token
        else []
    )
    return ToolOutcome(result=result, next_actions=actions)


def renew_media_edit_dispatch_batch(
    runtime: McpRuntime,
    arguments: RenewMediaEditDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().renew_media_edit_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        lease_seconds=arguments.lease_seconds,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def release_media_edit_dispatch_batch(
    runtime: McpRuntime,
    arguments: ReleaseMediaEditDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().release_media_edit_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def submit_media_edit_dispatch_batch(
    runtime: McpRuntime,
    arguments: SubmitMediaEditDispatchBatchInput,
) -> ToolOutcome:
    result = runtime.require_application().submit_media_edit_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        result=arguments.result.model_dump(mode="json"),
        idempotency_key=arguments.idempotency_key,
    )
    run_id = str(result.get("run_id") or "")
    state = str(result.get("status") or result.get("run_status") or "").lower()
    if state == "completed" and result.get("session_id"):
        result_revision = result.get("result_revision")
        if result_revision is None:
            try:
                result_revision = (
                    int(result.get("source_revision_number") or result.get("source_revision")) + 1
                )
            except (TypeError, ValueError):
                result_revision = None
        actions = (
            [
                NextAction(
                    tool="pandrator_list_media_edit_cuts",
                    arguments={
                        "session_id": result["session_id"],
                        "revision": result_revision,
                    },
                    reason="List the newly materialized unreviewed cut topology before boundary inspection and approval.",
                )
            ]
            if result_revision is not None
            else [_get_action(run_id)]
            if run_id
            else []
        )
    elif state == "finalizing":
        actions = [
            _retry_submit_action(
                arguments.batch_id,
                arguments.lease_token,
                arguments.result.model_dump(mode="json"),
                arguments.idempotency_key,
            )
        ]
    else:
        actions = [_get_action(run_id)] if run_id and state == "failed" else []
    return ToolOutcome(result={"schema_version": "1", **result}, next_actions=actions)
