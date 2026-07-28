"""Deterministic inspect-first recommendations."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas import RecommendNextStepsInput


def recommend_next_steps(
    runtime: McpRuntime,
    arguments: RecommendNextStepsInput,
) -> dict[str, Any]:
    if not arguments.session_id:
        return {
            "schema_version": "1",
            "goal": arguments.goal,
            "basis": "static_guidance",
            "steps": [
                {
                    "tool": "pandrator_get_capabilities",
                    "reason": "Check which local and provider-backed features are available.",
                },
                {
                    "tool": "pandrator_list_sessions",
                    "reason": "Inspect existing work before creating or replacing anything.",
                },
                {
                    "tool": "pandrator_explain_system",
                    "arguments": {"topic": "workflows"},
                    "reason": "Choose the workflow that matches the desired outcome.",
                },
            ],
        }
    application = runtime.require_application()
    session = application.get_session(arguments.session_id)
    workflow = application.get_workflow(arguments.session_id)
    stages = workflow.get("stages")
    incomplete: list[str] = []
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, dict) and str(stage.get("status") or "") not in {
                "complete",
                "completed",
                "succeeded",
            }:
                key = str(stage.get("key") or stage.get("stage_key") or "")
                if key:
                    incomplete.append(key)
    return {
        "schema_version": "1",
        "goal": arguments.goal,
        "basis": "live_session",
        "session": {
            "id": session.get("id"),
            "name": session.get("name"),
            "workflow_kind": session.get("workflow_kind"),
            "status": session.get("status"),
            "revision": session.get("revision"),
        },
        "incomplete_stages": incomplete,
        "steps": [
            {
                "tool": "pandrator_get_workflow",
                "arguments": {"session_id": arguments.session_id},
                "reason": "Review current stages, selections, and prerequisites.",
            },
            {
                "tool": "pandrator_plan_workflow",
                "arguments": {
                    "session_id": arguments.session_id,
                    "target_stage": (
                        incomplete[-1]
                        if incomplete
                        else "generate_audio"
                    ),
                },
                "reason": "Preview exact work and provider disclosures before execution.",
            },
        ],
    }
