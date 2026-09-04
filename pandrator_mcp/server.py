"""July 2026 MCP adapter for Pandrator guidance and bounded automation."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from contextlib import redirect_stdout
from typing import Annotated, Any, Literal

from pydantic import Field

from . import __version__
from .context import McpRuntime
from .errors import PandratorMcpError, ToolFailure
from .request_context import begin_request, end_request
from .results import ToolOutcome
from .schemas import (
    AssembleGenerationRunInput,
    AttachExistingSourceInput,
    BrowseLocalSourcesInput,
    CancelWorkInput,
    CapabilitiesInput,
    ClaimDispatchBatchInput,
    ClaimSourceCleaningDispatchBatchInput,
    ClaimSpeechOptimizationDispatchBatchInput,
    ConfigureTtsInput,
    ControlRuntimeInput,
    CreateDispatchRunInput,
    CreateSessionInput,
    CreateSourceCleaningDispatchRunInput,
    CreateSpeechOptimizationDispatchRunInput,
    CreateTextSourceInput,
    CuePatchInput,
    DescribeParametersInput,
    DispatchStructuredResultInput,
    DownloadArtifactInput,
    ExecuteComponentPlanInput,
    ExecuteWorkflowPlanInput,
    ExplainSystemInput,
    GetDispatchRunInput,
    GetSessionInput,
    GetSessionSettingsInput,
    GetSourceCleaningDispatchRunInput,
    GetSpeechOptimizationDispatchRunInput,
    GetWorkflowInput,
    GetWorkInput,
    GetWorkLogInput,
    GuideTopic,
    ImportLocalSourceInput,
    ImportSubtitlesInput,
    InspectSourceCleaningDispatchExtractionInput,
    ListArtifactsInput,
    ListDispatchRunsInput,
    ListGenerationRunsInput,
    ListGenerationSegmentsInput,
    ListSessionsInput,
    ListSourceCleaningDispatchRunsInput,
    ListSourcesInput,
    ListSpeechOptimizationDispatchRunsInput,
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
    RegenerateSegmentsInput,
    ReleaseDispatchBatchInput,
    ReleaseSourceCleaningDispatchBatchInput,
    ReleaseSpeechOptimizationDispatchBatchInput,
    RenewDispatchBatchInput,
    RenewSourceCleaningDispatchBatchInput,
    RenewSpeechOptimizationDispatchBatchInput,
    ReplaceSubtitleTextInput,
    SelectTakeInput,
    SourceCleaningDispatchResultInput,
    SpeechOptimizationDispatchResultInput,
    SubmitDispatchBatchInput,
    SubmitSourceCleaningDispatchBatchInput,
    SubmitSpeechOptimizationDispatchBatchInput,
    SubtitleStage,
    SystemStatusInput,
    TargetStatusInput,
    TtsCatalogInput,
    UpdateGenerationSegmentInput,
    UpdateSessionInput,
    UpdateSessionSettingsInput,
    VoiceCatalogInput,
)
from .schemas.delegation import execution_policy_json_schema
from .tools import (
    assemble_generation_run,
    attach_existing_source,
    browse_local_sources,
    cancel_work,
    capabilities,
    claim_dispatch_batch,
    claim_source_cleaning_dispatch_batch,
    claim_speech_optimization_dispatch_batch,
    configure_tts,
    control_runtime,
    create_dispatch_run,
    create_session,
    create_source_cleaning_dispatch_run,
    create_speech_optimization_dispatch_run,
    create_text_source,
    describe_parameters,
    download_artifact,
    execute_component_plan,
    execute_workflow_plan,
    explain_system,
    get_dispatch_run,
    get_session,
    get_session_settings,
    get_source_cleaning_dispatch_run,
    get_speech_optimization_dispatch_run,
    get_work,
    get_work_log,
    get_workflow,
    import_local_source,
    import_subtitles,
    inspect_source_cleaning_dispatch_extraction,
    list_artifacts,
    list_dispatch_runs,
    list_generation_runs,
    list_generation_segments,
    list_sessions,
    list_source_cleaning_dispatch_runs,
    list_sources,
    list_speech_optimization_dispatch_runs,
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
    regenerate_segments,
    release_dispatch_batch,
    release_source_cleaning_dispatch_batch,
    release_speech_optimization_dispatch_batch,
    renew_dispatch_batch,
    renew_source_cleaning_dispatch_batch,
    renew_speech_optimization_dispatch_batch,
    replace_subtitle_text,
    select_take,
    submit_dispatch_batch,
    submit_source_cleaning_dispatch_batch,
    submit_speech_optimization_dispatch_batch,
    system_status,
    target_status,
    tts_catalog,
    update_generation_segment,
    update_session,
    update_session_settings,
    voice_catalog,
)

_STDOUT_GUARD = threading.Lock()


def _tool_failure(error: PandratorMcpError, request_id: str) -> Exception:
    failure = ToolFailure(
        code=error.code,
        message=str(error),
        request_id=request_id,
        details=error.details,
        retryable=error.retryable,
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
            "pandrator-mcp requires the pinned mcp==2.1.1 runtime dependency."
        ) from error

    server = MCPServer(
        "Pandrator",
        version=__version__,
        instructions=(
            "Start unfamiliar work with pandrator_recommend_next_steps, the "
            "guide index, target status, and capabilities. Operate only on the "
            "configured target and approved local path roots; never request "
            "connection URLs or credentials in tool arguments. Resolve TTS "
            "service, model, and voice names from the live catalog instead of "
            "assuming examples exist. Passive workflows use the run's selected "
            "serial or bounded-parallel policy: keep every lease scoped to its "
            "batch, return every required ID exactly once, submit or release all "
            "batches in the current wave, and follow next_actions until complete. "
            "Consequential execution must "
            "use an exact unexpired plan and every reviewed confirmation. Poll "
            "durable work until terminal before using its outputs. For a known "
            "session outcome, prefer pandrator_plan_orchestrated_workflow as the "
            "single-turn procedure layer; it defers the immutable native plan "
            "until passive artifacts are complete. Use filtered "
            "pandrator_describe_parameters when exact setting definitions are needed."
        ),
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    execute_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
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
        include_live_context: bool = True,
    ) -> dict[str, Any]:
        """Explain one advertised topic with deterministic, versioned guidance."""

        return _call(
            explain_system,
            runtime,
            ExplainSystemInput(
                topic=topic,
                audience=audience,
                include_live_context=include_live_context,
            ),
        )

    @server.tool(
        name="pandrator_recommend_next_steps",
        title="Recommend safe Pandrator next steps",
        annotations=read_only,
    )
    def recommendations_tool(
        session_id: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """Recommend inspect-first steps without changing Pandrator."""

        return _call(
            recommend_next_steps,
            runtime,
            RecommendNextStepsInput(session_id=session_id, goal=goal),
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

        return _call(
            target_status,
            runtime,
            TargetStatusInput(
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

        return _call(
            system_status,
            runtime,
            SystemStatusInput(
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

        return _call(capabilities, runtime, CapabilitiesInput())

    @server.tool(
        name="pandrator_browse_local_sources",
        title="Browse approved local source roots",
        annotations=read_only,
    )
    def local_sources_browse_tool(
        root: Annotated[str | None, Field(max_length=80)] = None,
        directory: Annotated[str, Field(max_length=1024)] = "",
        query: Annotated[str | None, Field(max_length=160)] = None,
        recursive: bool = False,
        sort: Literal["modified_desc", "name_asc"] = "modified_desc",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List opaque roots or safe relative paths; never expose absolute paths."""

        return _call(
            browse_local_sources,
            runtime,
            BrowseLocalSourcesInput(
                root=root,
                directory=directory,
                query=query,
                recursive=recursive,
                sort=sort,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_list_sessions",
        title="List Pandrator sessions",
        annotations=read_only,
    )
    def sessions_tool(
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        workflow_kind: Literal["audiobook", "subtitles", "voiceover"] | None = None,
        state: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """List bounded session summaries from the configured target."""

        return _call(
            list_sessions,
            runtime,
            ListSessionsInput(
                limit=limit,
                workflow_kind=workflow_kind,
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

        return _call(
            get_session,
            runtime,
            GetSessionInput(session_id=session_id),
        )

    @server.tool(
        name="pandrator_get_workflow",
        title="Inspect a Pandrator workflow",
        annotations=read_only,
    )
    def workflow_get_tool(session_id: str) -> dict[str, Any]:
        """Inspect the stages, prerequisites, and selections for one session."""

        return _call(
            get_workflow,
            runtime,
            GetWorkflowInput(session_id=session_id),
        )

    @server.tool(
        name="pandrator_preview_subtitles",
        title="Preview subtitle cues, transcript segments, and translations",
        annotations=read_only,
    )
    def preview_subtitles_tool(
        session_id: str,
        stage: SubtitleStage | None = None,
        artifact_id: str | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        query: str | None = None,
        around_ordinal: Annotated[int | None, Field(ge=1)] = None,
        context: Annotated[int, Field(ge=0, le=20)] = 3,
        start_ordinal: Annotated[int | None, Field(ge=1)] = None,
        end_ordinal: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict[str, Any]:
        """Preview paginated cues and transcript segments inline without downloading."""

        return _call(
            preview_subtitles,
            runtime,
            PreviewSubtitlesInput(
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

        return _call(
            replace_subtitle_text,
            runtime,
            ReplaceSubtitleTextInput(
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

        parsed_cues = [
            CuePatchInput(
                ordinal=int(item["ordinal"]),
                text=item.get("text"),
                speaker=item.get("speaker"),
                start_ms=item.get("start_ms"),
                end_ms=item.get("end_ms"),
            )
            for item in cues
        ]
        return _call(
            patch_subtitle_cues,
            runtime,
            PatchSubtitleCuesInput(
                session_id=session_id,
                stage=stage,
                expected_revision=expected_revision,
                cues=parsed_cues,
                idempotency_key=idempotency_key,
            ),
        )

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
        srt_content: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Import reviewed subtitles from raw SRT text or file."""

        return _call(
            import_subtitles,
            runtime,
            ImportSubtitlesInput(
                session_id=session_id,
                stage=stage,
                expected_revision=expected_revision,
                srt_content=srt_content,
                filename=filename,
                idempotency_key=idempotency_key,
            ),
        )

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

        return _call(
            get_session_settings,
            runtime,
            GetSessionSettingsInput(
                session_id=session_id,
                section=section,
            ),
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
        workflow_kind: Literal["audiobook", "subtitles", "voiceover"] | None = None,
        query: Annotated[str | None, Field(max_length=100)] = None,
        limit: Annotated[int, Field(ge=1, le=300)] = 100,
    ) -> dict[str, Any]:
        """Discover only definitions matching at least one supplied filter."""

        return _call(
            describe_parameters,
            runtime,
            DescribeParametersInput(
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
        query: Annotated[str | None, Field(max_length=160)] = None,
        kind: Annotated[str | None, Field(max_length=80)] = None,
        mime_type: Annotated[str | None, Field(max_length=160)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List bounded source metadata without paths or source contents."""

        return _call(
            list_sources,
            runtime,
            ListSourcesInput(
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
        role: Literal["primary", "reference"] = "primary",
    ) -> dict[str, Any]:
        """Store supplied UTF-8 text as a managed source and attach it once."""

        return _call(
            create_text_source,
            runtime,
            CreateTextSourceInput(
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
        role: Literal["primary", "reference"] = "primary",
    ) -> dict[str, Any]:
        """Stream one approved local file via resumable upload and attach it once."""

        return _call(
            import_local_source,
            runtime,
            ImportLocalSourceInput(
                session_id=session_id,
                root=root,
                relative_path=relative_path,
                role=role,
                expected_session_revision=expected_session_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_create_dispatch_run",
        title="Create a subtitle dispatch run",
        annotations=write_action,
    )
    def dispatch_create_tool(
        session_id: str,
        kind: Literal["correction", "translation"],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        instructions: Annotated[str, Field(max_length=16_000)] = "",
        source_artifact_id: Annotated[
            str | None,
            Field(min_length=1, max_length=80),
        ] = None,
        source_language: Annotated[
            str | None,
            Field(min_length=2, max_length=40),
        ] = None,
        target_language: Annotated[
            str | None,
            Field(min_length=2, max_length=40),
        ] = None,
        char_limit: Annotated[int, Field(ge=1, le=100_000)] = 6_000,
        max_segments_per_batch: Annotated[
            int,
            Field(ge=1, le=500),
        ] = 40,
        no_remove_subtitles: bool = False,
        context_before: Annotated[int, Field(ge=0, le=20)] = 8,
        context_after: Annotated[int, Field(ge=0, le=20)] = 2,
        timing_context_mode: Literal["full", "overlap_only", "none"] = "full",
        substantial_gap_ms: Annotated[
            int,
            Field(ge=0, le=60_000),
        ] = 2_000,
        glossary: dict[str, str] | None = None,
        execution_mode: Literal["serial", "parallel"] = "serial",
        max_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 1,
        context_capsule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a serial or bounded-parallel correction/translation run."""

        return _call(
            create_dispatch_run,
            runtime,
            CreateDispatchRunInput(
                session_id=session_id,
                kind=kind,
                source_artifact_id=source_artifact_id,
                source_language=source_language,
                target_language=target_language,
                instructions=instructions,
                char_limit=char_limit,
                max_segments_per_batch=max_segments_per_batch,
                no_remove_subtitles=no_remove_subtitles,
                context_before=context_before,
                context_after=context_after,
                timing_context_mode=timing_context_mode,
                substantial_gap_ms=substantial_gap_ms,
                glossary=glossary or {},
                execution_mode=execution_mode,
                max_parallel_batches=max_parallel_batches,
                context_capsule=context_capsule or {},
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_list_dispatch_runs",
        title="List subtitle dispatch runs",
        annotations=read_only,
    )
    def dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List dispatch metadata only; canonical task packets appear on claim."""

        return _call(
            list_dispatch_runs,
            runtime,
            ListDispatchRunsInput(session_id=session_id, limit=limit),
        )

    @server.tool(
        name="pandrator_get_dispatch_run",
        title="Inspect a subtitle dispatch run",
        annotations=read_only,
    )
    def dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect run metadata and final artifact state without batch content."""

        return _call(
            get_dispatch_run,
            runtime,
            GetDispatchRunInput(run_id=run_id),
        )

    @server.tool(
        name="pandrator_claim_dispatch_batch",
        title="Claim a subtitle dispatch batch",
        annotations=write_action,
    )
    def dispatch_claim_tool(
        run_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Claim one canonical task packet; each cue and timing value appears once."""

        return _call(
            claim_dispatch_batch,
            runtime,
            ClaimDispatchBatchInput(
                run_id=run_id,
                lease_seconds=lease_seconds,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_renew_dispatch_batch",
        title="Renew a subtitle dispatch lease",
        annotations=write_action,
    )
    def dispatch_renew_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Renew only the matching batch lease; keep lease_token scoped to this batch."""

        return _call(
            renew_dispatch_batch,
            runtime,
            RenewDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_release_dispatch_batch",
        title="Release a subtitle dispatch lease",
        annotations=write_action,
    )
    def dispatch_release_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Release a claimed batch with its matching lease_token before retrying later."""

        return _call(
            release_dispatch_batch,
            runtime,
            ReleaseDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_submit_dispatch_batch",
        title="Submit a subtitle dispatch batch",
        annotations=write_action,
    )
    def dispatch_submit_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        result: DispatchStructuredResultInput | None = None,
        response_text: Annotated[str | None, Field(max_length=524_288)] = None,
    ) -> dict[str, Any]:
        """Submit one typed result; response_text is a legacy compatibility path."""

        return _call(
            submit_dispatch_batch,
            runtime,
            SubmitDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                result=result,
                response_text=response_text,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_create_source_cleaning_dispatch_run",
        title="Create a passive PDF/EPUB cleanup run",
        annotations=write_action,
    )
    def source_cleaning_dispatch_create_tool(
        session_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        source_artifact_id: Annotated[
            str | None,
            Field(min_length=1, max_length=80),
        ] = None,
        instructions: Annotated[str, Field(max_length=16_000)] = "",
        evidence_limit: Annotated[int, Field(ge=20, le=2_000)] = 500,
        remove_footnotes: bool | None = None,
        filter_citations: bool | None = None,
        pdf_ocr_mode: Literal["auto", "off", "force"] | None = None,
        pdf_ocr_language: Annotated[
            str | None,
            Field(min_length=2, max_length=80),
        ] = None,
        pdf_ocr_dpi: Annotated[int | None, Field(ge=120, le=400)] = None,
        pdf_remove_toc: bool | None = None,
        pdf_remove_repeated_marginals: bool | None = None,
    ) -> dict[str, Any]:
        """Queue deterministic preparation; no model provider or token budget is used."""

        return _call(
            create_source_cleaning_dispatch_run,
            runtime,
            CreateSourceCleaningDispatchRunInput(
                session_id=session_id,
                source_artifact_id=source_artifact_id,
                instructions=instructions,
                evidence_limit=evidence_limit,
                remove_footnotes=remove_footnotes,
                filter_citations=filter_citations,
                pdf_ocr_mode=pdf_ocr_mode,
                pdf_ocr_language=pdf_ocr_language,
                pdf_ocr_dpi=pdf_ocr_dpi,
                pdf_remove_toc=pdf_remove_toc,
                pdf_remove_repeated_marginals=pdf_remove_repeated_marginals,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_list_source_cleaning_dispatch_runs",
        title="List passive PDF/EPUB cleanup runs",
        annotations=read_only,
    )
    def source_cleaning_dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List run metadata without exposing book text or phase evidence."""

        return _call(
            list_source_cleaning_dispatch_runs,
            runtime,
            ListSourceCleaningDispatchRunsInput(
                session_id=session_id,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_get_source_cleaning_dispatch_run",
        title="Inspect a passive PDF/EPUB cleanup run",
        annotations=read_only,
    )
    def source_cleaning_dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect preparation, progress, validation, and final artifact metadata."""

        return _call(
            get_source_cleaning_dispatch_run,
            runtime,
            GetSourceCleaningDispatchRunInput(run_id=run_id),
        )

    @server.tool(
        name="pandrator_claim_source_cleaning_dispatch_batch",
        title="Claim a PDF/EPUB cleanup phase",
        annotations=write_action,
    )
    def source_cleaning_dispatch_claim_tool(
        run_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Claim one rich editorial packet with bounded book evidence."""

        return _call(
            claim_source_cleaning_dispatch_batch,
            runtime,
            ClaimSourceCleaningDispatchBatchInput(
                run_id=run_id,
                lease_seconds=lease_seconds,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_renew_source_cleaning_dispatch_batch",
        title="Renew a PDF/EPUB cleanup lease",
        annotations=write_action,
    )
    def source_cleaning_dispatch_renew_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Renew only the matching editorial phase lease."""

        return _call(
            renew_source_cleaning_dispatch_batch,
            runtime,
            RenewSourceCleaningDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_release_source_cleaning_dispatch_batch",
        title="Release a PDF/EPUB cleanup lease",
        annotations=write_action,
    )
    def source_cleaning_dispatch_release_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Return an unfinished editorial phase to the ready queue."""

        return _call(
            release_source_cleaning_dispatch_batch,
            runtime,
            ReleaseSourceCleaningDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_inspect_source_cleaning_dispatch_extraction",
        title="Inspect a leased PDF/EPUB extraction",
        annotations=write_action,
    )
    def source_cleaning_dispatch_inspect_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        action: Literal[
            "batch",
            "inspect_document_structure",
            "inspect_navigation",
            "search",
            "regex_search",
            "preview",
            "inspect_block",
            "get_epub_markup_for_text",
            "preview_raw_markup_range",
            "list_epub_selectors",
            "preview_selector",
            "list_repeated_lines",
            "find_heading_candidates",
            "analyze_chapter_structure",
            "analyze_cleanup_structure",
            "find_footnote_candidates",
            "find_metadata_candidates",
        ],
        arguments: Annotated[dict[str, Any], Field(max_length=100)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        view: Literal["working", "baseline", "source"] = "working",
    ) -> dict[str, Any]:
        """Browse/search the full pinned extraction and authorize returned blocks."""

        return _call(
            inspect_source_cleaning_dispatch_extraction,
            runtime,
            InspectSourceCleaningDispatchExtractionInput(
                batch_id=batch_id,
                lease_token=lease_token,
                action=action,
                arguments=arguments,
                view=view,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_submit_source_cleaning_dispatch_batch",
        title="Submit a PDF/EPUB cleanup phase",
        annotations=write_action,
    )
    def source_cleaning_dispatch_submit_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        result: SourceCleaningDispatchResultInput,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Submit typed proposal decisions and optional phase-scoped operations."""

        return _call(
            submit_source_cleaning_dispatch_batch,
            runtime,
            SubmitSourceCleaningDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                result=result,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_create_speech_optimization_dispatch_run",
        title="Create a passive speech-optimization run",
        annotations=write_action,
    )
    def speech_optimization_dispatch_create_tool(
        session_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        source_artifact_id: Annotated[
            str | None,
            Field(min_length=1, max_length=80),
        ] = None,
        language: Annotated[
            str | None,
            Field(min_length=1, max_length=40),
        ] = None,
        voice_language: Annotated[
            str | None,
            Field(min_length=1, max_length=40),
        ] = None,
        tts_service: Annotated[
            str | None,
            Field(min_length=1, max_length=80),
        ] = None,
        instructions: Annotated[str, Field(max_length=16_000)] = "",
        char_limit: Annotated[int, Field(ge=1, le=1_000_000)] = 20_000,
        max_units_per_batch: Annotated[int, Field(ge=1, le=500)] = 100,
        context_before: Annotated[int, Field(ge=0, le=20)] = 4,
        context_after: Annotated[int, Field(ge=0, le=20)] = 2,
        include_timing: bool = True,
        execution_mode: Literal["serial", "parallel"] = "serial",
        max_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 1,
        context_capsule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue serial or bounded-parallel speech-text batches for this MCP model."""

        return _call(
            create_speech_optimization_dispatch_run,
            runtime,
            CreateSpeechOptimizationDispatchRunInput(
                session_id=session_id,
                source_artifact_id=source_artifact_id,
                language=language,
                voice_language=voice_language,
                tts_service=tts_service,
                instructions=instructions,
                char_limit=char_limit,
                max_units_per_batch=max_units_per_batch,
                context_before=context_before,
                context_after=context_after,
                include_timing=include_timing,
                execution_mode=execution_mode,
                max_parallel_batches=max_parallel_batches,
                context_capsule=context_capsule or {},
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_list_speech_optimization_dispatch_runs",
        title="List passive speech-optimization runs",
        annotations=read_only,
    )
    def speech_optimization_dispatch_list_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List run metadata without exposing speech text or lease capabilities."""

        return _call(
            list_speech_optimization_dispatch_runs,
            runtime,
            ListSpeechOptimizationDispatchRunsInput(
                session_id=session_id,
                limit=limit,
            ),
        )

    @server.tool(
        name="pandrator_get_speech_optimization_dispatch_run",
        title="Inspect a passive speech-optimization run",
        annotations=read_only,
    )
    def speech_optimization_dispatch_get_tool(run_id: str) -> dict[str, Any]:
        """Inspect progress and final artifact metadata without batch contents."""

        return _call(
            get_speech_optimization_dispatch_run,
            runtime,
            GetSpeechOptimizationDispatchRunInput(run_id=run_id),
        )

    @server.tool(
        name="pandrator_claim_speech_optimization_dispatch_batch",
        title="Claim a passive speech-text batch",
        annotations=write_action,
    )
    def speech_optimization_dispatch_claim_tool(
        run_id: str,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Claim the next sequential units plus read-only boundary context."""

        return _call(
            claim_speech_optimization_dispatch_batch,
            runtime,
            ClaimSpeechOptimizationDispatchBatchInput(
                run_id=run_id,
                lease_seconds=lease_seconds,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_renew_speech_optimization_dispatch_batch",
        title="Renew a speech-optimization lease",
        annotations=write_action,
    )
    def speech_optimization_dispatch_renew_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
        lease_seconds: Annotated[int, Field(ge=30, le=3_600)] = 900,
    ) -> dict[str, Any]:
        """Renew only the matching speech-text batch lease."""

        return _call(
            renew_speech_optimization_dispatch_batch,
            runtime,
            RenewSpeechOptimizationDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_release_speech_optimization_dispatch_batch",
        title="Release a speech-optimization lease",
        annotations=write_action,
    )
    def speech_optimization_dispatch_release_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Return an unfinished speech-text batch to the ready queue."""

        return _call(
            release_speech_optimization_dispatch_batch,
            runtime,
            ReleaseSpeechOptimizationDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_submit_speech_optimization_dispatch_batch",
        title="Submit a passive speech-text batch",
        annotations=write_action,
    )
    def speech_optimization_dispatch_submit_tool(
        batch_id: str,
        lease_token: Annotated[str, Field(min_length=1, max_length=160)],
        result: SpeechOptimizationDispatchResultInput,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=8,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
            ),
        ],
    ) -> dict[str, Any]:
        """Return every unit exactly once so Pandrator can materialize the revision."""

        return _call(
            submit_speech_optimization_dispatch_batch,
            runtime,
            SubmitSpeechOptimizationDispatchBatchInput(
                batch_id=batch_id,
                lease_token=lease_token,
                result=result,
                idempotency_key=idempotency_key,
            ),
        )

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
        ] = "audiobook",
        source_language: Annotated[
            str,
            Field(min_length=2, max_length=40),
        ] = "auto",
        target_language: Annotated[
            str | None,
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
            ],
            ...,
        ] = (),
    ) -> dict[str, Any]:
        """Create one session; retries with the same key replay the first result."""

        return _call(
            create_session,
            runtime,
            CreateSessionInput(
                name=name,
                workflow_kind=workflow_kind,
                source_language=source_language,
                target_language=target_language,
                workflow_preset=workflow_preset,
                included_stages=included_stages,
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
            str | None,
            Field(min_length=1, max_length=200),
        ] = None,
        workflow_kind: Literal[
            "audiobook",
            "subtitles",
            "voiceover",
        ]
        | None = None,
        source_language: Annotated[
            str | None,
            Field(min_length=2, max_length=40),
        ] = None,
        target_language: Annotated[
            str | None,
            Field(min_length=2, max_length=40),
        ] = None,
        workflow_preset: Annotated[
            str | None,
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
        return _call(
            update_session,
            runtime,
            UpdateSessionInput.model_validate(values),
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
        role: Literal["primary", "reference"] = "primary",
    ) -> dict[str, Any]:
        """Attach one existing source when the session revision still matches."""

        return _call(
            attach_existing_source,
            runtime,
            AttachExistingSourceInput(
                session_id=session_id,
                source_asset_id=source_asset_id,
                role=role,
                expected_session_revision=expected_session_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_update_session_settings",
        title="Update one Pandrator session settings section",
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
        """Replace one settings override using revision-safe idempotency."""

        return _call(
            update_session_settings,
            runtime,
            UpdateSessionSettingsInput(
                session_id=session_id,
                section=section,
                expected_revision=expected_revision,
                value=value,
                idempotency_key=idempotency_key,
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

        return _call(
            plan_workflow,
            runtime,
            PlanWorkflowInput(
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
        export_mode: Literal["media", "subtitles", "text"] = "media",
        audio_mode: Literal["preserve", "mixed", "dubbing_only"] = "mixed",
        subtitle_mode: Literal["none", "soft", "burned"] = "none",
        subtitle_selection: Literal["source", "translation", "dual"] = "translation",
        subtitle_format: Literal["srt", "vtt"] = "srt",
        execution_mode: Literal["serial", "parallel"] = "serial",
        max_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 1,
        context_capsule: dict[str, Any] | None = None,
        materialize: bool = False,
        filename: Annotated[str | None, Field(max_length=255)] = None,
        wait_seconds: Annotated[int, Field(ge=0, le=3_600)] = 0,
        expires_in_minutes: Annotated[int, Field(ge=1, le=60)] = 30,
    ) -> dict[str, Any]:
        """Describe live-inherited passive loops and the deferred native plan.

        Passive settings are inherited from the live session and safe overrides;
        generated keys identify retries for that resolved procedure. Typed export
        settings affect the native plan, while materialization and filename are
        delivery controls handled after the export artifact exists.
        """

        return _call(
            plan_orchestrated_workflow,
            runtime,
            PlanOrchestratedWorkflowInput(
                session_id=session_id,
                goal=goal,
                passive_stages=passive_stages,
                final_stage=final_stage,
                overrides=overrides or {},
                export_mode=export_mode,
                audio_mode=audio_mode,
                subtitle_mode=subtitle_mode,
                subtitle_selection=subtitle_selection,
                subtitle_format=subtitle_format,
                execution_mode=execution_mode,
                max_parallel_batches=max_parallel_batches,
                context_capsule=context_capsule or {},
                materialize=materialize,
                filename=filename,
                wait_seconds=wait_seconds,
                expires_in_minutes=expires_in_minutes,
            ),
        )

    @server.tool(
        name="pandrator_plan_export_variant",
        title="Preview a typed export variant",
        annotations=read_only,
    )
    def export_variant_plan_tool(
        session_id: str,
        generation_run_id: Annotated[str | None, Field(max_length=80)] = None,
        export_mode: Literal["media", "subtitles", "text"] = "media",
        audio_mode: Literal["preserve", "mixed", "dubbing_only"] = "mixed",
        subtitle_mode: Literal["none", "soft", "burned"] = "none",
        subtitle_selection: Literal["source", "translation", "dual"] = "translation",
        subtitle_format: Literal["srt", "vtt"] = "srt",
        expires_in_minutes: Annotated[int, Field(ge=1, le=60)] = 30,
    ) -> dict[str, Any]:
        """Create the ordinary immutable workflow plan for one explicit output."""

        return _call(
            plan_export_variant,
            runtime,
            PlanExportVariantInput(
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

        return _call(
            execute_workflow_plan,
            runtime,
            ExecuteWorkflowPlanInput(
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
        session_id: str | None = None,
        kind: str | None = None,
        role: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List bounded artifact metadata without paths or content."""

        return _call(
            list_artifacts,
            runtime,
            ListArtifactsInput(
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
        filename: Annotated[str | None, Field(max_length=255)] = None,
    ) -> dict[str, Any]:
        """Resume and verify one immutable artifact without exposing server paths."""

        return _call(
            download_artifact,
            runtime,
            DownloadArtifactInput(
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

        return _call(
            provider_status,
            runtime,
            ProviderStatusInput(include_disabled=include_disabled),
        )

    @server.tool(
        name="pandrator_get_tts_catalog",
        title="Inspect TTS services, models, and voices",
        annotations=read_only,
    )
    def tts_catalog_tool(
        service_id: Annotated[str | None, Field(max_length=160)] = None,
        model: Annotated[str | None, Field(max_length=300)] = None,
        query: Annotated[str | None, Field(max_length=160)] = None,
        available_only: bool = False,
        detail: Literal["summary", "full"] = "summary",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Resolve current TTS choices without exposing credentials or endpoints."""

        return _call(
            tts_catalog,
            runtime,
            TtsCatalogInput(
                service_id=service_id,
                model=model,
                query=query,
                available_only=available_only,
                detail=detail,
                refresh=refresh,
            ),
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
        model: Annotated[str | None, Field(max_length=300)] = None,
        voice: Annotated[str | None, Field(max_length=300)] = None,
        language: Annotated[
            str | None,
            Field(min_length=2, max_length=40),
        ] = None,
        style_instructions: Annotated[
            str | None,
            Field(max_length=12_000),
        ] = None,
    ) -> dict[str, Any]:
        """Validate exact catalog IDs and update only the session's TTS override."""

        return _call(
            configure_tts,
            runtime,
            ConfigureTtsInput(
                session_id=session_id,
                service_id=service_id,
                model=model,
                voice=voice,
                language=language,
                style_instructions=style_instructions,
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
        language: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        """Inspect bounded voice metadata without samples or content."""

        return _call(
            voice_catalog,
            runtime,
            VoiceCatalogInput(language=language, limit=limit),
        )

    @server.tool(
        name="pandrator_list_generation_runs",
        title="List reviewable generation runs",
        annotations=read_only,
    )
    def generation_runs_tool(
        session_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """List generation run summaries for review or export selection."""

        return _call(
            list_generation_runs,
            runtime,
            ListGenerationRunsInput(session_id=session_id, limit=limit),
        )

    @server.tool(
        name="pandrator_list_generation_segments",
        title="List generation segments and audio takes",
        annotations=read_only,
    )
    def generation_segments_tool(
        session_id: str,
        cursor: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        generation_run_id: str | None = None,
    ) -> dict[str, Any]:
        """List generation segments, assigned voices, takes, and text."""

        return _call(
            list_generation_segments,
            runtime,
            ListGenerationSegmentsInput(
                session_id=session_id,
                cursor=cursor,
                limit=limit,
                generation_run_id=generation_run_id,
            ),
        )

    @server.tool(
        name="pandrator_update_generation_segment",
        title="Update generation segment text or voice overrides",
        annotations=write_action,
    )
    def generation_segment_update_tool(
        session_id: str,
        segment_id: str,
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        optimized_text: Annotated[str | None, Field(max_length=2000)] = None,
        voice_id: Annotated[str | None, Field(max_length=100)] = None,
        voice: Annotated[str | None, Field(max_length=100)] = None,
        language: Annotated[str | None, Field(max_length=20)] = None,
    ) -> dict[str, Any]:
        """Update a generation segment's text or voice override with revision guard."""

        return _call(
            update_generation_segment,
            runtime,
            UpdateGenerationSegmentInput(
                session_id=session_id,
                segment_id=segment_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                optimized_text=optimized_text,
                voice_id=voice_id,
                voice=voice,
                language=language,
            ),
        )

    @server.tool(
        name="pandrator_select_take",
        title="Select an alternative audio take for a generation segment",
        annotations=write_action,
    )
    def generation_select_take_tool(
        segment_id: str,
        take_id: str,
        expected_revision: Annotated[int, Field(ge=0)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Select an alternative synthesized take for a segment."""

        return _call(
            select_take,
            runtime,
            SelectTakeInput(
                segment_id=segment_id,
                take_id=take_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_regenerate_segments",
        title="Trigger targeted synthesis for specific generation segments",
        annotations=execute_action,
    )
    def generation_regenerate_segments_tool(
        session_id: str,
        segment_ids: list[str],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> dict[str, Any]:
        """Trigger targeted synthesis for a specific list of segment IDs."""

        return _call(
            regenerate_segments,
            runtime,
            RegenerateSegmentsInput(
                session_id=session_id,
                segment_ids=segment_ids,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_assemble_generation_run",
        title="Assemble the session using takes current at a generation run",
        annotations=execute_action,
    )
    def generation_assemble_tool(
        session_id: str,
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        generation_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Assemble the whole session, using the selected/current takes at that run. For a single review clip, download the generation take artifact exposed by pandrator_list_generation_segments instead."""

        return _call(
            assemble_generation_run,
            runtime,
            AssembleGenerationRunInput(
                session_id=session_id,
                generation_run_id=generation_run_id,
                idempotency_key=idempotency_key,
            ),
        )

    @server.tool(
        name="pandrator_list_work",
        title="List durable Pandrator work",
        annotations=read_only,
    )
    def work_list_tool(
        session_id: str | None = None,
        kinds: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List payload-free, redacted durable work projections."""

        return _call(
            list_work,
            runtime,
            ListWorkInput(
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

        return _call(
            get_work,
            runtime,
            GetWorkInput(
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

        return _call(
            get_work_log,
            runtime,
            GetWorkLogInput(
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

        return _call(
            cancel_work,
            runtime,
            CancelWorkInput(
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

        return _call(
            plan_component_change,
            runtime,
            PlanComponentChangeInput(
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

        return _call(
            execute_component_plan,
            runtime,
            ExecuteComponentPlanInput(
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

        return _call(
            control_runtime,
            runtime,
            ControlRuntimeInput(
                action=action,
                runtime_target=runtime_target,
                service_ids=service_ids,
                confirmation=confirmation,
                idempotency_key=idempotency_key,
            ),
        )

    @server.resource("pandrator://guide/index")
    def guide_index_resource() -> str:
        """List deterministic packaged Pandrator guides."""

        return _resource_call(runtime.guides.index)

    @server.resource("pandrator://guide/{topic}")
    def guide_resource(topic: str) -> str:
        """Read one deterministic packaged Pandrator guide."""

        return _resource_call(runtime.guides.get, topic)

    @server.resource("pandrator://target/current")
    def target_resource() -> str:
        """Inspect the current target without requiring authentication."""

        return _resource_call(
            target_status,
            runtime,
            TargetStatusInput(include_authenticated_identity=False),
        )

    @server.resource("pandrator://live/status")
    def live_status_resource() -> str:
        """Inspect current application and Manager status."""

        return _resource_call(system_status, runtime, SystemStatusInput())

    @server.resource("pandrator://live/capabilities")
    def live_capabilities_resource() -> str:
        """Inspect current capabilities."""

        return _resource_call(capabilities, runtime, CapabilitiesInput())

    @server.resource("pandrator://sessions/{session_id}/workflow")
    def workflow_resource(session_id: str) -> str:
        """Inspect one live workflow snapshot."""

        return _resource_call(
            get_workflow,
            runtime,
            GetWorkflowInput(session_id=session_id),
        )

    @server.resource("pandrator://work/{work_type}/{work_id}")
    def work_resource(
        work_type: Literal["job", "manager_operation"],
        work_id: str,
    ) -> str:
        """Inspect one application or Manager work item."""

        arguments = GetWorkInput(
            work_type=work_type,
            work_id=work_id,
            include_events=False,
        )
        return _resource_call(get_work, runtime, arguments)

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
            "Claim one sequential batch, obey the packet's operation contract, return "
            "every required item ID exactly once, submit the typed result, and follow "
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

    # MCP 2.1.1 derives a tool's schema from its flat Python signature and has
    # no public hook for a cross-field constraint. Keep the runtime validator
    # in the strict input models and augment the three exposed flat schemas so
    # clients cannot mistake serial/2 or parallel/1 for documented-valid input.
    for tool_name in (
        "pandrator_create_dispatch_run",
        "pandrator_create_speech_optimization_dispatch_run",
        "pandrator_plan_orchestrated_workflow",
    ):
        registered_tool = server._tool_manager.get_tool(tool_name)
        if registered_tool is None:  # pragma: no cover - registration invariant
            raise RuntimeError(f"Missing registered MCP tool: {tool_name}")
        registered_tool.parameters.update(execution_policy_json_schema())

    return server
