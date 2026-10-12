"""Canonical subtitle passage tools with strict native argument validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from ..context import McpRuntime
from ..native_text import NativeNullableString
from ..schemas.subtitle_passages import (
    PatchSubtitlePassagesInput,
    PreviewSubtitlePassagesInput,
    SubtitlePassagePatch,
)
from ..tools.subtitle_passages import patch_subtitle_passages, preview_subtitle_passages


def register_subtitle_passage_tools(
    server: Any, runtime: McpRuntime, call: Callable[..., dict[str, Any]],
    *, read_only: Any, write_action: Any,
) -> None:
    @server.tool(name="pandrator_preview_subtitle_passages",
                 title="Inspect logical subtitle passages", annotations=read_only)
    def preview_passages(
        session_id: str, artifact_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        query: NativeNullableString = None,
    ) -> dict[str, Any]:
        """Read logical passage IDs, turns and edit guards for one exact artifact. Display cue ordinals are separate."""
        return call(preview_subtitle_passages, runtime, PreviewSubtitlePassagesInput,
                    {key: value for key, value in locals().items()
                     if key in PreviewSubtitlePassagesInput.model_fields})

    @server.tool(name="pandrator_patch_subtitle_passages",
                 title="Edit logical subtitle passages and turns", annotations=write_action)
    def patch_passages(
        session_id: str, source_artifact_id: str, expected_source_hash: str,
        expected_revision: Annotated[int, Field(ge=1)], expected_composition_hash: str,
        passages: Annotated[list[SubtitlePassagePatch], Field(min_length=1, max_length=100)],
        idempotency_key: str, dry_run: Annotated[bool, Field(strict=True)] = False,
    ) -> dict[str, Any]:
        """Apply sparse source passage edits, preserving all other passages and evidence. Set starts_new_turn for a speaker handoff; dry_run performs save validation without publication."""
        return call(patch_subtitle_passages, runtime, PatchSubtitlePassagesInput,
                    {key: value for key, value in locals().items()
                     if key in PatchSubtitlePassagesInput.model_fields})
