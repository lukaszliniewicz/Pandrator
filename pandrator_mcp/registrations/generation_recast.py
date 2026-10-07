"""Flat native MCP signatures for casting and audio-version actions."""

from typing import Annotated, Any

from pydantic import Field

from ..native_text import NativeNullableString
from ..schemas.generation_recast import (
    PreviewRecastInput,
    PreviewUndoRecastInput,
    RecastVoicesInput,
    UndoRecastInput,
)
from ..tools.generation_recast import (
    preview_recast,
    preview_undo_recast,
    recast_voices,
    undo_recast,
)


def register_generation_recast_tools(
    server, runtime, validated_call, *, read_only, write_action
) -> None:
    @server.tool(name="pandrator_preview_recast", title="Preview voice recasting",
                 annotations=read_only)
    def preview_recast_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        selector_keys: Annotated[list[str] | None, Field(max_length=1000)] = None,
        speech_plan_revision_id: Annotated[
            NativeNullableString, Field(min_length=1, max_length=80)
        ] = None,
        force: bool = False,
        include_missing: bool = False,
    ) -> dict[str, Any]:
        """Preview voice changes; null selectors choose changed voices, [] chooses none."""
        return validated_call(preview_recast, runtime, PreviewRecastInput, {key: value for key, value in locals().items() if key in PreviewRecastInput.model_fields})

    @server.tool(name="pandrator_recast_voices", title="Recast previewed voices",
                 annotations=write_action)
    def recast_voices_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_selection_hash: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(
            min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
        )],
        selector_keys: Annotated[list[str] | None, Field(max_length=1000)] = None,
        speech_plan_revision_id: Annotated[
            NativeNullableString, Field(min_length=1, max_length=80)
        ] = None,
        force: bool = False,
        include_missing: bool = False,
    ) -> dict[str, Any]:
        """Queue exactly the previewed selection, fenced by its hash and idempotency key."""
        return validated_call(recast_voices, runtime, RecastVoicesInput, {key: value for key, value in locals().items() if key in RecastVoicesInput.model_fields})

    @server.tool(name="pandrator_preview_undo_recast", title="Preview recast undo",
                 annotations=read_only)
    def preview_undo_recast_tool(
        generation_run_id: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> dict[str, Any]:
        """Preview guarded restoration of audio choices from before a recast."""
        return validated_call(preview_undo_recast, runtime, PreviewUndoRecastInput, {key: value for key, value in locals().items() if key in PreviewUndoRecastInput.model_fields})

    @server.tool(name="pandrator_undo_recast", title="Undo previewed recast audio",
                 annotations=write_action)
    def undo_recast_tool(
        generation_run_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_selection_hash: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(
            min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
        )],
    ) -> dict[str, Any]:
        """Restore previewed audio choices while preserving the current cast."""
        return validated_call(undo_recast, runtime, UndoRecastInput, {key: value for key, value in locals().items() if key in UndoRecastInput.model_fields})
