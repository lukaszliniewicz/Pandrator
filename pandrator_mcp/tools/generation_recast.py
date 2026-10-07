"""Compact receipts for previewed voice changes and audio-version undo."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..schemas.generation_recast import (
    PreviewRecastInput,
    PreviewUndoRecastInput,
    RecastVoicesInput,
    UndoRecastInput,
)
from ..work_mapping import application_work_reference

_RUN_FIELDS = (
    "id", "session_id", "plan_revision_id", "speech_plan_revision_id", "sequence_number",
    "operation", "status", "state", "job_id", "work_id", "generate_count", "keep_count",
    "missing_count", "selected_count", "audio_version", "created_at", "updated_at",
)
_VERSION_FIELDS = (
    "kind", "selectors", "target_count", "generated_count", "applied_count", "remaining_count",
    "undone", "before_available",
)


def _run_receipt(run: dict[str, Any], *, voice_page: bool) -> dict[str, Any]:
    result = {key: run[key] for key in _RUN_FIELDS if key in run}
    version = result.get("audio_version")
    if isinstance(version, dict):
        result["audio_version"] = {key: version[key] for key in _VERSION_FIELDS if key in version}
    if result.get("session_id"):
        path = f"/sessions/{quote(str(result['session_id']), safe='')}"
        result["application_link"] = path + ("/voice" if voice_page else "")
    return result


def _request(runtime: McpRuntime, action: str, arguments) -> dict[str, Any]:
    try:
        return runtime.require_application().generation_recast_request(
            action, arguments.model_dump(mode="json", exclude_none=False)
        )
    except PandratorMcpError as error:
        if error.code == "application_response_timeout" and action in {"recast", "undo"}:
            error.next_actions = [NextAction(
                tool="pandrator_recast_voices" if action == "recast" else "pandrator_undo_recast",
                arguments=arguments.model_dump(mode="json", exclude_none=False),
                reason="Replay this request with the same selection hash and idempotency key.",
            )]
        raise


def preview_recast(runtime: McpRuntime, arguments: PreviewRecastInput) -> ToolOutcome:
    payload = _request(runtime, "preview", arguments)
    keys = (
        "plan_id", "speech_plan_revision_id", "selection_hash", "settings_summary",
        "controls_revision", "control_revision", "selectors", "selected_keys", "generate_count",
        "keep_count", "missing_count", "shared_segment_count", "blocked_reason",
    )
    result = {key: payload[key] for key in keys if key in payload}
    result["segments"] = (payload.get("segments") or [])[:50]
    return ToolOutcome(result={"schema_version": "1", **result})


def recast_voices(runtime: McpRuntime, arguments: RecastVoicesInput) -> ToolOutcome:
    payload = _request(runtime, "recast", arguments)
    payload = {"session_id": arguments.session_id, **payload}
    return ToolOutcome(
        result={"schema_version": "1", **_run_receipt(payload, voice_page=True)},
        work=application_work_reference(payload),
    )


def preview_undo_recast(runtime: McpRuntime, arguments: PreviewUndoRecastInput) -> ToolOutcome:
    payload = _request(runtime, "undo_preview", arguments)
    keys = (
        "generation_run_id", "speech_plan_revision_id", "selection_hash", "restore_count",
        "blocked_reason",
    )
    return ToolOutcome(result={"schema_version": "1", **{
        key: payload[key] for key in keys if key in payload
    }})


def undo_recast(runtime: McpRuntime, arguments: UndoRecastInput) -> ToolOutcome:
    payload = _request(runtime, "undo", arguments)
    return ToolOutcome(result={
        "schema_version": "1",
        "run": _run_receipt(payload.get("run") or {}, voice_page=False),
        "restored_count": payload.get("restored_count"),
    })
