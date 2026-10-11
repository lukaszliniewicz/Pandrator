"""Portable MCP tools for previewing and permanently purging trashed sessions."""

from __future__ import annotations

import hashlib
import inspect
import uuid
from typing import Annotated, Any, Literal

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.session_purge import (
    DeleteSessionPermanentlyInput,
    GetSessionPurgeInspectionInput,
    GetSessionTrashPolicyInput,
    PreviewSessionDeletionInput,
    UpdateSessionTrashPolicyInput,
)
from ..work_mapping import application_work_reference


def preview_session_deletion(
    runtime: McpRuntime,
    arguments: PreviewSessionDeletionInput,
) -> ToolOutcome:
    application = runtime.require_application()
    if not arguments.admission:
        return ToolOutcome(result=application.preview_session_deletion(arguments.session_id))
    key = arguments.idempotency_key or f"purge-inspection:{uuid.uuid4()}"
    result = application.admit_session_purge_inspection(
        arguments.session_id, idempotency_key=key,
    )
    return ToolOutcome(
        result={**result, "idempotency_key": key},
        work=application_work_reference(result),
        next_actions=[NextAction(
            tool="pandrator_get_work",
            arguments={"work_type": "job", "work_id": result["job_id"]},
            reason="Wait for completion, then call pandrator_get_session_purge_inspection with this job ID.",
        )],
    )


def delete_session_permanently(
    runtime: McpRuntime,
    arguments: DeleteSessionPermanentlyInput,
) -> ToolOutcome:
    application = runtime.require_application()
    if not arguments.admission:
        return ToolOutcome(result=application.delete_session_permanently(
            arguments.session_id,
            expected_revision=arguments.expected_revision,
            impact_token=arguments.impact_token,
        ))
    key = arguments.idempotency_key or "purge-delete:" + hashlib.sha256(
        f"{arguments.session_id}:{arguments.expected_revision}:{arguments.impact_token.lower()}".encode()
    ).hexdigest()
    result = application.admit_session_purge(
        arguments.session_id,
        expected_revision=arguments.expected_revision,
        impact_token=arguments.impact_token,
        idempotency_key=key,
    )
    return ToolOutcome(
        result={**result, "idempotency_key": key},
        work=application_work_reference(result),
        next_actions=[NextAction(
            tool="pandrator_get_work",
            arguments={"work_type": "job", "work_id": result["job_id"]},
            reason="Monitor irreversible deletion through its durable work receipt.",
        )],
    )


def get_session_purge_inspection(
    runtime: McpRuntime,
    arguments: GetSessionPurgeInspectionInput,
) -> ToolOutcome:
    result = runtime.require_application().get_session_purge_inspection(arguments.job_id)
    if result.get("state") == "succeeded":
        return ToolOutcome(result=result)
    return ToolOutcome(
        result=result,
        work=application_work_reference(result),
        next_actions=[NextAction(
            tool="pandrator_get_work",
            arguments={"work_type": "job", "work_id": arguments.job_id},
            reason="Continue monitoring the durable inspection.",
        )],
    )


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
                   description, annotations, response=None) -> None:
    def invoke(**values) -> dict[str, Any]:
        mode = values.pop("response_mode", "standard")
        result = validated_call(handler, runtime, model, values)
        return response(result, mode) if response is not None else result

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
    if response is not None:
        annotations_map["response_mode"] = Literal["standard", "structured"]
        parameters.append(inspect.Parameter(
            "response_mode", inspect.Parameter.KEYWORD_ONLY,
            default="standard", annotation=annotations_map["response_mode"],
        ))
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
    response=lambda value, _mode: value,
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
        annotations=read_only, response=response,
    )
    _register_tool(
        server,
        runtime,
        validated_call,
        model=GetSessionPurgeInspectionInput,
        handler=get_session_purge_inspection,
        name="pandrator_get_session_purge_inspection",
        title="Get session purge inspection",
        description="Read the completed durable inspection and its original impact token.",
        annotations=read_only, response=response,
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
        annotations=destructive_action, response=response,
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
        annotations=read_only, response=response,
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
        annotations=revisioned_write_action, response=response,
    )


__all__ = [
    "delete_session_permanently",
    "get_session_trash_policy",
    "get_session_purge_inspection",
    "preview_session_deletion",
    "register_session_purge_tools",
    "update_session_trash_policy",
]
