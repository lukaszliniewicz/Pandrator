"""Portable MCP tools for previewing and permanently purging trashed sessions."""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from ..context import McpRuntime
from ..results import ToolOutcome
from ..schemas.session_purge import (
    DeleteSessionPermanentlyInput,
    GetSessionTrashPolicyInput,
    PreviewSessionDeletionInput,
    UpdateSessionTrashPolicyInput,
)


def preview_session_deletion(
    runtime: McpRuntime,
    arguments: PreviewSessionDeletionInput,
) -> ToolOutcome:
    return ToolOutcome(
        result=runtime.require_application().preview_session_deletion(
            arguments.session_id
        )
    )


def delete_session_permanently(
    runtime: McpRuntime,
    arguments: DeleteSessionPermanentlyInput,
) -> ToolOutcome:
    result = runtime.require_application().delete_session_permanently(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        impact_token=arguments.impact_token,
    )
    return ToolOutcome(result=result)


def get_session_trash_policy(
    runtime: McpRuntime,
    _arguments: GetSessionTrashPolicyInput,
) -> ToolOutcome:
    return ToolOutcome(result=runtime.require_application().get_session_trash_policy())


def update_session_trash_policy(
    runtime: McpRuntime,
    arguments: UpdateSessionTrashPolicyInput,
) -> ToolOutcome:
    result = runtime.require_application().update_session_trash_policy(
        expected_revision=arguments.expected_revision,
        days=arguments.days,
    )
    return ToolOutcome(result=result)


def _register_tool(server, runtime, validated_call, *, model, handler, name, title,
                   description, annotations) -> None:
    def invoke(**values) -> dict[str, Any]:
        return validated_call(handler, runtime, model, values)

    parameters = []
    annotations_map: dict[str, object] = {"return": dict[str, Any]}
    for field_name, field in model.model_fields.items():
        annotations_map[field_name] = Annotated[field.annotation, field]
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotations_map[field_name],
                default=(
                    inspect.Parameter.empty
                    if field.is_required()
                    else field.default
                ),
            )
        )
    invoke.__name__ = name
    invoke.__doc__ = description
    invoke.__annotations__ = annotations_map
    invoke.__signature__ = inspect.Signature(
        parameters,
        return_annotation=dict[str, Any],
    )
    server.tool(name=name, title=title, annotations=annotations)(invoke)


def register_session_purge_tools(
    server,
    runtime: McpRuntime,
    validated_call,
    *,
    read_only,
    revisioned_write_action,
    destructive_action,
) -> None:
    """Register flat tools with separate preview, confirm, and policy actions."""

    _register_tool(
        server,
        runtime,
        validated_call,
        model=PreviewSessionDeletionInput,
        handler=preview_session_deletion,
        name="pandrator_preview_session_deletion",
        title="Preview permanent session deletion",
        description=(
            "Inspect blockers, owned files, retained shared artifacts, and the "
            "revision-bound impact token for a trashed session."
        ),
        annotations=read_only,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=DeleteSessionPermanentlyInput,
        handler=delete_session_permanently,
        name="pandrator_delete_session_permanently",
        title="Permanently delete a trashed session",
        description=(
            "Permanently remove a trashed session after preview. Requires the "
            "current revision, matching impact token, and explicit confirm=true."
        ),
        annotations=destructive_action,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=GetSessionTrashPolicyInput,
        handler=get_session_trash_policy,
        name="pandrator_get_session_trash_policy",
        title="Inspect session trash retention",
        description="Read automatic retention days for future session trash events.",
        annotations=read_only,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=UpdateSessionTrashPolicyInput,
        handler=update_session_trash_policy,
        name="pandrator_update_session_trash_policy",
        title="Set session trash retention",
        description=(
            "Set retention for future trash events using the current policy "
            "revision. Use days=null to disable automatic cleanup."
        ),
        annotations=revisioned_write_action,
    )


__all__ = [
    "delete_session_permanently",
    "get_session_trash_policy",
    "preview_session_deletion",
    "register_session_purge_tools",
    "update_session_trash_policy",
]
