"""Deterministic inspect-first recommendations."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas import RecommendNextStepsInput


def recommend_next_steps(
    runtime: McpRuntime,
    arguments: RecommendNextStepsInput,
) -> dict[str, Any]:
    goal = str(arguments.goal or "").casefold()
    if not arguments.session_id:
        workflow_topic = (
            "voiceover-and-dubbing"
            if any(word in goal for word in ("voiceover", "dub", "dubbing"))
            else "subtitles"
            if any(word in goal for word in ("subtitle", "transcrib", "caption"))
            else "audiobooks"
            if any(word in goal for word in ("book", "epub", "pdf", "audiobook"))
            else "workflows"
        )
        return {
            "schema_version": "1",
            "goal": arguments.goal,
            "basis": "static_guidance",
            "steps": [
                {
                    "tool": "pandrator_get_target_status",
                    "reason": (
                        "Check authentication scopes and whether local source and "
                        "output roots are configured for the requested workflow."
                    ),
                },
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
                    "arguments": {"topic": workflow_topic},
                    "reason": "Choose the workflow that matches the desired outcome.",
                },
                {
                    "tool": "pandrator_browse_local_sources",
                    "reason": (
                        "If the source is a same-machine file, list only the approved "
                        "root names before selecting a relative path."
                    ),
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
    steps: list[dict[str, Any]] = [
        {
            "tool": "pandrator_get_workflow",
            "arguments": {"session_id": arguments.session_id},
            "reason": "Review current stages, selections, and prerequisites.",
        }
    ]
    if "correct" in goal or "proofread" in goal:
        steps.append(
            {
                "tool": "pandrator_create_dispatch_run",
                "arguments": {
                    "session_id": arguments.session_id,
                    "kind": "correction",
                },
                "reason": "Create a passive correction run for this model to process.",
            }
        )
    if "translat" in goal:
        steps.append(
            {
                "tool": "pandrator_create_dispatch_run",
                "arguments": {
                    "session_id": arguments.session_id,
                    "kind": "translation",
                },
                "reason": "Create a passive translation run after correction is selected.",
            }
        )
    if any(word in goal for word in ("speech optim", "tts optim", "speakable")):
        steps.append(
            {
                "tool": "pandrator_create_speech_optimization_dispatch_run",
                "arguments": {"session_id": arguments.session_id},
                "reason": "Create a passive speech-optimization run for this model.",
            }
        )
    if any(word in goal for word in ("tts", "voice", "narrat", "audio")):
        steps.extend(
            [
                {
                    "tool": "pandrator_get_tts_catalog",
                    "reason": "Resolve live service, model, and voice IDs without guessing.",
                },
                {
                    "tool": "pandrator_get_session_settings",
                    "arguments": {
                        "session_id": arguments.session_id,
                        "section": "tts",
                    },
                    "reason": "Get the settings revision before catalog-backed configuration.",
                },
            ]
        )
    final_tool = (
        "pandrator_plan_export_variant"
        if any(word in goal for word in ("export", "burn", "deliver", "final product"))
        else "pandrator_plan_workflow"
    )
    final_arguments: dict[str, Any] = {"session_id": arguments.session_id}
    if final_tool == "pandrator_plan_workflow":
        final_arguments["target_stage"] = incomplete[-1] if incomplete else "generate_audio"
    steps.append(
        {
            "tool": final_tool,
            "arguments": final_arguments,
            "reason": "Preview exact work and provider disclosures before execution.",
        }
    )
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
        "steps": steps,
    }
