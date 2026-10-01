"""Passive pull, lease, and submit handlers for subtitle dispatch runs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ClaimDispatchBatchInput,
    CreateDispatchRunInput,
    GetDispatchRunInput,
    InspectDispatchSplitBoundariesInput,
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
    "correction_style",
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
_COMPACT_MANIFEST_KEYS = (
    "instructions",
    "result_contract",
    "quality_policy",
    "kind",
    "output_role",
    "source_language",
    "target_language",
    "no_remove_subtitles",
    "correction_style",
    "timing_context_mode",
    "substantial_gap_ms",
)
_TASK_KEYS = (
    "session_id",
    "kind",
    "output_role",
    "source_artifact_id",
    "source_content_hash",
    "source_language",
    "target_language",
    "instructions",
    "result_contract",
    "no_remove_subtitles",
    "correction_style",
    "known_speakers",
    "glossary",
    "timing_context_mode",
    "substantial_gap_ms",
    "quality_policy",
)
_COMPACT_TASK_KEYS = tuple(key for key in _TASK_KEYS if key not in _COMPACT_MANIFEST_KEYS)
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
_CUE_KEYS = (
    "cue_id",
    "evidence_cue_ids",
    "text",
    "speaker",
    "turn_id",
    "timing_basis",
)
_BOUNDARY_CUE_KEYS = ("text", "speaker")
_TIMING_KEYS = (
    "start_ms",
    "end_ms",
    "gap_from_previous_ms",
    "overlap_with_previous_ms",
    "timing_basis",
)
_COMPACT_CUE_COLUMNS = (
    "cue_id",
    "evidence_cue_ids",
    "text",
    "speaker",
    "turn_index",
    "start_ms",
    "end_ms",
    "gap_from_previous_ms",
    "overlap_with_previous_ms",
    "timing_basis",
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


def _canonical_sha256(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _turn_table(cues: list[dict[str, Any]]) -> tuple[list[Any], list[int | None]]:
    turns: list[Any] = []
    indexes: dict[str, int] = {}
    cue_indexes: list[int | None] = []
    for cue in cues:
        turn_id = cue.get("turn_id")
        if turn_id is None:
            cue_indexes.append(None)
            continue
        key = json.dumps(
            turn_id,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if key not in indexes:
            indexes[key] = len(turns)
            turns.append(turn_id)
        cue_indexes.append(indexes[key])
    return turns, cue_indexes


def _compact_claim(
    projected: dict[str, Any],
    *,
    known_manifest_hash: str | None,
) -> dict[str, Any]:
    """Encode the canonical claim projection in the versioned compact form."""

    result = dict(projected)
    result["packet_format"] = "compact-v1"

    task = projected.get("task")
    manifest = _project_fields(task, _COMPACT_MANIFEST_KEYS) if isinstance(task, dict) else {}
    manifest_hash = _canonical_sha256(manifest)
    result["manifest_hash"] = manifest_hash
    if known_manifest_hash != manifest_hash:
        result["manifest"] = manifest
    if isinstance(task, dict):
        result["task"] = _project_fields(task, _COMPACT_TASK_KEYS)

    batch = projected.get("batch")
    if isinstance(batch, dict):
        compact_batch = dict(batch)
        cues = batch.get("cues")
        if isinstance(cues, list):
            turns, turn_indexes = _turn_table(cues)
            rows: list[list[Any]] = []
            for cue, turn_index in zip(cues, turn_indexes, strict=True):
                timing = cue.get("timing")
                timing = timing if isinstance(timing, dict) else {}
                rows.append(
                    [
                        cue.get("cue_id"),
                        cue.get("evidence_cue_ids"),
                        cue.get("text"),
                        cue.get("speaker"),
                        turn_index,
                        timing.get("start_ms"),
                        timing.get("end_ms"),
                        timing.get("gap_from_previous_ms"),
                        timing.get("overlap_with_previous_ms"),
                        (
                            cue.get("timing_basis")
                            if cue.get("timing_basis") is not None
                            else timing.get("timing_basis")
                        ),
                    ]
                )
            compact_batch.pop("cues", None)
            compact_batch["cue_columns"] = list(_COMPACT_CUE_COLUMNS)
            compact_batch["cue_rows"] = rows
            compact_batch["turns"] = turns
        result["batch"] = compact_batch
    return result


def _lease(payload: dict[str, Any]) -> dict[str, Any]:
    result = {"schema_version": "1"}
    result.update(_project_fields(payload, _LEASE_KEYS))
    return result


def _submission(payload: dict[str, Any]) -> dict[str, Any]:
    result = {"schema_version": "1"}
    result.update(_project_fields(payload, _SUBMIT_KEYS))
    return result


def _native_submit_result(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if result.kind == "correction":
        grouped_fields = {"edits", "deletes", "merges", "splits"}
        grouped = bool(grouped_fields.intersection(result.model_fields_set))
        operations: list[dict[str, Any]] = []
        if grouped:
            for edit in result.edits:
                operation: dict[str, Any] = {
                    "action": "edit",
                    "cue_ids": [edit.cue_id],
                    "texts": [edit.text],
                    "starts_new_turn": edit.starts_new_turn,
                }
                if edit.speaker is not None:
                    operation["speakers"] = [edit.speaker]
                operations.append(operation)
            operations.extend(
                {"action": "delete", "cue_ids": [cue_id]} for cue_id in result.deletes
            )
            for merge in result.merges:
                operation = merge.model_dump(mode="json")
                operation["action"] = "merge"
                operations.append(operation)
            for split in result.splits:
                operation = split.model_dump(mode="json")
                operation["action"] = "split"
                operation["cue_ids"] = [operation.pop("cue_id")]
                operations.append(operation)
        else:
            operations = [operation.model_dump(mode="json") for operation in result.operations]
        native = {
            "kind": "correction",
            "operations": operations,
            "uncertainties": [
                uncertainty.model_dump(mode="json") for uncertainty in result.uncertainties
            ],
        }
        return (
            type(result)
            .model_validate(native)
            .model_dump(
                mode="json",
                include={"kind", "operations", "uncertainties"},
            )
        )
    if result.kind == "translation":
        translations = (
            [item.model_dump(mode="json") for item in result.translations]
            if result.translations is not None
            else [item.model_dump(mode="json") for item in result.items]
        )
        native = {
            "kind": "translation",
            "translations": translations,
            "glossary_updates": result.glossary_updates,
        }
        return (
            type(result)
            .model_validate(native)
            .model_dump(
                mode="json",
                exclude={"items"},
            )
        )
    return result.model_dump(mode="json")


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
        correction_style=arguments.correction_style,
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


def inspect_dispatch_split_boundaries(
    runtime: McpRuntime, arguments: InspectDispatchSplitBoundariesInput
) -> dict[str, Any]:
    return runtime.require_application().inspect_dispatch_split_boundaries(**arguments.model_dump())


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
    if arguments.packet_format == "compact":
        projected = _compact_claim(
            projected,
            known_manifest_hash=arguments.known_manifest_hash,
        )
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
        result=_native_submit_result(arguments.result),
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
