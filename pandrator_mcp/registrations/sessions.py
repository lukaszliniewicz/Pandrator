"""Session lifecycle native tool registrations using the shared request guard."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic.experimental.missing_sentinel import MISSING

from ..context import McpRuntime
from ..native_enums import NativeNullableEnum
from ..native_text import NativeNullableString
from ..schemas import (
    AttachExistingSourceInput,
    CreateSessionInput,
    GetSessionInput,
    ListSessionsInput,
    MultilingualSetup,
    RestoreSessionInput,
    TrashSessionInput,
    UpdateSessionInput,
)
from ..tools import (
    attach_existing_source,
    create_session,
    get_session,
    list_sessions,
    restore_session,
    trash_session,
    update_session,
)


def register_session_library_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_input_factory: Callable[..., dict[str, Any]],
    *,
    read_only: Any,
    revisioned_write_action: Any,
) -> None:
    @server.tool(
        name="pandrator_list_sessions",
        title="List Pandrator sessions",
        annotations=read_only,
    )
    def sessions_tool(
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        workflow_kind: NativeNullableEnum[
            Literal["audiobook", "subtitles", "voiceover", "media_edit"]
        ] = None,
        include_trashed: bool = False,
        state: NativeNullableString = None,
        query: NativeNullableString = None,
    ) -> dict[str, Any]:
        """List bounded session summaries from the configured target."""

        return _call_with_input_factory(
            list_sessions,
            runtime,
            lambda: ListSessionsInput(
                limit=limit,
                workflow_kind=workflow_kind,
                include_trashed=include_trashed,
                state=state,
                query=query,
            ),
        )

    @server.tool(
        name="pandrator_get_session",
        title="Inspect a Pandrator session",
        annotations=read_only,
    )
    def session_get_tool(session_id: str) -> dict[str, Any]:
        """Inspect one session summary and its current revision."""

        return _call_with_input_factory(
            get_session,
            runtime,
            lambda: GetSessionInput(session_id=session_id),
        )

    @server.tool(
        name="pandrator_trash_session",
        title="Move a session to recoverable trash",
        annotations=revisioned_write_action,
    )
    def session_trash_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision: Annotated[int, Field(ge=0)],
    ) -> dict[str, Any]:
        """Move exactly one session to recoverable trash; it can be restored later."""

        return _call_with_input_factory(
            trash_session,
            runtime,
            lambda: TrashSessionInput(
                session_id=session_id,
                expected_revision=expected_revision,
            ),
        )

    @server.tool(
        name="pandrator_restore_session",
        title="Restore a session from recoverable trash",
        annotations=revisioned_write_action,
    )
    def session_restore_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        expected_revision: Annotated[int, Field(ge=0)],
    ) -> dict[str, Any]:
        """Restore exactly one trashed session using its current revision."""

        return _call_with_input_factory(
            restore_session,
            runtime,
            lambda: RestoreSessionInput(
                session_id=session_id,
                expected_revision=expected_revision,
            ),
        )


def register_session_setup_tools(
    server: Any,
    runtime: McpRuntime,
    _call_with_input_factory: Callable[..., dict[str, Any]],
    *,
    write_action: Any,
) -> None:
    @server.tool(
        name="pandrator_create_session",
        title="Create a Pandrator session",
        annotations=write_action,
    )
    def session_create_tool(
        name: Annotated[str, Field(min_length=1, max_length=200)],
        idempotency_key: str,
        workflow_kind: Literal[
            "audiobook",
            "subtitles",
            "voiceover",
            "media_edit",
        ] = "audiobook",
        source_language: Annotated[
            str,
            Field(min_length=2, max_length=40),
        ] = "auto",
        target_language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=40),
        ] = None,
        workflow_preset: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
            ),
        ] = "custom",
        included_stages: tuple[
            Literal[
                "transcribe",
                "correct",
                "translate",
                "clean_source",
                "prepare_text",
                "optimize_document",
                "optimize_tts",
                "generate_audio",
                "export",
                "edit_media",
            ],
            ...,
        ] = (),
        multilingual_setup: MultilingualSetup | None = None,
    ) -> dict[str, Any]:
        """Create one session; retries with the same key replay the first result."""

        return _call_with_input_factory(
            create_session,
            runtime,
            lambda: CreateSessionInput(
                name=name,
                workflow_kind=workflow_kind,
                source_language=source_language,
                target_language=target_language,
                workflow_preset=workflow_preset,
                included_stages=included_stages,
                multilingual_setup=multilingual_setup,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_update_session",
        title="Update a Pandrator session",
        annotations=write_action,
    )
    def session_update_tool(
        session_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        idempotency_key: str,
        name: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=200),
        ] = None,
        workflow_kind: NativeNullableEnum[
            Literal[
                "audiobook",
                "subtitles",
                "voiceover",
                "media_edit",
            ]
        ] = None,
        multilingual_setup: MultilingualSetup | None | MISSING = MISSING,
        source_language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=40),
        ] = None,
        target_language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=40),
        ] = None,
        workflow_preset: Annotated[
            NativeNullableString,
            Field(
                min_length=1,
                max_length=64,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
            ),
        ] = None,
        included_stages: tuple[
            Literal[
                "transcribe",
                "correct",
                "translate",
                "clean_source",
                "prepare_text",
                "optimize_document",
                "optimize_tts",
                "generate_audio",
                "export",
                "edit_media",
            ],
            ...,
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Apply explicit fields only when the inspected revision still matches."""

        values = {
            "session_id": session_id,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
        }
        optional = {
            "name": name,
            "workflow_kind": workflow_kind,
            "source_language": source_language,
            "target_language": target_language,
            "workflow_preset": workflow_preset,
            "included_stages": included_stages,
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        if multilingual_setup is not MISSING:
            values["multilingual_setup"] = multilingual_setup
        return _call_with_input_factory(
            update_session,
            runtime,
            lambda: UpdateSessionInput.model_validate(values),
        )

    @server.tool(
        name="pandrator_attach_existing_source",
        title="Attach a reusable source to a Pandrator session",
        annotations=write_action,
    )
    def source_attach_tool(
        session_id: str,
        source_asset_id: str,
        expected_session_revision: Annotated[int, Field(ge=1)],
        idempotency_key: str,
        role: Literal["primary", "reference", "transcript", "media"] = "primary",
    ) -> dict[str, Any]:
        """Attach one existing source when the session revision still matches."""

        return _call_with_input_factory(
            attach_existing_source,
            runtime,
            lambda: AttachExistingSourceInput(
                session_id=session_id,
                source_asset_id=source_asset_id,
                role=role,
                expected_session_revision=expected_session_revision,
                idempotency_key=idempotency_key,
            ),
        )
