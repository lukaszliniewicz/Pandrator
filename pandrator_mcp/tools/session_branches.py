"""Fork reviewed work and create language sessions without starting providers."""

from __future__ import annotations

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas.session_branches import (
    CancelTranslationProjectOperationInput,
    CreateTranslationBranchesInput,
    CreateTranslationProjectInput,
    ExecuteTranslationProjectOperationInput,
    ForkSessionInput,
    GetTranslationProjectExportManifestInput,
    GetTranslationProjectInput,
    GetTranslationProjectOperationInput,
    PreviewTranslationProjectOperationInput,
    RequestTranslationProjectExportBundleInput,
    RetryTranslationProjectOperationPreviewInput,
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


def preview_translation_project_operation(
    runtime: McpRuntime,
    arguments: PreviewTranslationProjectOperationInput,
) -> ToolOutcome:
    result = runtime.require_application().preview_translation_project_operation(
        arguments.project_id,
        selected_branch_ids=arguments.selected_branch_ids,
        expected_project_revision=arguments.expected_project_revision,
        action=arguments.action,
        export_kind=arguments.export_kind,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def get_translation_project_operation(
    runtime: McpRuntime,
    arguments: GetTranslationProjectOperationInput,
) -> ToolOutcome:
    result = runtime.require_application().get_translation_project_operation(
        arguments.operation_id
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def execute_translation_project_operation(
    runtime: McpRuntime,
    arguments: ExecuteTranslationProjectOperationInput,
) -> ToolOutcome:
    result = runtime.require_application().execute_translation_project_operation(
        arguments.operation_id,
        preview_digest=arguments.preview_digest,
        accepted_confirmations=arguments.accepted_confirmations,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def cancel_translation_project_operation(
    runtime: McpRuntime,
    arguments: CancelTranslationProjectOperationInput,
) -> ToolOutcome:
    result = runtime.require_application().cancel_translation_project_operation(
        arguments.operation_id,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def retry_translation_project_operation_preview(
    runtime: McpRuntime,
    arguments: RetryTranslationProjectOperationPreviewInput,
) -> ToolOutcome:
    result = runtime.require_application().retry_translation_project_operation_preview(
        arguments.operation_id,
        expected_project_revision=arguments.expected_project_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def get_translation_project_export_manifest(
    runtime: McpRuntime,
    arguments: GetTranslationProjectExportManifestInput,
) -> ToolOutcome:
    result = runtime.require_application().get_translation_project_export_manifest(
        arguments.operation_id
    )
    return ToolOutcome(result={"schema_version": "1", **result})


def request_translation_project_export_bundle(
    runtime: McpRuntime,
    arguments: RequestTranslationProjectExportBundleInput,
) -> ToolOutcome:
    result = runtime.require_application().request_translation_project_export_bundle(
        arguments.operation_id,
        expected_manifest_digest=arguments.expected_manifest_digest,
        idempotency_key=arguments.idempotency_key,
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
        (
            PreviewTranslationProjectOperationInput,
            preview_translation_project_operation,
            "pandrator_preview_translation_project_operation",
            "Preview a translation-project operation",
            "Create a job-free preview for selected language branches. Review each child's "
            "captured inputs, readiness and required confirmations before execution.",
            read_only,
        ),
        (
            GetTranslationProjectOperationInput,
            get_translation_project_operation,
            "pandrator_get_translation_project_operation",
            "Inspect a translation-project operation",
            "Read the complete redacted operation and child outcomes, including passive "
            "dispatch state, manual-resume requirements and produced artifact downloads.",
            read_only,
        ),
        (
            ExecuteTranslationProjectOperationInput,
            execute_translation_project_operation,
            "pandrator_execute_translation_project_operation",
            "Execute a reviewed translation-project preview",
            "Submit only eligible children from the supplied preview digest. Pass only "
            "confirmations the user explicitly accepted; none are inferred automatically.",
            write_action,
        ),
        (
            CancelTranslationProjectOperationInput,
            cancel_translation_project_operation,
            "pandrator_cancel_translation_project_operation",
            "Cancel pending translation-project work",
            "Request cancellation of supported active work and cancel pending children. "
            "Completed children and passive dispatch authority are preserved.",
            write_action,
        ),
        (
            RetryTranslationProjectOperationPreviewInput,
            retry_translation_project_operation_preview,
            "pandrator_retry_translation_project_operation_preview",
            "Preview retries for a translation-project operation",
            "Build a fresh guarded preview for retryable children without executing them. "
            "Completed children remain retained and active or partial passive work requires manual resume.",
            read_only,
        ),
        (
            GetTranslationProjectExportManifestInput,
            get_translation_project_export_manifest,
            "pandrator_get_translation_project_export_manifest",
            "Inspect a translation-project export manifest",
            "Inspect the complete redacted export manifest, including incomplete branch exports and verified artifact metadata.",
            read_only,
        ),
        (
            RequestTranslationProjectExportBundleInput,
            request_translation_project_export_bundle,
            "pandrator_request_translation_project_export_bundle",
            "Request a translation-project export bundle",
            "Queue or recover the ZIP bundle for the exact current export-manifest digest. This does not start translation or synthesis.",
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
