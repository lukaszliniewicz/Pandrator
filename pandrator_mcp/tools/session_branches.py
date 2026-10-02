"""Fork reviewed work and create language sessions without starting providers."""

from __future__ import annotations

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.session_branches import (
    CreateTranslationBranchesInput,
    CreateTranslationProjectInput,
    ForkSessionInput,
    GetTranslationProjectInput,
)
from .session_purge import _register_tool
from .sessions import _session_projection


def fork_session(runtime: McpRuntime, arguments: ForkSessionInput) -> ToolOutcome:
    result = runtime.require_application().fork_session(
        arguments.session_id,
        **arguments.model_dump(exclude={"session_id"}, exclude_none=True),
    )
    return ToolOutcome(
        result={
            "schema_version": "1",
            **_session_projection(result),
            **{
                key: result.get(key)
                for key in (
                    "forked_from_session_id",
                    "checkpoint_artifact_id",
                    "copied_stages",
                    "copied_media_artifact_ids",
                )
            },
        },
        next_actions=[
            NextAction(
                tool="pandrator_get_workflow",
                arguments={"session_id": result["id"]},
                reason="Inspect the independent fork and its copied subtitle/media checkpoints.",
            )
        ],
    )


def get_translation_project(
    runtime: McpRuntime,
    arguments: GetTranslationProjectInput,
) -> ToolOutcome:
    return ToolOutcome(
        result={
            "schema_version": "1",
            **runtime.require_application().get_translation_project(arguments.session_id),
        }
    )


def create_translation_project(
    runtime: McpRuntime,
    arguments: CreateTranslationProjectInput,
) -> ToolOutcome:
    return ToolOutcome(
        result={
            "schema_version": "1",
            **runtime.require_application().create_translation_project(
                arguments.session_id,
                **arguments.model_dump(exclude={"session_id"}, exclude_none=True),
            ),
        }
    )


def create_translation_branches(
    runtime: McpRuntime,
    arguments: CreateTranslationBranchesInput,
) -> ToolOutcome:
    result = runtime.require_application().create_translation_branches(
        arguments.project_id,
        **arguments.model_dump(exclude={"project_id"}, mode="json", exclude_none=True),
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def register_session_branch_tools(
    server,
    runtime,
    validated_call,
    *,
    read_only,
    write_action,
) -> None:
    actions = (
        (
            ForkSessionInput,
            fork_session,
            "pandrator_fork_session",
            "Fork a reviewed session",
            "Fork an exact correction/translation checkpoint. Copy its edited base video and "
            "evidence by default; generated voices and exports stay independent.",
            write_action,
        ),
        (
            GetTranslationProjectInput,
            get_translation_project,
            "pandrator_get_translation_project",
            "Inspect a multilingual project",
            "Inspect the pinned corrected source and bounded independent language branches "
            "for a source or branch session.",
            read_only,
        ),
        (
            CreateTranslationProjectInput,
            create_translation_project,
            "pandrator_create_translation_project",
            "Create a multilingual project",
            "Pin an exact corrected source and edited timeline for a multilingual project. "
            "Set create_planned_branches to create the saved session language plan atomically. "
            "Planned branches use automatic target-language subtitle profiles by default; "
            "set carry_source_subtitle_settings in the saved setup to preserve current source settings. "
            "Does not translate or start synthesis.",
            write_action,
        ),
        (
            CreateTranslationBranchesInput,
            create_translation_branches,
            "pandrator_create_translation_branches",
            "Create independent language branches",
            "Atomically create up to 20 language sessions from one pinned project checkpoint. "
            "Each target uses automatic subtitle profiles by default; set its "
            "carry_source_subtitle_settings to true to copy the source's current effective settings. "
            "Run translation, voice review and generation independently in each returned session; "
            "branch runs can proceed in parallel.",
            write_action,
        ),
    )
    for model, handler, name, title, description, tool_annotations in actions:
        _register_tool(
            server,
            runtime,
            validated_call,
            model=model,
            handler=handler,
            name=name,
            title=title,
            description=description,
            annotations=tool_annotations,
        )
