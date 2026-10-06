"""Deterministic MCP prompt registrations."""

from __future__ import annotations

from typing import Any, Literal


def register_prompts(server: Any) -> None:
    """Register prompts at their existing inventory position."""

    @server.prompt(name="start_audiobook")
    def start_audiobook_prompt(goal: str) -> str:
        """Guide a review-first audiobook workflow."""

        return (
            f"Help the user produce this audiobook outcome: {goal}\n"
            "Read the audiobook guide, inspect capabilities and existing sessions, "
            "then use preview → approval → exact execution → observation. Never "
            "request credentials in chat."
        )

    @server.prompt(name="dub_media")
    def dub_media_prompt(goal: str) -> str:
        """Guide a review-first dubbing workflow."""

        return (
            f"Help the user dub media with this outcome: {goal}\n"
            "Read the dubbing guide, inspect providers, voices, and capabilities, "
            "then preview provider disclosures and exact stages before execution."
        )

    @server.prompt(name="produce_subtitles")
    def produce_subtitles_prompt(goal: str) -> str:
        """Guide a review-first subtitle workflow."""

        return (
            f"Help the user produce subtitles with this outcome: {goal}\n"
            "Read the subtitles guide, inspect the session workflow and artifacts, "
            "and preserve review revisions before any consequential execution."
        )

    @server.prompt(name="produce_voiceover_end_to_end")
    def produce_voiceover_end_to_end_prompt(goal: str) -> str:
        """Guide a provider-agnostic media-to-deliverables workflow."""

        return (
            f"Complete this Pandrator outcome end to end: {goal}\n"
            "When the session already exists, begin with "
            "pandrator_plan_orchestrated_workflow to describe requested passive "
            "stages and the deferred native plan. Otherwise begin with "
            "pandrator_recommend_next_steps, target status, and the "
            "voiceover guide. Use pandrator_browse_local_sources only on approved "
            "named roots; create or inspect the session, then call "
            "pandrator_import_local_source for the selected relative file. Plan and execute "
            "transcription, polling its durable work to terminal. Use passive "
            "subtitle dispatch for correction and translation: claim exactly one "
            "batch, produce the requested text yourself, submit every required ID "
            "once, and repeat until complete. If speech optimization is wanted, use "
            "its passive dispatcher the same way. Resolve the TTS service, model, "
            "and voice from pandrator_get_tts_catalog; examples in the user's goal "
            "are preferences, never hard-coded identifiers. Call "
            "pandrator_configure_tts, plan and execute generation, and poll to terminal. "
            "Use pandrator_list_generation_runs before "
            "pandrator_plan_export_variant for each requested output. Execute every "
            "exact export plan, poll it to terminal, list its artifacts, and call "
            "pandrator_download_artifact for requested outputs. Report artifact IDs "
            "and local paths."
        )

    @server.prompt(name="run_passive_processing")
    def run_passive_processing_prompt(
        kind: Literal[
            "subtitle_correction",
            "subtitle_translation",
            "source_cleanup",
            "speech_optimization",
        ],
        goal: str,
    ) -> str:
        """Guide one model-operated passive dispatch loop."""

        return (
            f"Perform passive {kind} for this outcome: {goal}\n"
            "Read the matching guide and inspect the session and selected source. "
            "Create the matching dispatch run with the user's quality instructions. "
            "Do not configure an external model provider: you are the processor. "
            "Choose boundary context for continuity at setup. Claim one sequential "
            "batch; use compact packets and cache only manifests still available to "
            "you. Obey the kind's operation and ID-coverage contract, submit only "
            "the required typed result and new context information, and follow "
            "next_actions. Renew the lease before it expires when needed. Continue "
            "until the run is terminal, then inspect the resulting artifact."
        )

    @server.prompt(name="diagnose_failed_work")
    def diagnose_failed_work_prompt(work_id: str) -> str:
        """Guide read-only work failure diagnosis."""

        return (
            f"Diagnose Pandrator work {work_id}. Inspect the work record and its "
            "redacted log, then explain the failure and safe next actions. Do not "
            "retry, cancel, or repair unless the user separately authorizes it."
        )

    @server.prompt(name="repair_pandrator_instance")
    def repair_instance_prompt(goal: str = "restore healthy operation") -> str:
        """Guide plan-first Manager recovery."""

        return (
            f"Help the user {goal}. Inspect target status, Manager status, and "
            "Manager doctor first. Create a component plan and obtain explicit "
            "confirmation before any repair or runtime action."
        )
