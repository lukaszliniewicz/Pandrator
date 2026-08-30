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
    AttachExistingSourceInput,
    CancelWorkInput,
    CapabilitiesInput,
    ClaimDispatchBatchInput,
    ControlRuntimeInput,
    CreateDispatchRunInput,
    CreateSessionInput,
    DispatchStructuredResultInput,
    ExecuteComponentPlanInput,
    ExecuteWorkflowPlanInput,
    ExplainSystemInput,
    GetDispatchRunInput,
    GetSessionInput,
    GetSessionSettingsInput,
    GetWorkflowInput,
    GetWorkInput,
    GetWorkLogInput,
    GuideTopic,
    ListArtifactsInput,
    ListDispatchRunsInput,
    ListSessionsInput,
    ListSourcesInput,
    ListWorkInput,
    ManagerDesiredComponentInput,
    PlanComponentChangeInput,
    PlanWorkflowInput,
    ProviderStatusInput,
    RecommendNextStepsInput,
    ReleaseDispatchBatchInput,
    RenewDispatchBatchInput,
    SubmitDispatchBatchInput,
    SystemStatusInput,
    TargetStatusInput,
    UpdateSessionInput,
    UpdateSessionSettingsInput,
    VoiceCatalogInput,
)
from .tools import (
    attach_existing_source,
    cancel_work,
    capabilities,
    claim_dispatch_batch,
    control_runtime,
    create_dispatch_run,
    create_session,
    execute_component_plan,
    execute_workflow_plan,
    explain_system,
    get_dispatch_run,
    get_session,
    get_session_settings,
    get_work,
    get_work_log,
    get_workflow,
    list_artifacts,
    list_dispatch_runs,
    list_sessions,
    list_sources,
    list_work,
    manager_doctor,
    manager_status,
    plan_component_change,
    plan_workflow,
    provider_status,
    recommend_next_steps,
    release_dispatch_batch,
    renew_dispatch_batch,
    submit_dispatch_batch,
    system_status,
    target_status,
    update_session,
    update_session_settings,
    voice_catalog,
)

_STDOUT_GUARD = threading.Lock()


def _tool_failure(error: PandratorMcpError, request_id: str) -> RuntimeError:
    failure = ToolFailure(
        code=error.code,
        message=str(error),
        request_id=request_id,
        details=error.details,
        retryable=error.retryable,
    )
    return RuntimeError(failure.model_dump_json())


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
            "Explain Pandrator using packaged guides and inspect only the "
            "configured target. Never ask for connection URLs or credentials "
            "inside tool calls. Consequential workflow execution must use an "
            "exact, unexpired plan and the confirmations the user reviewed."
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
        name="pandrator_list_sessions",
        title="List Pandrator sessions",
        annotations=read_only,
    )
    def sessions_tool(
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        workflow_kind: Literal["audiobook", "subtitles", "voiceover"] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """List bounded session summaries from the configured target."""

        return _call(
            list_sessions,
            runtime,
            ListSessionsInput(
                limit=limit,
                workflow_kind=workflow_kind,
                state=state,
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
        name="pandrator_list_sources",
        title="List reusable Pandrator source assets",
        annotations=read_only,
    )
    def sources_tool(
        state: Literal["current", "trashed"] = "current",
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List bounded source metadata without paths or source contents."""

        return _call(
            list_sources,
            runtime,
            ListSourcesInput(state=state, limit=limit),
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
    ) -> dict[str, Any]:
        """Create a run; then claim batches sequentially for correction or translation."""

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
        values.update(
            {
                key: value
                for key, value in optional.items()
                if value is not None
            }
        )
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
    ) -> dict[str, Any]:
        """Inspect one payload-free work item and an optional redacted event tail."""

        return _call(
            get_work,
            runtime,
            GetWorkInput(
                work_type=work_type,
                work_id=work_id,
                include_events=include_events,
                event_limit=event_limit,
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

    return server
