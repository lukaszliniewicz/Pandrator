"""Public MCP input schemas."""

from .common import ToolEnvelope, ToolInput, WorkReference
from .guidance import GUIDE_TOPICS, ExplainSystemInput, GuideTopic
from .inventory import ListArtifactsInput, ProviderStatusInput, VoiceCatalogInput
from .manager import (
    ControlRuntimeInput,
    ExecuteComponentPlanInput,
    ManagerDesiredComponentInput,
    PlanComponentChangeInput,
)
from .recommendations import RecommendNextStepsInput
from .sessions import (
    AttachExistingSourceInput,
    CreateSessionInput,
    GetSessionInput,
    GetSessionSettingsInput,
    GetWorkflowInput,
    ListSessionsInput,
    ListSourcesInput,
    UpdateSessionInput,
    UpdateSessionSettingsInput,
)
from .system import CapabilitiesInput, SystemStatusInput, TargetStatusInput
from .work import (
    CancelWorkInput,
    GetWorkInput,
    GetWorkLogInput,
    ListWorkInput,
)
from .workflow import (
    ExecuteWorkflowPlanInput,
    PlanWorkflowInput,
    RunWorkflowInput,
)

TOOL_INPUT_MODELS = (
    ExplainSystemInput,
    RecommendNextStepsInput,
    TargetStatusInput,
    SystemStatusInput,
    CapabilitiesInput,
    ListSessionsInput,
    GetSessionInput,
    GetWorkflowInput,
    GetSessionSettingsInput,
    ListSourcesInput,
    ListArtifactsInput,
    ProviderStatusInput,
    VoiceCatalogInput,
    ListWorkInput,
    GetWorkInput,
    GetWorkLogInput,
    CreateSessionInput,
    UpdateSessionInput,
    AttachExistingSourceInput,
    UpdateSessionSettingsInput,
    PlanWorkflowInput,
    ExecuteWorkflowPlanInput,
    CancelWorkInput,
    ManagerDesiredComponentInput,
    PlanComponentChangeInput,
    ExecuteComponentPlanInput,
    ControlRuntimeInput,
)

__all__ = [
    "AttachExistingSourceInput",
    "CancelWorkInput",
    "CapabilitiesInput",
    "CreateSessionInput",
    "ExplainSystemInput",
    "GUIDE_TOPICS",
    "GuideTopic",
    "ExecuteWorkflowPlanInput",
    "GetSessionInput",
    "GetSessionSettingsInput",
    "GetWorkInput",
    "GetWorkLogInput",
    "GetWorkflowInput",
    "ListArtifactsInput",
    "ListSessionsInput",
    "ListSourcesInput",
    "ListWorkInput",
    "ControlRuntimeInput",
    "ExecuteComponentPlanInput",
    "ManagerDesiredComponentInput",
    "PlanComponentChangeInput",
    "ProviderStatusInput",
    "PlanWorkflowInput",
    "RecommendNextStepsInput",
    "RunWorkflowInput",
    "SystemStatusInput",
    "TargetStatusInput",
    "TOOL_INPUT_MODELS",
    "ToolEnvelope",
    "ToolInput",
    "UpdateSessionInput",
    "UpdateSessionSettingsInput",
    "WorkReference",
    "VoiceCatalogInput",
]
