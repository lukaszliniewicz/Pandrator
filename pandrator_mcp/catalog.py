"""Declarative MCP action catalog, independent of handlers and transport."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RiskClass(StrEnum):
    READ = "read"
    WRITE = "write"
    RUN = "run"
    CANCEL = "cancel"
    MANAGER_RUNTIME = "manager_runtime"
    MANAGER_MUTATE = "manager_mutate"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    title: str
    input_model: str
    risk: RiskClass
    required_scope: str | None
    downstream_operation_id: str | None
    method: str | None
    path: str | None
    enabled: bool
    requires_idempotency: bool = False
    requires_confirmation: bool = False

    @property
    def mutating(self) -> bool:
        return self.risk != RiskClass.READ


class ActionCatalog:
    def __init__(self, actions: tuple[ActionSpec, ...]):
        self._actions = {action.name: action for action in actions}
        if len(self._actions) != len(actions):
            raise ValueError("Action names must be unique.")

    def get(self, name: str) -> ActionSpec:
        return self._actions[name]

    def list(self) -> tuple[ActionSpec, ...]:
        return tuple(self._actions[name] for name in sorted(self._actions))


ACTION_CATALOG = ActionCatalog(
    (
        ActionSpec(
            "pandrator_explain_system",
            "Explain Pandrator",
            "ExplainSystemInput",
            RiskClass.READ,
            None,
            None,
            None,
            None,
            True,
        ),
        ActionSpec(
            "pandrator_recommend_next_steps",
            "Recommend safe next steps",
            "RecommendNextStepsInput",
            RiskClass.READ,
            "app.read",
            None,
            None,
            None,
            True,
        ),
        ActionSpec(
            "pandrator_get_target_status",
            "Inspect configured target status",
            "TargetStatusInput",
            RiskClass.READ,
            None,
            "getHealth",
            "GET",
            "/api/v1/health",
            True,
        ),
        ActionSpec(
            "pandrator_get_system_status",
            "Inspect Pandrator status",
            "SystemStatusInput",
            RiskClass.READ,
            "app.read",
            "getHealth",
            "GET",
            "/api/v1/health",
            True,
        ),
        ActionSpec(
            "pandrator_get_capabilities",
            "Inspect Pandrator capabilities",
            "CapabilitiesInput",
            RiskClass.READ,
            "app.read",
            "getCapabilities",
            "GET",
            "/api/v1/capabilities",
            True,
        ),
        ActionSpec(
            "pandrator_list_sessions",
            "List Pandrator sessions",
            "ListSessionsInput",
            RiskClass.READ,
            "app.read",
            "listSessions",
            "GET",
            "/api/v1/sessions",
            True,
        ),
        ActionSpec(
            "pandrator_get_session",
            "Inspect a Pandrator session",
            "GetSessionInput",
            RiskClass.READ,
            "app.read",
            "getSession",
            "GET",
            "/api/v1/sessions/{sessionId}",
            True,
        ),
        ActionSpec(
            "pandrator_get_workflow",
            "Inspect a session workflow",
            "GetWorkflowInput",
            RiskClass.READ,
            "app.read",
            "getWorkflow",
            "GET",
            "/api/v1/sessions/{sessionId}/workflow",
            True,
        ),
        ActionSpec(
            "pandrator_get_session_settings",
            "Inspect effective session settings",
            "GetSessionSettingsInput",
            RiskClass.READ,
            "app.read",
            "getSessionSettings",
            "GET",
            "/api/v1/sessions/{sessionId}/settings/{section}",
            True,
        ),
        ActionSpec(
            "pandrator_list_sources",
            "List reusable source assets",
            "ListSourcesInput",
            RiskClass.READ,
            "app.read",
            "listSourceAssets",
            "GET",
            "/api/v1/sources",
            True,
        ),
        ActionSpec(
            "pandrator_list_artifacts",
            "List artifact metadata",
            "ListArtifactsInput",
            RiskClass.READ,
            "app.read",
            "listArtifacts",
            "GET",
            "/api/v1/artifacts",
            True,
        ),
        ActionSpec(
            "pandrator_get_provider_status",
            "Inspect provider readiness",
            "ProviderStatusInput",
            RiskClass.READ,
            "app.read",
            "listProviders",
            "GET",
            "/api/v1/providers",
            True,
        ),
        ActionSpec(
            "pandrator_get_voice_catalog",
            "Inspect the voice catalog",
            "VoiceCatalogInput",
            RiskClass.READ,
            "app.read",
            "listVoices",
            "GET",
            "/api/v1/voices",
            True,
        ),
        ActionSpec(
            "pandrator_list_work",
            "List durable work",
            "ListWorkInput",
            RiskClass.READ,
            "app.read",
            "listWork",
            "GET",
            "/api/v1/work",
            True,
        ),
        ActionSpec(
            "pandrator_get_work",
            "Inspect durable work",
            "GetWorkInput",
            RiskClass.READ,
            "app.read",
            "getWork",
            "GET",
            "/api/v1/work/{jobId}",
            True,
        ),
        ActionSpec(
            "pandrator_get_work_log",
            "Inspect a redacted work log",
            "GetWorkLogInput",
            RiskClass.READ,
            "app.read",
            "listWorkEvents",
            "GET",
            "/api/v1/work/{jobId}/events",
            True,
        ),
        ActionSpec(
            "pandrator_manager_status",
            "Inspect Pandrator Manager status",
            "SystemStatusInput",
            RiskClass.READ,
            "manager.read",
            "getManagerStatus",
            "GET",
            "/api/v1/manager/status",
            True,
        ),
        ActionSpec(
            "pandrator_manager_doctor",
            "Inspect Pandrator Manager diagnostics",
            "SystemStatusInput",
            RiskClass.READ,
            "manager.read",
            "getManagerDoctorReport",
            "GET",
            "/api/v1/manager/doctor",
            True,
        ),
        ActionSpec(
            "pandrator_create_session",
            "Create a Pandrator session",
            "CreateSessionInput",
            RiskClass.WRITE,
            "app.write",
            "createSession",
            "POST",
            "/api/v1/sessions",
            True,
            True,
        ),
        ActionSpec(
            "pandrator_update_session",
            "Update a Pandrator session",
            "UpdateSessionInput",
            RiskClass.WRITE,
            "app.write",
            "updateSession",
            "PATCH",
            "/api/v1/sessions/{sessionId}",
            True,
            True,
        ),
        ActionSpec(
            "pandrator_attach_existing_source",
            "Attach a reusable source asset",
            "AttachExistingSourceInput",
            RiskClass.WRITE,
            "app.write",
            "attachSessionSource",
            "POST",
            "/api/v1/sessions/{sessionId}/sources",
            True,
            True,
        ),
        ActionSpec(
            "pandrator_update_session_settings",
            "Update one session settings section",
            "UpdateSessionSettingsInput",
            RiskClass.WRITE,
            "app.write",
            "putSessionSettings",
            "PUT",
            "/api/v1/sessions/{sessionId}/settings/{section}",
            True,
            True,
        ),
        ActionSpec(
            "pandrator_plan_workflow",
            "Preview an exact workflow execution plan",
            "PlanWorkflowInput",
            RiskClass.READ,
            "app.read",
            "createWorkflowPlan",
            "POST",
            "/api/v1/sessions/{sessionId}/workflow-plans",
            True,
        ),
        ActionSpec(
            "pandrator_execute_workflow_plan",
            "Execute an exact reviewed workflow plan",
            "ExecuteWorkflowPlanInput",
            RiskClass.RUN,
            "app.run",
            "executeWorkflowPlan",
            "POST",
            "/api/v1/workflow-plans/{planId}/execute",
            True,
            True,
            True,
        ),
        ActionSpec(
            "pandrator_cancel_work",
            "Cancel durable work",
            "CancelWorkInput",
            RiskClass.CANCEL,
            "app.cancel",
            "cancelWork",
            "POST",
            "/api/v1/work/{jobId}/cancel",
            True,
            True,
        ),
        ActionSpec(
            "pandrator_plan_component_change",
            "Preview an exact Manager component plan",
            "PlanComponentChangeInput",
            RiskClass.READ,
            "manager.read",
            "createManagerPlan",
            "POST",
            "/api/v1/manager/plans",
            True,
            True,
        ),
        ActionSpec(
            "pandrator_control_runtime",
            "Control Pandrator or managed service runtime",
            "ControlRuntimeInput",
            RiskClass.MANAGER_RUNTIME,
            "manager.runtime",
            "controlManagerRuntime",
            "POST",
            "/api/v1/manager/runtime/{action}",
            True,
            True,
            True,
        ),
        ActionSpec(
            "pandrator_execute_component_plan",
            "Execute an exact reviewed Manager component plan",
            "ExecuteComponentPlanInput",
            RiskClass.MANAGER_MUTATE,
            "manager.mutate",
            "submitManagerOperation",
            "POST",
            "/api/v1/manager/operations",
            True,
            True,
            True,
        ),
    )
)
