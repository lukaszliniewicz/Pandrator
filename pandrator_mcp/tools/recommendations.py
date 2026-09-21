"""Deterministic inspect-first recommendations."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas import RecommendNextStepsInput


def recommend_next_steps(
    runtime: McpRuntime,
    arguments: RecommendNextStepsInput,
) -> dict[str, Any]:
    raw_goal = str(arguments.goal or "").strip()
    goal = raw_goal.casefold()
    multivoice = any(
        term in goal
        for term in (
            "multivoice",
            "multi-voice",
            "multi voice",
            "multiple voices",
            "character voices",
            "audio drama",
        )
    )
    voice_steps = []
    if not multivoice and any(
        term in goal for term in ("cast", "voice", "audio drama", "scottish")
    ):
        voice_steps = [
            {
                "tool": "pandrator_get_voice_capabilities",
                "reason": "Discover available renderer modes, reference requirements, and markup support.",
            },
            {
                "tool": "pandrator_get_voice_catalog",
                "reason": "Search reusable voices and evidence before designing a new reference.",
            },
            {
                "tool": "pandrator_explain_system",
                "arguments": {"topic": "voice-casting"},
                "reason": "Follow the passive discovery, audition, reference, and casting procedure.",
            },
        ]
    if not arguments.session_id:
        if multivoice and not any(term in goal for term in ("dubbing", "voiceover")):
            return {
                "schema_version": "1",
                "goal": arguments.goal,
                "basis": "static_guidance",
                "steps": [
                    {
                        "tool": "pandrator_explain_system",
                        "arguments": {"topic": "multivoice-audiobooks", "detail": "full"},
                        "reason": "Read the end-to-end procedure once, then create or reuse an audiobook session.",
                    }
                ],
            }
        workflow_topic = (
            "workflows"
            if any(
                word in goal
                for word in (
                    "media edit",
                    "edit video",
                    "edit recording",
                    "cut video",
                    "cut recording",
                    "trim video",
                    "trim recording",
                    "social clip",
                )
            )
            else "voiceover-and-dubbing"
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
                *voice_steps,
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
    if multivoice and (session.get("workflow_kind") or session.get("kind")) == "audiobook":
        return {
            "schema_version": "1",
            "goal": arguments.goal,
            "basis": "live_session",
            "session": {
                key: session.get(key)
                for key in ("id", "name", "workflow_kind", "status", "revision")
            },
            "incomplete_stages": incomplete,
            "steps": [
                {
                    "tool": "pandrator_get_audiobook_setup",
                    "arguments": {"session_id": arguments.session_id},
                    "reason": "Inspect mode, engine, narration budget and revision before atomic multi_voice configuration.",
                },
                {
                    "tool": "pandrator_get_generation_controls",
                    "arguments": {"session_id": arguments.session_id},
                    "reason": "Reuse stable character IDs and current voice bindings; update only the missing cast.",
                },
                {
                    "tool": "pandrator_explain_system",
                    "arguments": {"topic": "multivoice-audiobooks", "detail": "full"},
                    "reason": "Follow prepared JSON annotation, direct compiler preview, reviewed generation, durable waiting and assembly.",
                },
            ],
        }
    steps: list[dict[str, Any]] = [
        *voice_steps,
        {
            "tool": "pandrator_get_workflow",
            "arguments": {"session_id": arguments.session_id},
            "reason": "Review current stages, selections, and prerequisites.",
        },
    ]
    media_edit_session = (session.get("workflow_kind") or session.get("kind")) == "media_edit"
    if not media_edit_session and ("correct" in goal or "proofread" in goal):
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
    if not media_edit_session and "translat" in goal:
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
    if not media_edit_session and any(
        word in goal for word in ("speech optim", "tts optim", "speakable")
    ):
        steps.append(
            {
                "tool": "pandrator_create_speech_optimization_dispatch_run",
                "arguments": {"session_id": arguments.session_id},
                "reason": "Create a passive speech-optimization run for this model.",
            }
        )
    if media_edit_session and raw_goal:
        instructions = raw_goal
        steps.append(
            {
                "tool": "pandrator_plan_media_edit_workflow",
                "arguments": {
                    "session_id": arguments.session_id,
                    "instructions": instructions,
                },
                "reason": (
                    "Plan the complete live media-edit procedure, including source "
                    "setup, caption/ASR timing, passive proposal, review, and render."
                ),
            }
        )
    elif media_edit_session:
        steps.append(
            {
                "tool": "pandrator_get_media_edit",
                "arguments": {"session_id": arguments.session_id},
                "reason": (
                    "Inspect the current edit plan, then supply a nonblank editorial "
                    "goal to plan the complete media-edit procedure."
                ),
            }
        )
    if not media_edit_session and any(word in goal for word in ("tts", "voice", "narrat", "audio")):
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
    if not media_edit_session:
        final_tool = (
            "pandrator_plan_export_variant"
            if any(word in goal for word in ("export", "burn", "deliver", "final product"))
            else "pandrator_plan_workflow"
        )
        final_arguments: dict[str, Any] = {"session_id": arguments.session_id}
        if final_tool == "pandrator_plan_workflow":
            supported_stages = {
                "transcribe",
                "correct",
                "translate",
                "clean_source",
                "prepare_text",
                "optimize_document",
                "optimize_tts",
                "generate_audio",
                "export",
            }
            final_arguments["target_stage"] = next(
                (stage for stage in reversed(incomplete) if stage in supported_stages),
                "generate_audio",
            )
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
