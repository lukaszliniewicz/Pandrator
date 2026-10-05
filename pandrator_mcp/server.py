"""July 2026 MCP adapter for Pandrator guidance and bounded automation."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from . import __version__
from .argument_validation import create_argument_validation_extension
from .context import McpRuntime
from .errors import FailureCode, PandratorMcpError, ToolFailure
from .native_enums import NativeNullableEnum
from .native_text import NativeNullableString
from .registrations.dispatch import (
    register_dispatch_batch_tools,
    register_dispatch_lifecycle_tools,
    register_dispatch_run_tools,
)
from .registrations.generation import (
    register_generation_execution_tools,
    register_generation_plan_tools,
)
from .registrations.media_edit import (
    register_media_edit_dispatch_tools,
    register_media_edit_tools,
)
from .registrations.prompts import register_prompts
from .registrations.resources import register_resources
from .registrations.session_settings import (
    register_session_settings_read_tools,
    register_session_settings_write_tools,
)
from .registrations.sessions import (
    register_session_library_tools,
    register_session_setup_tools,
)
from .registrations.source_cleaning_dispatch import register_source_cleaning_dispatch_tools
from .registrations.speech_optimization_dispatch import register_speech_optimization_dispatch_tools
from .registrations.transcription import register_transcription_tools
from .request_context import begin_request, end_request
from .results import ToolOutcome
from .schemas import (
    AdoptSubtitleSourceInput,
    BrowseLocalSourcesInput,
    CancelWorkInput,
    CapabilitiesInput,
    ConfigureTtsInput,
    ControlRuntimeInput,
    CreateTextSourceInput,
    CuePatchInput,
    DeleteOutputInput,
    DescribeParametersInput,
    DownloadArtifactInput,
    ElevenLabsVoiceSettingsInput,
    ExecuteComponentPlanInput,
    ExecuteWorkflowPlanInput,
    ExplainSystemInput,
    GetSubtitleEvidenceInput,
    GetWorkflowInput,
    GetWorkInput,
    GetWorkLogInput,
    GuideTopic,
    ImportLocalSourceInput,
    ImportSubtitlesInput,
    ListArtifactsInput,
    ListSourcesInput,
    ListWorkInput,
    ManagerDesiredComponentInput,
    PatchSubtitleCuesInput,
    PlanComponentChangeInput,
    PlanExportVariantInput,
    PlanOrchestratedWorkflowInput,
    PlanWorkflowInput,
    PreviewSubtitlesInput,
    ProviderStatusInput,
    RecommendNextStepsInput,
    ReplaceSubtitleTextInput,
    RequestSubtitleEvidenceInput,
    ResolveSubtitleEvidenceInput,
    SubtitleStage,
    SystemStatusInput,
    TargetStatusInput,
    TtsCatalogInput,
    VoiceCatalogInput,
)
from .schemas.delegation import (
    DelegationContextCapsuleInput,
    execution_policy_json_schema,
)
from .schemas.e2e import AudioCppCatalogueInput
from .schemas.subtitle_evidence import GetSubtitleEvidenceRoutesInput
from .schemas.workflow_inputs import GetWorkflowInputsInput, SelectWorkflowInputInput
from .tools import (
    adopt_subtitle_source,
    browse_local_sources,
    cancel_work,
    capabilities,
    configure_tts,
    control_runtime,
    create_text_source,
    delete_output,
    describe_parameters,
    download_artifact,
    execute_component_plan,
    execute_workflow_plan,
    explain_system,
    get_subtitle_evidence,
    get_work,
    get_work_log,
    get_workflow,
    import_local_source,
    import_subtitles,
    list_artifacts,
    list_sources,
    list_work,
    manager_doctor,
    manager_status,
    patch_subtitle_cues,
    plan_component_change,
    plan_export_variant,
    plan_orchestrated_workflow,
    plan_workflow,
    preview_subtitles,
    provider_status,
    recommend_next_steps,
    replace_subtitle_text,
    request_subtitle_evidence,
    resolve_subtitle_evidence,
    system_status,
    target_status,
    tts_catalog,
    voice_catalog,
)
from .tools.e2e import audio_cpp_catalogue
from .tools.subtitle_evidence import get_subtitle_evidence_routes
from .tools.workflow_inputs import get_workflow_inputs, select_workflow_input

_STDOUT_GUARD = threading.Lock()


def _tool_failure(error: PandratorMcpError, request_id: str) -> Exception:
    failure = ToolFailure(
        code=TypeAdapter(FailureCode).validate_python(error.code),
        message=str(error),
        request_id=request_id,
        details=error.details,
        retryable=error.retryable,
        next_actions=error.next_actions,
    )
    # Import lazily so ordinary CLI/configuration operations remain usable
    # without importing the protocol runtime. ToolError is the SDK's expected
    # business-failure channel and becomes a normal tool result with isError.
    from mcp.server.mcpserver.exceptions import ToolError

    return ToolError(failure.model_dump_json())


def _guarded_call(function, *args) -> tuple[str, Any]:
    request_id = str(uuid.uuid4())
    tokens = begin_request(request_id)
    try:
        # Some optional ML and Manager dependencies still print diagnostics.
        # Keep those writes on stderr so they can never become protocol frames,
        # including on Windows where descriptor rebinding is not sufficient for
        # every Python stream wrapper. MCP runs synchronous tools in worker
        # threads, so serialize the process-global stream swap as well.
        with _STDOUT_GUARD, redirect_stdout(sys.stderr):
            result = function(*args)
    except PandratorMcpError as error:
        raise _tool_failure(error, request_id) from error
    finally:
        end_request(tokens)
    return request_id, result


def _call(function, *args) -> dict[str, Any]:
    request_id, result = _guarded_call(function, *args)
    if isinstance(result, ToolOutcome):
        return {
            "schema_version": "1",
            "request_id": request_id,
            "result": result.result,
            "work": (result.work.model_dump(mode="json") if result.work is not None else None),
            "warnings": [warning.model_dump(mode="json") for warning in result.warnings],
            "next_actions": [action.model_dump(mode="json") for action in result.next_actions],
        }
    return {
        "schema_version": "1",
        "request_id": request_id,
        "result": result,
        "work": None,
        "warnings": [],
        "next_actions": [],
    }


def _response(envelope: dict[str, Any], mode: str = "standard") -> Any:
    """Opt-in structured transport: full data once, compact text fallback.

    Standard mode retains the SDK's full text serialization for older clients.
    Clients selecting structured mode must consume structuredContent.
    """
    if mode == "standard":
        return envelope
    from mcp.types import CallToolResult, TextContent

    result = envelope.get("result") or {}
    summary = {
        "schema_version": envelope["schema_version"],
        "request_id": envelope["request_id"],
        "data": "structuredContent",
        **{key: result[key] for key in ("run_id", "batch_id", "status", "manifest_hash")
           if isinstance(result, dict) and key in result},
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(summary, separators=(",", ":")))],
        structured_content=envelope,
    )


def _call_with_input_factory(
    function,
    runtime: McpRuntime,
    factory: Callable[[], Any],
) -> dict[str, Any]:
    """Construct tool arguments inside the request and stdout guard."""

    def invoke() -> Any:
        try:
            arguments = factory()
        except ValidationError as error:
            raise PandratorMcpError(
                "validation_error",
                "The tool input is invalid.",
                details={
                    "errors": error.errors(
                        include_url=False,
                        include_context=False,
                        include_input=False,
                    )
                },
            ) from error
        return function(runtime, arguments)

    return _call(invoke)


def _call_with_validated_input(
    function,
    runtime: McpRuntime,
    model: type[Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    return _call_with_input_factory(function, runtime, lambda: model.model_validate(values))


def _resource_call(function, *args) -> str:
    _, result = _guarded_call(function, *args)
    if isinstance(result, ToolOutcome):
        result = result.result
    return json.dumps(result, ensure_ascii=False, indent=2)


def build_server(runtime: McpRuntime):
    """Construct a July 2026 MCP server without importing the SDK eagerly."""

    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as error:
        raise RuntimeError(
            "pandrator-mcp requires the pinned mcp==2.2.0 runtime dependency."
        ) from error

    def registered_tool_schema(name: str) -> dict[str, Any] | None:
        registered = server._tool_manager.get_tool(name)
        return registered.parameters if registered is not None else None

    server = MCPServer(
        "Pandrator",
        version=__version__,
        extensions=[create_argument_validation_extension(registered_tool_schema, _tool_failure)],
        instructions=(
            "Use only the configured target and approved roots; never pass credentials "
            "or connection URLs. For unfamiliar work read recommend_next_steps and "
            "the workflow guide; inspect status/capabilities and live model/voice catalogues. "
            "Passive runs: obey serial/parallel policy, scope leases to batches, return "
            "every required ID once, submit/release the wave, follow next_actions. "
            "Execute exact unexpired plans with reviewed confirmations; poll work to "
            "terminal. Prefer plan_orchestrated_workflow for known session outcomes. "
            "Use filtered describe_parameters. patch_session_settings merges overrides; "
            "update_session_settings replaces them."
        ),
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    revisioned_write_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
    execute_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
    destructive_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    )

    from .tools.audiobook import register_audiobook_tools
    from .tools.generation_controls import register_generation_controls_tools
    from .tools.performance import register_performance_tools
    from .tools.session_branches import register_session_branch_tools
    from .tools.session_purge import register_session_purge_tools
    from .tools.voice_lifecycle import register_voice_lifecycle_tools
    from .tools.voice_metadata import register_voice_metadata_tools
    from .tools.voice_setup import register_voice_setup_tools

    register_performance_tools(server, runtime, _call_with_validated_input,
                               read_only=read_only, write_action=write_action)
    register_generation_controls_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )
    register_audiobook_tools(
        server, runtime, _call_with_validated_input,
        read_only=read_only, write_action=revisioned_write_action,
    )
    register_voice_setup_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=revisioned_write_action,
    )
    register_session_branch_tools(
        server, runtime, _call_with_validated_input,
        read_only=read_only, write_action=revisioned_write_action,
    )
    register_session_purge_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        revisioned_write_action=revisioned_write_action,
        destructive_action=destructive_action,
    )
    register_voice_metadata_tools(
        server,
        runtime,
        _call_with_validated_input,
        write_action=revisioned_write_action,
    )
    register_voice_lifecycle_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    @server.tool(
        name="pandrator_explain_system",
        title="Explain how Pandrator works",
        annotations=read_only,
    )
    def explain_tool(
        topic: GuideTopic = "overview",
        audience: Literal[
            "new_user",
            "operator",
            "developer",
            "administrator",
        ] = "new_user",
        detail: Literal["summary", "full"] = "summary",
        include_live_context: bool = False,
    ) -> dict[str, Any]:
        """Get concise topic guidance; use detail='full' for the complete procedure. Live health/identity lookup is opt-in."""

        return _call_with_input_factory(
            explain_system,
            runtime,
            lambda: ExplainSystemInput(
                topic=topic,
                audience=audience,
                detail=detail,
                include_live_context=include_live_context,
            ),
        )

    @server.tool(
        name="pandrator_recommend_next_steps",
        title="Recommend safe Pandrator next steps",
        annotations=read_only,
    )
    def recommendations_tool(
        session_id: NativeNullableString = None,
        goal: NativeNullableString = None,
    ) -> dict[str, Any]:
        """Recommend inspect-first steps without changing Pandrator."""

        return _call_with_input_factory(
            recommend_next_steps,
            runtime,
            lambda: RecommendNextStepsInput(session_id=session_id, goal=goal),
        )

    @server.tool(
        name="pandrator_get_target_status",
        title="Inspect the configured Pandrator target",
        annotations=read_only,
    )
    def target_tool(
        include_authenticated_identity: bool = True,
    ) -> dict[str, Any]:
        """Inspect target reachability and optional pinned identity."""

        return _call_with_input_factory(
            target_status,
            runtime,
            lambda: TargetStatusInput(
                include_authenticated_identity=include_authenticated_identity,
            ),
        )

    @server.tool(
        name="pandrator_get_system_status",
        title="Inspect Pandrator status",
        annotations=read_only,
    )
    def status_tool(
        include_capabilities: bool = True,
        include_manager: bool = True,
    ) -> dict[str, Any]:
        """Inspect application identity, health, capabilities, and Manager state."""

        return _call_with_input_factory(
            system_status,
            runtime,
            lambda: SystemStatusInput(
                include_capabilities=include_capabilities,
                include_manager=include_manager,
            ),
        )

    @server.tool(
        name="pandrator_get_capabilities",
        title="Inspect Pandrator capabilities",
        annotations=read_only,
    )
    def capabilities_tool() -> dict[str, Any]:
        """Inspect side-effect-free runtime and feature capability probes."""

        return _call_with_input_factory(capabilities, runtime, lambda: CapabilitiesInput())

    @server.tool(
        name="pandrator_browse_local_sources",
        title="Browse approved local source roots",
        annotations=read_only,
    )
    def local_sources_browse_tool(
        root: Annotated[NativeNullableString, Field(max_length=80)] = None,
        directory: Annotated[str, Field(max_length=1024)] = "",
        query: Annotated[NativeNullableString, Field(max_length=160)] = None,
        recursive: bool = False,
        sort: Literal["modified_desc", "name_asc"] = "modified_desc",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List opaque roots or safe relative paths; never expose absolute paths."""

        return _call_with_input_factory(
            browse_local_sources,
            runtime,
            lambda: BrowseLocalSourcesInput(
                root=root,
                directory=directory,
                query=query,
                recursive=recursive,
                sort=sort,
                limit=limit,
            ),
        )

    register_session_library_tools(
        server,
        runtime,
        _call_with_input_factory,
        read_only=read_only,
        revisioned_write_action=revisioned_write_action,
    )

    @server.tool(
        name="pandrator_get_workflow",
        title="Inspect a Pandrator workflow",
        annotations=read_only,
    )
    def workflow_get_tool(session_id: str, response_mode: Literal["standard", "structured"] = "standard") -> dict[str, Any]:
        """Inspect the stages, prerequisites, and selections for one session."""

        envelope = _call_with_input_factory(
            get_workflow,
            runtime,
            lambda: GetWorkflowInput(session_id=session_id),
        )
        return _response(envelope, response_mode)

    @server.tool(name="pandrator_get_workflow_inputs", title="Inspect exact workflow inputs", annotations=read_only)
    def workflow_inputs_get_tool(session_id: str) -> dict[str, Any]:
        """Read exact consumer inputs, revisions, hashes, and blocking reasons."""
        return _call_with_validated_input(get_workflow_inputs, runtime, GetWorkflowInputsInput, {"session_id": session_id})

    @server.tool(name="pandrator_select_workflow_input", title="Select an exact workflow input", annotations=write_action)
    def workflow_input_select_tool(session_id: str, consumer: Literal["translation", "generation"], role: Literal["source", "correction", "translation"], artifact_id: str,
                                   expected_outcome_revision: Annotated[int, Field(ge=0)], expected_selection_revision: Annotated[int, Field(ge=0)], idempotency_key: str,
                                   expected_translation_settings_revision: Annotated[int | None, Field(ge=0)] = None) -> dict[str, Any]:
        """Atomically select an input role/version with current manifest revision fences."""
        return _call_with_validated_input(select_workflow_input, runtime, SelectWorkflowInputInput,
            {key: value for key, value in locals().items() if key in SelectWorkflowInputInput.model_fields})

    register_dispatch_lifecycle_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    @server.tool(name="pandrator_get_subtitle_evidence_routes", title="Inspect available audio evidence engines", annotations=read_only)
    def subtitle_evidence_routes_tool(language: Annotated[NativeNullableString, Field(min_length=2, max_length=40)] = None, include_languages: bool = False) -> dict[str, Any]:
        """Read the live shared engine catalogue; language arrays are opt-in."""
        return _call_with_validated_input(get_subtitle_evidence_routes, runtime, GetSubtitleEvidenceRoutesInput,
            {"language": language, "include_languages": include_languages})

    register_media_edit_tools(
        server,
        runtime,
        _call_with_validated_input,
        _response,
        read_only=read_only,
        write_action=write_action,
        execute_action=execute_action,
    )

    @server.tool(
        name="pandrator_preview_subtitles",
        title="Preview subtitle cues, transcript segments, and translations",
        annotations=read_only,
    )
    def preview_subtitles_tool(
        session_id: str,
        stage: NativeNullableEnum[SubtitleStage] = None,
        artifact_id: NativeNullableString = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        query: NativeNullableString = None,
        around_ordinal: Annotated[int | None, Field(ge=1)] = None,
        context: Annotated[int, Field(ge=0, le=20)] = 3,
        start_ordinal: Annotated[int | None, Field(ge=1)] = None,
        end_ordinal: Annotated[int | None, Field(ge=1)] = None,
        response_mode: Literal["standard", "structured"] = "standard",
    ) -> dict[str, Any]:
        """Preview paginated cues and transcript segments inline without downloading."""

        envelope = _call_with_input_factory(
            preview_subtitles,
            runtime,
            lambda: PreviewSubtitlesInput(
                session_id=session_id,
                stage=stage,
                artifact_id=artifact_id,
                offset=offset,
                limit=limit,
                query=query,
                around_ordinal=around_ordinal,
                context=context,
                start_ordinal=start_ordinal,
                end_ordinal=end_ordinal,
            ),
        )
        return _response(envelope, response_mode)

    @server.tool(
        name="pandrator_replace_subtitle_text",
        title="Find and replace text across subtitle cues with revision guard",
        annotations=write_action,
    )
    def replace_subtitle_text_tool(
        session_id: str,
        stage: SubtitleStage,
        expected_revision: Annotated[int, Field(ge=1)],
        search_text: Annotated[str, Field(min_length=1, max_length=500)],
        replacement_text: Annotated[str, Field(max_length=500)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        match_case: bool = False,
        whole_word: bool = True,
        is_regex: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Find and replace text across subtitle cues with revision guard."""

        return _call_with_input_factory(
            replace_subtitle_text,
            runtime,
            lambda: ReplaceSubtitleTextInput(
                session_id=session_id,
                stage=stage,
                expected_revision=expected_revision,
                search_text=search_text,
                replacement_text=replacement_text,
                match_case=match_case,
                whole_word=whole_word,
                is_regex=is_regex,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_patch_subtitle_cues",
        title="Patch specific subtitle cues by ordinal with revision guard",
        annotations=write_action,
    )
    def patch_subtitle_cues_tool(
        session_id: str,
        stage: SubtitleStage,
        expected_revision: Annotated[int, Field(ge=1)],
        cues: list[dict[str, Any]],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Patch specific subtitle cues by ordinal while preserving all other cues."""

        def make_arguments() -> PatchSubtitleCuesInput:
            parsed_cues = [
                CuePatchInput.model_validate(
                    {
                        "ordinal": item.get("ordinal"),
                        "text": item.get("text"),
                        "speaker": item.get("speaker"),
                        "start_ms": item.get("start_ms"),
                        "end_ms": item.get("end_ms"),
                    }
                )
                for item in cues
            ]
            return PatchSubtitleCuesInput(
                session_id=session_id,
                stage=stage,
                expected_revision=expected_revision,
                cues=parsed_cues,
                idempotency_key=idempotency_key,
            )

        return _call_with_input_factory(patch_subtitle_cues, runtime, make_arguments)

    @server.tool(
        name="pandrator_import_subtitles",
        title="Import reviewed subtitles from raw SRT text or file",
        annotations=write_action,
    )
    def import_subtitles_tool(
        session_id: str,
        stage: SubtitleStage,
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        srt_content: NativeNullableString = None,
        filename: NativeNullableString = None,
    ) -> dict[str, Any]:
        """Import reviewed subtitles from raw SRT text or file."""

        return _call_with_input_factory(
            import_subtitles,
            runtime,
            lambda: ImportSubtitlesInput(
                session_id=session_id,
                stage=stage,
                expected_revision=expected_revision,
                srt_content=srt_content,
                filename=filename,
                idempotency_key=idempotency_key,
            ),
        )

    register_session_settings_read_tools(
        server,
        runtime,
        _call_with_input_factory,
        read_only=read_only,
    )

    @server.tool(
        name="pandrator_describe_parameters",
        title="Discover filtered Pandrator parameter definitions",
        annotations=read_only,
    )
    def describe_parameters_tool(
        sections: tuple[
            Literal[
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
            ...,
        ] = (),
        names: tuple[Annotated[str, Field(min_length=1, max_length=50)], ...] = (),
        workflow_kind: NativeNullableEnum[
            Literal["audiobook", "subtitles", "voiceover", "media_edit"]
        ] = None,
        query: Annotated[NativeNullableString, Field(max_length=100)] = None,
        limit: Annotated[int, Field(ge=1, le=300)] = 100,
    ) -> dict[str, Any]:
        """Discover only definitions matching at least one supplied filter."""

        return _call_with_input_factory(
            describe_parameters,
            runtime,
            lambda: DescribeParametersInput(
                sections=sections,
                names=names,
                workflow_kind=workflow_kind,
                query=query,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_list_sources",
        title="List reusable Pandrator source assets",
        annotations=read_only,
    )
    def sources_tool(
        state: Literal["current", "trashed"] = "current",
        query: Annotated[NativeNullableString, Field(max_length=160)] = None,
        kind: Annotated[NativeNullableString, Field(max_length=80)] = None,
        mime_type: Annotated[NativeNullableString, Field(max_length=160)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List bounded source metadata without paths or source contents."""

        return _call_with_input_factory(
            list_sources,
            runtime,
            lambda: ListSourcesInput(
                state=state,
                query=query,
                kind=kind,
                mime_type=mime_type,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_create_text_source",
        title="Create and attach a plain-text source",
        annotations=write_action,
    )
    def text_source_create_tool(
        session_id: str,
        text: Annotated[str, Field(min_length=1, max_length=1_000_000)],
        expected_session_revision: Annotated[int, Field(ge=1)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        filename: Annotated[str, Field(min_length=1, max_length=255)] = "inline.txt",
        role: Literal["primary", "reference", "transcript", "media"] = "primary",
    ) -> dict[str, Any]:
        """Store supplied UTF-8 text as a managed source and attach it once."""

        return _call_with_input_factory(
            create_text_source,
            runtime,
            lambda: CreateTextSourceInput(
                session_id=session_id,
                text=text,
                filename=filename,
                role=role,
                expected_session_revision=expected_session_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_import_local_source",
        title="Import and attach a local source",
        annotations=write_action,
    )
    def local_source_import_tool(
        session_id: str,
        root: Annotated[str, Field(min_length=1, max_length=80)],
        relative_path: Annotated[str, Field(min_length=1, max_length=2048)],
        expected_session_revision: Annotated[int, Field(ge=1)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        role: Literal["primary", "reference", "transcript", "media"] = "primary",
    ) -> dict[str, Any]:
        """Stream one approved local file via resumable upload and attach it once."""

        return _call_with_input_factory(
            import_local_source,
            runtime,
            lambda: ImportLocalSourceInput(
                session_id=session_id,
                root=root,
                relative_path=relative_path,
                role=role,
                expected_session_revision=expected_session_revision,
                idempotency_key=idempotency_key,
            ),
        )

    register_transcription_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    register_dispatch_run_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    @server.tool(
        name="pandrator_request_subtitle_evidence",
        title="Escalate a subtitle cue for audio evidence",
        annotations=write_action,
    )
    def subtitle_evidence_request_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        source_artifact_id: Annotated[str, Field(min_length=1, max_length=80)],
        cue_id: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=4_000)],
        routes: Annotated[
            list[Literal["whisper", "parakeet", "moss", "qwen3", "azure_mai_transcribe_2", "azure_mai_transcribe_1_5", "audio_llm"]],
            Field(min_length=1, max_length=7),
        ],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        padding_before_ms: Annotated[int, Field(ge=0, le=15_000)] = 2_000,
        padding_after_ms: Annotated[int, Field(ge=0, le=15_000)] = 2_000,
        force_refresh: bool = False,
        audio_model_ids: Annotated[list[str] | None, Field(max_length=3)] = None,
    ) -> dict[str, Any]:
        """Queue independent, bounded re-transcriptions for one exact cue."""

        return _call_with_validated_input(request_subtitle_evidence, runtime, RequestSubtitleEvidenceInput,
            {key: value for key, value in locals().items() if key in RequestSubtitleEvidenceInput.model_fields and value is not None})

    @server.tool(
        name="pandrator_get_subtitle_evidence",
        title="Inspect subtitle audio evidence",
        annotations=read_only,
    )
    def subtitle_evidence_get_tool(evidence_id: str, response_mode: Literal["standard", "structured"] = "standard") -> dict[str, Any]:
        """Read candidate transcripts, provenance, timing, and cost status."""

        envelope = _call_with_input_factory(
            get_subtitle_evidence,
            runtime,
            lambda: GetSubtitleEvidenceInput(evidence_id=evidence_id),
        )
        return _response(envelope, response_mode)

    @server.tool(
        name="pandrator_resolve_subtitle_evidence",
        title="Resolve subtitle audio evidence",
        annotations=write_action,
    )
    def subtitle_evidence_resolve_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        evidence_id: Annotated[str, Field(min_length=1, max_length=120)],
        action: Literal["accepted", "edited", "deleted", "uncertain", "dismissed"],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        candidate_id: Annotated[
            NativeNullableString,
            Field(min_length=1, max_length=120),
        ] = None,
        text: Annotated[NativeNullableString, Field(max_length=16_000)] = None,
        note: Annotated[str, Field(max_length=4_000)] = "",
    ) -> dict[str, Any]:
        """Record the explicit editorial disposition of an evidence request."""

        return _call_with_input_factory(
            resolve_subtitle_evidence,
            runtime,
            lambda: ResolveSubtitleEvidenceInput(
                session_id=session_id,
                evidence_id=evidence_id,
                action=action,
                candidate_id=candidate_id,
                text=text,
                note=note,
                idempotency_key=idempotency_key,
            ),
        )

    register_dispatch_batch_tools(
        server,
        runtime,
        _call_with_validated_input,
        _response,
        read_only=read_only,
        write_action=write_action,
    )

    register_source_cleaning_dispatch_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    register_speech_optimization_dispatch_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    register_media_edit_dispatch_tools(
        server,
        runtime,
        _call_with_validated_input,
        read_only=read_only,
        write_action=write_action,
    )

    register_session_setup_tools(
        server,
        runtime,
        _call_with_input_factory,
        write_action=write_action,
    )

    register_session_settings_write_tools(
        server,
        runtime,
        _call_with_input_factory,
        write_action=write_action,
    )

    @server.tool(
        name="pandrator_delete_output",
        title="Permanently delete one session output file",
        annotations=destructive_action,
    )
    def output_delete_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        artifact_id: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> dict[str, Any]:
        """Permanently remove only the requested output file by artifact ID."""

        return _call_with_input_factory(
            delete_output,
            runtime,
            lambda: DeleteOutputInput(
                session_id=session_id,
                artifact_id=artifact_id,
            ),
        )

    @server.tool(
        name="pandrator_plan_workflow",
        title="Preview an exact Pandrator workflow plan",
        annotations=read_only,
    )
    def workflow_plan_tool(
        session_id: str,
        target_stage: Literal[
            "transcribe",
            "correct",
            "translate",
            "clean_source",
            "prepare_text",
            "optimize_document",
            "optimize_tts",
            "generate_audio",
            "export",
        ] = "generate_audio",
        overrides: dict[str, Any] | None = None,
        expires_in_minutes: Annotated[
            int,
            Field(ge=1, le=60),
        ] = 30,
    ) -> dict[str, Any]:
        """Preview stages, reuse, providers, disclosures, locks, and confirmations."""

        return _call_with_input_factory(
            plan_workflow,
            runtime,
            lambda: PlanWorkflowInput(
                session_id=session_id,
                target_stage=target_stage,
                overrides=overrides or {},
                expires_in_minutes=expires_in_minutes,
            ),
        )

    @server.tool(
        name="pandrator_plan_orchestrated_workflow",
        title="Plan a model-orchestrated Pandrator workflow",
        annotations=read_only,
    )
    def orchestrated_workflow_plan_tool(
        session_id: Annotated[str, Field(min_length=1, max_length=80)],
        goal: Annotated[str, Field(min_length=1, max_length=1_000)],
        passive_stages: tuple[
            Literal["correction", "translation", "speech_optimization"], ...
        ] = (),
        final_stage: Literal["generate_audio", "export"] = "export",
        overrides: dict[str, Any] | None = None,
        export_mode: Literal["media", "audio", "subtitles", "text"] = "media",
        audio_mode: Literal["preserve", "mixed", "dubbing_only"] = "mixed",
        subtitle_mode: Literal["none", "soft", "burned"] = "none",
        subtitle_selection: Literal["source", "translation", "dual"] = "translation",
        subtitle_format: Literal["srt", "vtt"] = "srt",
        execution_mode: Literal["serial", "parallel"] = "serial",
        max_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 1,
        context_capsule: DelegationContextCapsuleInput | None = None,
        materialize: bool = False,
        filename: Annotated[NativeNullableString, Field(max_length=255)] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=3_600)] = 0,
        expires_in_minutes: Annotated[int, Field(ge=1, le=60)] = 30,
    ) -> dict[str, Any]:
        """Describe live-inherited passive loops and the deferred native plan.

        Passive settings are inherited from the live session and safe overrides;
        generated keys identify retries for that resolved procedure. Typed export
        settings affect the native plan, while materialization and filename are
        delivery controls handled after the export artifact exists.
        """

        values = {**locals(), "passive_stages": passive_stages, "final_stage": final_stage}
        return _call_with_validated_input(
            plan_orchestrated_workflow, runtime, PlanOrchestratedWorkflowInput,
            {key: value for key, value in values.items()
             if key in PlanOrchestratedWorkflowInput.model_fields and value is not None},
        )

    @server.tool(
        name="pandrator_plan_export_variant",
        title="Preview a typed export variant",
        annotations=read_only,
    )
    def export_variant_plan_tool(
        session_id: str,
        generation_run_id: Annotated[NativeNullableString, Field(max_length=80)] = None,
        export_mode: Literal["media", "audio", "subtitles", "text"] = "media",
        audio_mode: Literal["preserve", "mixed", "dubbing_only"] = "mixed",
        subtitle_mode: Literal["none", "soft", "burned"] = "none",
        subtitle_selection: Literal["source", "translation", "dual"] = "translation",
        subtitle_format: Literal["srt", "vtt"] = "srt",
        expires_in_minutes: Annotated[int, Field(ge=1, le=60)] = 30,
    ) -> dict[str, Any]:
        """Create the ordinary immutable workflow plan for one explicit output."""

        return _call_with_input_factory(
            plan_export_variant,
            runtime,
            lambda: PlanExportVariantInput(
                session_id=session_id,
                generation_run_id=generation_run_id,
                export_mode=export_mode,
                audio_mode=audio_mode,
                subtitle_mode=subtitle_mode,
                subtitle_selection=subtitle_selection,
                subtitle_format=subtitle_format,
                expires_in_minutes=expires_in_minutes,
            ),
        )

    @server.tool(
        name="pandrator_execute_workflow_plan",
        title="Execute an exact reviewed Pandrator workflow plan",
        annotations=execute_action,
    )
    def workflow_execute_tool(
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Consume an unchanged plan once and return its durable work handle."""

        return _call_with_input_factory(
            execute_workflow_plan,
            runtime,
            lambda: ExecuteWorkflowPlanInput(
                plan_id=plan_id,
                plan_digest=plan_digest,
                accepted_confirmations=accepted_confirmations,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_list_artifacts",
        title="List Pandrator artifact metadata",
        annotations=read_only,
    )
    def artifacts_tool(
        session_id: NativeNullableString = None,
        kind: NativeNullableString = None,
        role: NativeNullableString = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List bounded artifact metadata without paths or content."""

        return _call_with_input_factory(
            list_artifacts,
            runtime,
            lambda: ListArtifactsInput(
                session_id=session_id,
                kind=kind,
                role=role,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_download_artifact",
        title="Download an artifact to the approved output root",
        annotations=write_action,
    )
    def artifact_download_tool(
        artifact_id: Annotated[str, Field(min_length=1, max_length=80)],
        filename: Annotated[NativeNullableString, Field(max_length=255)] = None,
    ) -> dict[str, Any]:
        """Resume and verify one immutable artifact without exposing server paths."""

        return _call_with_input_factory(
            download_artifact,
            runtime,
            lambda: DownloadArtifactInput(
                artifact_id=artifact_id,
                filename=filename,
            ),
        )

    @server.tool(
        name="pandrator_get_provider_status",
        title="Inspect Pandrator provider readiness",
        annotations=read_only,
    )
    def providers_tool(
        include_disabled: bool = True,
    ) -> dict[str, Any]:
        """Inspect providers without credential references or values."""

        return _call_with_input_factory(
            provider_status,
            runtime,
            lambda: ProviderStatusInput(include_disabled=include_disabled),
        )

    @server.tool(
        name="pandrator_get_tts_catalog",
        title="Inspect TTS services, models, and voices",
        annotations=read_only,
    )
    def tts_catalog_tool(
        service_id: Annotated[NativeNullableString, Field(max_length=160)] = None,
        include_compatibility: bool = False,
        model: Annotated[NativeNullableString, Field(max_length=300)] = None,
        query: Annotated[NativeNullableString, Field(max_length=160)] = None,
        available_only: bool = False,
        detail: Literal["summary", "full"] = "summary",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Resolve current TTS choices without exposing credentials or endpoints."""

        return _call_with_input_factory(
            tts_catalog,
            runtime,
            lambda: TtsCatalogInput(
                service_id=service_id,
                include_compatibility=include_compatibility,
                model=model,
                query=query,
                available_only=available_only,
                detail=detail,
                refresh=refresh,
            ),
        )

    @server.tool(
        name="pandrator_get_audio_cpp_catalogue",
        title="Browse the audio.cpp catalogue",
        annotations=read_only,
    )
    def audio_cpp_catalogue_tool(
        category: Annotated[str, Field(max_length=160)] = "",
        family: Annotated[str, Field(max_length=160)] = "",
        query: Annotated[str, Field(max_length=160)] = "",
        language: Annotated[str, Field(max_length=160)] = "",
        capability: Annotated[str, Field(max_length=160)] = "",
        commercial_use: Literal[
            "",
            "permitted",
            "noncommercial",
            "conditional",
            "unknown",
        ] = "",
        recommended_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
        offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
    ) -> dict[str, Any]:
        """Browse all versioned audio.cpp families and packages; catalogue presence is not installation or acoustic validation."""

        return _call_with_validated_input(
            audio_cpp_catalogue,
            runtime,
            AudioCppCatalogueInput,
            {
                "category": category,
                "family": family,
                "query": query,
                "language": language,
                "capability": capability,
                "commercial_use": commercial_use,
                "recommended_only": recommended_only,
                "limit": limit,
                "offset": offset,
            },
        )

    @server.tool(
        name="pandrator_configure_tts",
        title="Configure a catalog-backed TTS selection",
        annotations=write_action,
    )
    def tts_configure_tool(
        session_id: str,
        service_id: Annotated[str, Field(min_length=1, max_length=160)],
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        model: Annotated[NativeNullableString, Field(max_length=300)] = None,
        voice: Annotated[NativeNullableString, Field(max_length=300)] = None,
        language: Annotated[
            NativeNullableString,
            Field(min_length=2, max_length=40),
        ] = None,
        style_instructions: Annotated[
            NativeNullableString,
            Field(max_length=12_000),
        ] = None,
        tts_context_mode: NativeNullableEnum[Literal["off", "before", "both"]] = None,
        performance_context_before: Annotated[int | None, Field(ge=0, le=20)] = None,
        performance_context_after: Annotated[int | None, Field(ge=0, le=20)] = None,
        performance_context_max_chars: Annotated[int | None, Field(ge=0, le=16_000)] = None,
        performance_allow_vocalizations: bool | None = None,
        elevenlabs_voice_settings: ElevenLabsVoiceSettingsInput | None = None,
    ) -> dict[str, Any]:
        """Validate exact catalog IDs and update only the session's TTS override."""

        return _call_with_input_factory(
            configure_tts,
            runtime,
            lambda: ConfigureTtsInput(
                session_id=session_id,
                service_id=service_id,
                model=model,
                voice=voice,
                language=language,
                style_instructions=style_instructions,
                tts_context_mode=tts_context_mode,
                performance_context_before=performance_context_before,
                performance_context_after=performance_context_after,
                performance_context_max_chars=performance_context_max_chars,
                performance_allow_vocalizations=performance_allow_vocalizations,
                elevenlabs_voice_settings=elevenlabs_voice_settings,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_get_voice_catalog",
        title="Inspect the Pandrator voice catalog",
        annotations=read_only,
    )
    def voices_tool(
        query: Annotated[str, Field(max_length=300)] = "",
        language: Annotated[str, Field(max_length=40)] = "",
        accent: Annotated[str, Field(max_length=80)] = "",
        voice_category: Literal["", "male", "female", "androgynous", "unspecified"] = "",
        pitch: Literal["", "low", "mid", "high"] = "",
        texture: Annotated[str, Field(max_length=40)] = "",
        use_case: Annotated[str, Field(max_length=40)] = "",
        collection_id: Annotated[str, Field(max_length=160)] = "",
        kind: Literal["all", "managed", "provider"] = "all",
        origin: Annotated[str, Field(max_length=40)] = "",
        service_id: Annotated[str, Field(max_length=160)] = "",
        model: Annotated[str, Field(max_length=200)] = "",
        ready_only: bool = False,
        reviewed_only: bool = False,
        sort: Literal["relevance", "name", "recently_added", "recently_updated"] = "relevance",
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
        cursor: Annotated[NativeNullableString, Field(max_length=1024)] = None,
    ) -> dict[str, Any]:
        """Inspect the normalized voice catalog with bounded filters and cursors."""

        return _call_with_input_factory(
            voice_catalog,
            runtime,
            lambda: VoiceCatalogInput(
                query=query,
                language=language,
                accent=accent,
                voice_category=voice_category,
                pitch=pitch,
                texture=texture,
                use_case=use_case,
                collection_id=collection_id,
                kind=kind,
                origin=origin,
                service_id=service_id,
                model=model,
                ready_only=ready_only,
                reviewed_only=reviewed_only,
                sort=sort,
                limit=limit,
                cursor=cursor,
            ),
        )

    register_generation_plan_tools(
        server, runtime, _call, _call_with_validated_input,
        read_only=read_only, write_action=write_action,
    )

    @server.tool(name="pandrator_adopt_subtitle_source", title="Adopt an existing managed subtitle source", annotations=write_action)
    def adopt_subtitle_source_tool(session_id: str, source_asset_id: str, idempotency_key: str, expected_revision: int | None = None) -> dict[str, Any]:
        """Materialize a timed subtitle revision from an attached primary SRT/VTT, without another upload. Identical content is reused; reviewed derivatives are preserved."""
        return _call_with_validated_input(adopt_subtitle_source, runtime, AdoptSubtitleSourceInput, {key: value for key, value in locals().items() if key in AdoptSubtitleSourceInput.model_fields})

    register_generation_execution_tools(
        server, runtime, _call, _call_with_validated_input,
        write_action=write_action, execute_action=execute_action,
    )

    @server.tool(
        name="pandrator_list_work",
        title="List durable Pandrator work",
        annotations=read_only,
    )
    def work_list_tool(
        session_id: NativeNullableString = None,
        kinds: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List payload-free, redacted durable work projections."""

        return _call_with_input_factory(
            list_work,
            runtime,
            lambda: ListWorkInput(
                session_id=session_id,
                kinds=kinds,
                states=states,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_get_work",
        title="Inspect durable Pandrator work",
        annotations=read_only,
    )
    def work_get_tool(
        work_id: str,
        work_type: Literal["job", "manager_operation"] = "job",
        include_events: bool = False,
        event_limit: Annotated[int, Field(ge=1, le=200)] = 50,
        wait_seconds: Annotated[int, Field(ge=0, le=3_600)] = 0,
    ) -> dict[str, Any]:
        """Inspect work, optionally polling until terminal or the wait deadline."""

        return _call_with_input_factory(
            get_work,
            runtime,
            lambda: GetWorkInput(
                work_type=work_type,
                work_id=work_id,
                include_events=include_events,
                event_limit=event_limit,
                wait_seconds=wait_seconds,
            ),
        )

    @server.tool(
        name="pandrator_get_work_log",
        title="Inspect a redacted Pandrator work log",
        annotations=read_only,
    )
    def work_log_tool(
        work_id: str,
        work_type: Literal["job", "manager_operation"] = "job",
        after: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Inspect bounded redacted events or Manager task summaries."""

        return _call_with_input_factory(
            get_work_log,
            runtime,
            lambda: GetWorkLogInput(
                work_type=work_type,
                work_id=work_id,
                after=after,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_cancel_work",
        title="Cancel durable Pandrator work",
        annotations=execute_action,
    )
    def work_cancel_tool(
        work_id: str,
        idempotency_key: str,
        work_type: Literal["job", "manager_operation"] = "job",
    ) -> dict[str, Any]:
        """Request retry-safe cancellation for one exact application job."""

        return _call_with_input_factory(
            cancel_work,
            runtime,
            lambda: CancelWorkInput(
                work_type=work_type,
                work_id=work_id,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_manager_status",
        title="Inspect Pandrator Manager status",
        annotations=read_only,
    )
    def manager_status_tool() -> dict[str, Any]:
        """Inspect Manager state through the target's approved gateway."""

        return _call(manager_status, runtime)

    @server.tool(
        name="pandrator_manager_doctor",
        title="Inspect Pandrator Manager diagnostics",
        annotations=read_only,
    )
    def manager_doctor_tool() -> dict[str, Any]:
        """Inspect Manager host diagnostics without making repairs."""

        return _call(manager_doctor, runtime)

    @server.tool(
        name="pandrator_plan_component_change",
        title="Preview an exact Manager component plan",
        annotations=read_only,
    )
    def manager_plan_tool(
        kind: Literal["install", "update", "repair", "remove"],
        components: tuple[ManagerDesiredComponentInput, ...],
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a bounded immutable Manager plan for exact components."""

        return _call_with_input_factory(
            plan_component_change,
            runtime,
            lambda: PlanComponentChangeInput(
                kind=kind,
                components=components,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_execute_component_plan",
        title="Execute an exact reviewed Manager component plan",
        annotations=execute_action,
    )
    def manager_execute_plan_tool(
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Submit only the exact Manager plan digest the user reviewed."""

        return _call_with_input_factory(
            execute_component_plan,
            runtime,
            lambda: ExecuteComponentPlanInput(
                plan_id=plan_id,
                plan_digest=plan_digest,
                accepted_confirmations=accepted_confirmations,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_control_runtime",
        title="Control Pandrator or managed service runtime",
        annotations=execute_action,
    )
    def manager_runtime_tool(
        action: Literal["start", "stop", "restart"],
        runtime_target: Literal[
            "application",
            "managed_services",
        ],
        service_ids: tuple[str, ...],
        confirmation: Literal["runtime-control"],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Perform one explicitly reviewed, idempotent runtime action."""

        return _call_with_input_factory(
            control_runtime,
            runtime,
            lambda: ControlRuntimeInput(
                action=action,
                runtime_target=runtime_target,
                service_ids=service_ids,
                confirmation=confirmation,
                idempotency_key=idempotency_key,
            ),
        )

    register_resources(server, runtime, _resource_call)
    register_prompts(server)

    # MCP 2.2.0 derives a tool's schema from its flat Python signature and has
    # no public hook for a cross-field constraint. Keep the runtime validator
    # in strict input models and add compact conditionals to the flat schemas so
    # clients cannot mistake serial/2 or parallel/1 for documented-valid input.
    for tool_name in (
        "pandrator_create_dispatch_run",
        "pandrator_create_speech_optimization_dispatch_run",
        "pandrator_plan_orchestrated_workflow",
    ):
        registered_tool = server._tool_manager.get_tool(tool_name)
        if registered_tool is None:  # pragma: no cover - registration invariant
            raise RuntimeError(f"Missing registered MCP tool: {tool_name}")
        execution_policy_json_schema(registered_tool.parameters)

    # The native-call extension enforces this before generated argument models
    # can discard extra keys; nested schemas keep their own existing policies.
    for registered_tool in server._tool_manager.list_tools():
        registered_tool.parameters["additionalProperties"] = False

    return server
