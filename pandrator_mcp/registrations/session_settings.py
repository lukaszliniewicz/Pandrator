"""Session settings native registrations using the shared request guard."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ..context import McpRuntime
from ..schemas import (
    GetSessionSettingsInput,
    PatchSessionSettingsInput,
    UpdateSessionSettingsInput,
)
from ..tools import (
    get_session_settings,
    patch_session_settings,
    update_session_settings,
)


def register_session_settings_read_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_input_factory: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
) -> None:
    @server.tool(
        name="pandrator_get_session_settings",
        title="Inspect effective Pandrator session settings",
        annotations=read_only,
    )
    def session_settings_get_tool(
        session_id: str,
        section: Literal[
            "text",
            "stt",
            "subtitles",
            "correction",
            "translation",
            "tts",
            "audio",
            "rvc",
            "source_cleaning",
            "output",
        ],
    ) -> dict[str, Any]:
        """Inspect one settings section, its effective values, and revision."""

        return _call_with_input_factory(
            get_session_settings,
            runtime,
            lambda: GetSessionSettingsInput(
                session_id=session_id,
                section=section,
            ),
        )


def register_session_settings_write_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_input_factory: Callable[..., dict[str, Any]],
    *,
    write_action: Any,
) -> None:
    @server.tool(
        name="pandrator_update_session_settings",
        title=("Replace full Pandrator session settings section; omitted fields removed"),
        annotations=write_action,
    )
    def session_settings_update_tool(
        session_id: str,
        section: Literal[
            "text",
            "stt",
            "subtitles",
            "correction",
            "translation",
            "tts",
            "audio",
            "rvc",
            "source_cleaning",
            "output",
        ],
        expected_revision: Annotated[int, Field(ge=0)],
        value: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Replace the complete settings override using revision-safe idempotency.

        Omitted fields are removed. Use pandrator_patch_session_settings for
        ordinary partial edits.
        """

        return _call_with_input_factory(
            update_session_settings,
            runtime,
            lambda: UpdateSessionSettingsInput(
                session_id=session_id,
                section=section,
                expected_revision=expected_revision,
                value=value,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_patch_session_settings",
        title="Patch one Pandrator session settings section",
        annotations=write_action,
    )
    def session_settings_patch_tool(
        session_id: str,
        section: Literal[
            "text",
            "stt",
            "subtitles",
            "correction",
            "translation",
            "tts",
            "audio",
            "rvc",
            "source_cleaning",
            "output",
        ],
        expected_revision: Annotated[int, Field(ge=0)],
        value: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Merge top-level fields into the stored override with revision-safe idempotency.

        Use pandrator_update_session_settings for full replacement; omitted
        fields are removed by that operation. Nested values replace whole
        fields, and null remains a literal value.
        """

        return _call_with_input_factory(
            patch_session_settings,
            runtime,
            lambda: PatchSessionSettingsInput(
                session_id=session_id,
                section=section,
                expected_revision=expected_revision,
                value=value,
                idempotency_key=idempotency_key,
            ),
        )
