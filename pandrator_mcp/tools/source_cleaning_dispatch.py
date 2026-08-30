"""Passive PDF/EPUB source-cleaning dispatch handlers."""

from __future__ import annotations

import re
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ClaimSourceCleaningDispatchBatchInput,
    CreateSourceCleaningDispatchRunInput,
    GetSourceCleaningDispatchRunInput,
    InspectSourceCleaningDispatchExtractionInput,
    ListSourceCleaningDispatchRunsInput,
    ReleaseSourceCleaningDispatchBatchInput,
    RenewSourceCleaningDispatchBatchInput,
    SubmitSourceCleaningDispatchBatchInput,
)

_SAFE_ACTION_ID = re.compile(r"[^A-Za-z0-9._:-]")
_RUN_KEYS = (
    "id",
    "run_id",
    "session_id",
    "kind",
    "source_artifact_id",
    "source_format",
    "source_content_hash",
    "job_id",
    "status",
    "batch_count",
    "total_batches",
    "completed_batch_count",
    "accepted_batch_count",
    "remaining_batch_count",
    "accepted_operation_count",
    "rejected_proposal_count",
    "baseline_artifact_id",
    "index_artifact_id",
    "result_artifact_id",
    "final_artifact_id",
    "finalized",
    "requires_review",
    "validation",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
)
_BATCH_KEYS = (
    "id",
    "batch_id",
    "batch_ordinal",
    "phase",
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
    "accepted_operation_count",
    "rejected_proposal_count",
    "result_artifact_id",
    "final_artifact_id",
    "finalized",
    "requires_review",
    "validation",
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


def _status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or payload.get("run_status") or "").strip().lower()


def _run_id(payload: dict[str, Any], fallback: str = "") -> str:
    return str(payload.get("run_id") or payload.get("id") or fallback).strip()


def _action_key(prefix: str, identifier: str) -> str:
    safe = _SAFE_ACTION_ID.sub("-", identifier).strip("-") or "run"
    return f"source-cleaning-{prefix}:{safe}"[:200]


def _get_action(run_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_source_cleaning_dispatch_run",
        arguments={"run_id": run_id},
        reason=(
            "Inspect durable preparation or finalization state. Preparation may "
            "include local PDF OCR and does not call a model provider."
        ),
    )


def _claim_action(run_id: str, sequence: str) -> NextAction:
    return NextAction(
        tool="pandrator_claim_source_cleaning_dispatch_batch",
        arguments={
            "run_id": run_id,
            "lease_seconds": 900,
            "idempotency_key": _action_key(f"claim:{sequence}", run_id),
        },
        reason=(
            "Claim the next editorial phase packet. Its initial evidence is bounded; "
            "use the lease-scoped extraction inspection tool when more evidence is "
            "needed, and keep the lease token scoped to that batch."
        ),
    )


def _workflow_action(session_id: str) -> NextAction:
    return NextAction(
        tool="pandrator_get_workflow",
        arguments={"session_id": session_id},
        reason=("Inspect the selected clean_text artifact and plan narration preparation."),
    )


def create_source_cleaning_dispatch_run(
    runtime: McpRuntime,
    arguments: CreateSourceCleaningDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().create_source_cleaning_dispatch_run(
        arguments.session_id,
        source_artifact_id=arguments.source_artifact_id,
        instructions=arguments.instructions,
        evidence_limit=arguments.evidence_limit,
        remove_footnotes=arguments.remove_footnotes,
        filter_citations=arguments.filter_citations,
        pdf_ocr_mode=arguments.pdf_ocr_mode,
        pdf_ocr_language=arguments.pdf_ocr_language,
        pdf_ocr_dpi=arguments.pdf_ocr_dpi,
        pdf_remove_toc=arguments.pdf_remove_toc,
        pdf_remove_repeated_marginals=arguments.pdf_remove_repeated_marginals,
        idempotency_key=arguments.idempotency_key,
    )
    run_id = _run_id(result)
    return ToolOutcome(
        result=_metadata(result),
        next_actions=[_get_action(run_id)] if run_id else [],
    )


def list_source_cleaning_dispatch_runs(
    runtime: McpRuntime,
    arguments: ListSourceCleaningDispatchRunsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_source_cleaning_dispatch_runs(
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


def get_source_cleaning_dispatch_run(
    runtime: McpRuntime,
    arguments: GetSourceCleaningDispatchRunInput,
) -> ToolOutcome:
    result = runtime.require_application().get_source_cleaning_dispatch_run(arguments.run_id)
    state = _status(result)
    next_actions: list[NextAction] = []
    if state in {"ready", "running"}:
        next_actions.append(
            _claim_action(
                arguments.run_id,
                str(result.get("completed_batch_count") or 0),
            )
        )
    elif state in {"preparing", "finalizing"}:
        next_actions.append(_get_action(arguments.run_id))
    elif state == "completed" and str(result.get("session_id") or ""):
        next_actions.append(_workflow_action(str(result["session_id"])))
    return ToolOutcome(result=_metadata(result), next_actions=next_actions)


def claim_source_cleaning_dispatch_batch(
    runtime: McpRuntime,
    arguments: ClaimSourceCleaningDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().claim_source_cleaning_dispatch_batch(
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


def renew_source_cleaning_dispatch_batch(
    runtime: McpRuntime,
    arguments: RenewSourceCleaningDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().renew_source_cleaning_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        lease_seconds=arguments.lease_seconds,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def release_source_cleaning_dispatch_batch(
    runtime: McpRuntime,
    arguments: ReleaseSourceCleaningDispatchBatchInput,
) -> dict[str, Any]:
    payload = runtime.require_application().release_source_cleaning_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def inspect_source_cleaning_dispatch_extraction(
    runtime: McpRuntime,
    arguments: InspectSourceCleaningDispatchExtractionInput,
) -> dict[str, Any]:
    payload = runtime.require_application().inspect_source_cleaning_dispatch_extraction(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        action=arguments.action,
        arguments=arguments.arguments,
        view=arguments.view,
        idempotency_key=arguments.idempotency_key,
    )
    return {"schema_version": "1", **payload}


def submit_source_cleaning_dispatch_batch(
    runtime: McpRuntime,
    arguments: SubmitSourceCleaningDispatchBatchInput,
) -> ToolOutcome:
    result = runtime.require_application().submit_source_cleaning_dispatch_batch(
        arguments.batch_id,
        lease_token=arguments.lease_token,
        result=arguments.result.model_dump(mode="json"),
        idempotency_key=arguments.idempotency_key,
    )
    projected = {"schema_version": "1", **_fields(result, _SUBMIT_KEYS)}
    run_id = _run_id(result)
    next_actions: list[NextAction] = []
    if run_id and result.get("finalized"):
        next_actions.append(_get_action(run_id))
    elif run_id and result.get("accepted"):
        next_actions.append(_claim_action(run_id, arguments.batch_id))
    return ToolOutcome(result=projected, next_actions=next_actions)
