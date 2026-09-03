"""Stable mappings from downstream work records to MCP WorkReference."""

from __future__ import annotations

from typing import Any, Literal, cast

from .schemas.common import WorkReference

WorkState = Literal["queued", "running", "waiting", "succeeded", "failed", "cancelled"]
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _state(value: object) -> WorkState:
    normalized = str(value or "queued").strip().lower()
    if normalized in {"canceled", "cancelled"}:
        return "cancelled"
    if normalized in {
        "cancel_requested",
        "cancelling",
        "rolling_back",
    }:
        return "running"
    if normalized in {
        "planning",
        "awaiting_confirmation",
        "handoff_pending",
        "pending",
    }:
        return "waiting"
    if normalized == "recovery_required":
        return "failed"
    if normalized in {
        "queued",
        "running",
        "waiting",
        "succeeded",
        "failed",
    }:
        return cast(WorkState, normalized)
    return "waiting"


def application_work_reference(payload: dict[str, Any]) -> WorkReference:
    state = _state(payload.get("state") or payload.get("status"))
    progress = payload.get("progress")
    return WorkReference(
        type="job",
        id=str(payload.get("work_id") or payload.get("job_id") or payload.get("id") or ""),
        state=state,
        progress=(
            max(0.0, min(float(progress), 1.0)) if isinstance(progress, (int, float)) else None
        ),
        detail=(str(payload.get("detail"))[:2_000] if payload.get("detail") else None),
        cancellable=bool(payload.get("cancellable", state not in _TERMINAL)),
        poll_after_ms=max(
            0,
            min(
                int(
                    payload.get(
                        "poll_after_ms",
                        0 if state in _TERMINAL else 1_000,
                    )
                ),
                60_000,
            ),
        ),
    )


def manager_work_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove plan inputs, recovery internals, paths, and arbitrary results."""

    state = _state(payload.get("state"))
    return {
        "schema_version": "1",
        "type": "manager_operation",
        "id": str(payload.get("id") or ""),
        "kind": str(payload.get("kind") or ""),
        "state": state,
        "progress": max(
            0.0,
            min(float(payload.get("progress") or 0.0), 1.0),
        ),
        "current_task_id": (
            str(payload.get("current_task_id"))[:160] if payload.get("current_task_id") else None
        ),
        "error": (
            {
                "code": (
                    str(payload.get("error_code"))[:160] if payload.get("error_code") else None
                ),
                "message": str(payload.get("error_message") or "The Manager operation failed.")[
                    :2_000
                ],
            }
            if payload.get("error_code") or payload.get("error_message")
            else None
        ),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "finished_at": payload.get("finished_at"),
    }


def manager_work_reference(payload: dict[str, Any]) -> WorkReference:
    projected = manager_work_projection(payload)
    state = _state(projected["state"])
    return WorkReference(
        type="manager_operation",
        id=str(projected["id"]),
        state=state,
        progress=float(projected["progress"]),
        detail=(
            f"Current task: {projected['current_task_id']}"
            if projected["current_task_id"]
            else None
        ),
        cancellable=state not in _TERMINAL,
        poll_after_ms=0 if state in _TERMINAL else 1_000,
    )


def manager_task_log(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    safe_items: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items[:200]:
            if not isinstance(item, dict):
                continue
            task_value = item.get("task")
            error_value = item.get("error")
            task: dict[str, Any] = task_value if isinstance(task_value, dict) else {}
            error: dict[str, Any] = error_value if isinstance(error_value, dict) else {}
            safe_items.append(
                {
                    "task_id": str(task.get("id") or "")[:160],
                    "kind": str(task.get("kind") or "")[:160],
                    "ordinal": item.get("ordinal"),
                    "state": str(item.get("state") or ""),
                    "attempt": item.get("attempt"),
                    "error": {
                        "code": str(error.get("code") or "")[:160] or None,
                        "message": str(error.get("message") or "")[:2_000] or None,
                    },
                    "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                }
            )
    return {
        "schema_version": "1",
        "items": safe_items,
    }
