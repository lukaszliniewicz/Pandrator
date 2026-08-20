"""Versioned domain and API models for Pandrator Manager."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION = "v1"
SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ComputeVariant(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"
    VULKAN = "vulkan"
    METAL = "metal"
    ROCM = "rocm"
    WGPU = "wgpu"


class ComponentSection(StrEnum):
    CORE = "core"
    TEXT_TO_SPEECH = "text_to_speech"
    SPEECH_TO_TEXT = "speech_to_text"
    SPEECH_TO_SPEECH = "speech_to_speech"
    TRAINING = "training"


class SizeProvenance(StrEnum):
    """How the catalogue obtained a displayed transfer/disk size."""

    PUBLISHED = "published"
    MEASURED = "measured"
    ESTIMATE = "estimate"
    UNKNOWN = "unknown"


class ComponentCapability(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    label: str = Field(min_length=1)
    available: bool = True
    description: str = ""


class ComponentModel(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    label: str = Field(min_length=1)
    description: str = ""
    license_name: str | None = None
    license_url: str | None = None
    usage_note: str = ""
    capabilities: tuple[str, ...] = ()
    estimated_download_bytes: int | None = Field(default=None, ge=0)
    size_provenance: SizeProvenance = SizeProvenance.ESTIMATE


class ComponentOptionChoice(StrictModel):
    value: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""
    requires: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class ComponentInstallOption(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    description: str = ""
    state_field: Literal["options", "quantization"] = "options"
    default: str = Field(min_length=1)
    choices: tuple[ComponentOptionChoice, ...]

    @model_validator(mode="after")
    def validate_default(self) -> "ComponentInstallOption":
        values = {choice.value for choice in self.choices}
        if not values:
            raise ValueError("Install options require at least one choice.")
        if self.default not in values:
            raise ValueError(
                f"Install option default {self.default!r} is not a declared choice."
            )
        return self


class ComponentState(StrEnum):
    ABSENT = "absent"
    PRESENT = "present"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class OperationKind(StrEnum):
    INSTALL = "install"
    UPDATE = "update"
    REPAIR = "repair"
    REMOVE = "remove"
    UNINSTALL = "uninstall"
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    IMPORT = "import"


class OperationState(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    CANCELLING = "cancelling"
    ROLLING_BACK = "rolling_back"
    HANDOFF_PENDING = "handoff_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RECOVERY_REQUIRED,
    }
)


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class HealthState(StrEnum):
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    FAILED = "failed"


class DesiredComponentState(StrictModel):
    present: bool = True
    compute: ComputeVariant = ComputeVariant.AUTO
    quantization: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ResolvedComponentState(StrictModel):
    compute: ComputeVariant
    quantization: str | None = None
    platform: str
    options: dict[str, Any] = Field(default_factory=dict)


class ComponentDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    label: str = Field(min_length=1)
    description: str = ""
    guidance: str = ""
    section: ComponentSection = ComponentSection.CORE
    display_order: int = Field(default=100, ge=0)
    languages: tuple[str, ...] = ()
    capabilities: tuple[ComponentCapability, ...] = ()
    models: tuple[ComponentModel, ...] = ()
    install_options: tuple[ComponentInstallOption, ...] = ()
    driver: str = "marker"
    supported_systems: tuple[str, ...] = ("Windows", "Linux")
    supported_architectures: tuple[str, ...] = ("AMD64", "x86_64", "arm64", "aarch64")
    compute_variants: tuple[ComputeVariant, ...] = (ComputeVariant.CPU,)
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    resource_locks: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    source_markers: tuple[str, ...] = ()
    supported_actions: tuple[str, ...] = (
        "install",
        "update",
        "repair",
        "remove",
        "start",
        "stop",
    )
    required_runtime_tools: tuple[Literal["pixi"], ...] = ()
    environment_owner: str | None = None
    service_key: str | None = None
    default_port: int | None = Field(default=None, ge=1, le=65535)
    repo_url: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    estimated_download_bytes: int | None = Field(default=None, ge=0)
    estimated_installed_bytes: int | None = Field(default=None, ge=0)
    size_provenance: SizeProvenance = SizeProvenance.UNKNOWN
    size_note: str = ""

    @field_validator("supported_systems", "supported_architectures")
    @classmethod
    def non_empty_support(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Supported platform lists cannot be empty.")
        return value

    @model_validator(mode="after")
    def unique_presentation_ids(self) -> "ComponentDefinition":
        for label, values in (
            ("capability", tuple(item.id for item in self.capabilities)),
            ("model", tuple(item.id for item in self.models)),
            ("install option", tuple(item.key for item in self.install_options)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Component {self.id} has duplicate {label} IDs.")
        return self


class ComponentInspection(StrictModel):
    component_id: str
    state: ComponentState
    desired: DesiredComponentState | None = None
    resolved: ResolvedComponentState | None = None
    installed_version: str | None = None
    installed_revision: str | None = None
    evidence: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    inspected_at: datetime = Field(default_factory=utc_now)


class ConfirmationRequirement(StrictModel):
    kind: Literal["license", "destructive", "restart", "elevation"]
    key: str
    message: str
    url: str | None = None


class PreflightCheck(StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    status: Literal["pass", "warning", "error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DoctorCheck(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]*$")
    category: Literal[
        "workspace",
        "database",
        "release",
        "component",
        "service",
        "ownership",
        "integration",
        "transaction",
        "network",
    ]
    status: Literal["pass", "warning", "error"]
    message: str
    repairable: bool = False
    repair_target: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DoctorReport(StrictModel):
    healthy: bool
    checks: tuple[DoctorCheck, ...]
    summary: dict[Literal["pass", "warning", "error"], int]
    generated_at: datetime = Field(default_factory=utc_now)


class TaskSpec(StrictModel):
    id: str
    kind: str
    label: str
    component_id: str | None = None
    dependencies: tuple[str, ...] = ()
    resource_locks: tuple[str, ...] = ()
    estimated_download_bytes: int = Field(default=0, ge=0)
    estimated_disk_bytes: int = Field(default=0, ge=0)
    cancellation_boundary: bool = True
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    verification: dict[str, Any] = Field(default_factory=dict)
    rollback: dict[str, Any] = Field(default_factory=dict)


class OperationPlan(StrictModel):
    id: str
    api_version: str = API_VERSION
    schema_version: int = SCHEMA_VERSION
    kind: OperationKind
    workspace: str
    expected_revision: int = Field(ge=0)
    desired: dict[str, DesiredComponentState]
    inspections: dict[str, ComponentInspection]
    tasks: tuple[TaskSpec, ...]
    preflight: tuple[PreflightCheck, ...] = ()
    confirmations: tuple[ConfirmationRequirement, ...] = ()
    warnings: tuple[str, ...] = ()
    impacts: dict[str, Any] = Field(default_factory=dict)
    estimated_download_bytes: int = Field(default=0, ge=0)
    estimated_disk_bytes: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    digest: str

    @model_validator(mode="after")
    def validate_task_graph(self) -> "OperationPlan":
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Operation plan contains duplicate task IDs.")
        known = set(task_ids)
        for task in self.tasks:
            unknown = set(task.dependencies).difference(known)
            if unknown:
                raise ValueError(
                    f"Task {task.id} depends on unknown task(s): {', '.join(sorted(unknown))}."
                )
            if task.id in task.dependencies:
                raise ValueError(f"Task {task.id} cannot depend on itself.")
        return self


class OperationRecord(StrictModel):
    id: str
    plan_id: str
    kind: OperationKind
    state: OperationState
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    current_task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    recovery: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class OperationTaskRecord(StrictModel):
    operation_id: str
    task: TaskSpec
    ordinal: int = Field(ge=0)
    state: TaskState = TaskState.PENDING
    attempt: int = Field(default=0, ge=0)
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ProcessIdentity(StrictModel):
    pid: int = Field(gt=0)
    create_time: float = Field(gt=0)
    executable: str = Field(min_length=1)
    manager_instance_id: str = Field(min_length=1)
    # Optional for compatibility with identities persisted before service
    # family ownership was recorded.
    ownership_token: str | None = Field(default=None, min_length=32)
    process_group_id: int | None = Field(default=None, gt=0)
    session_id: int | None = Field(default=None, gt=0)


class HealthResult(StrictModel):
    state: HealthState
    service_id: str
    protocol_version: str | None = None
    message: str = ""
    checked_at: datetime = Field(default_factory=utc_now)
    details: dict[str, Any] = Field(default_factory=dict)


class ManagedService(StrictModel):
    id: str
    component_id: str
    service_key: str
    desired_running: bool = False
    endpoint: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    capabilities: tuple[str, ...] = ()
    health: HealthResult | None = None
    process: ProcessIdentity | None = None
    restart_count: int = Field(default=0, ge=0)


class HealthProbeSpec(StrictModel):
    kind: Literal["none", "http", "tcp"] = "none"
    url: str | None = None
    host: str = "127.0.0.1"
    port: int | None = Field(default=None, ge=1, le=65535)
    expected_service: str | None = None
    expected_protocol: str | None = None
    expected_json: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @model_validator(mode="after")
    def validate_target(self) -> "HealthProbeSpec":
        if self.kind == "http" and not self.url:
            raise ValueError("HTTP health probe requires a URL.")
        if self.kind == "tcp" and self.port is None:
            raise ValueError("TCP health probe requires a port.")
        if self.kind == "none" and (
            self.expected_service
            or self.expected_protocol
            or self.expected_json
        ):
            raise ValueError("Identity expectations require a typed health probe.")
        return self


class RestartPolicy(StrictModel):
    maximum_restarts: int = Field(default=3, ge=0, le=100)
    base_backoff_seconds: float = Field(default=1.0, ge=0, le=300)
    maximum_backoff_seconds: float = Field(default=30.0, ge=0, le=3600)
    stable_after_seconds: float = Field(default=60.0, ge=0)
    health_failure_threshold: int = Field(default=3, ge=1, le=100)


class ManagedProcessSpec(StrictModel):
    service_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]+$")
    component_id: str
    label: str
    executable: str = Field(min_length=1)
    arguments: tuple[str, ...] = ()
    cwd: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    ports: tuple[int, ...] = ()
    dependencies: tuple[str, ...] = ()
    readiness: HealthProbeSpec = Field(default_factory=HealthProbeSpec)
    shutdown_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    startup_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    restart: RestartPolicy = Field(default_factory=RestartPolicy)
    required: bool = True

    @field_validator("ports")
    @classmethod
    def validate_ports(cls, ports: tuple[int, ...]) -> tuple[int, ...]:
        if any(port < 1 or port > 65535 for port in ports):
            raise ValueError("Managed process ports must be between 1 and 65535.")
        if len(ports) != len(set(ports)):
            raise ValueError("Managed process declares a duplicate port.")
        return ports

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.executable, *self.arguments)


class ConnectionDescriptor(StrictModel):
    api_version: str = API_VERSION
    manager_version: str
    workspace: str
    base_url: str
    public_url: str | None = None
    instance_id: str
    pid: int = Field(gt=0)
    process_create_time: float = Field(gt=0)
    executable: str = Field(min_length=1)
    started_at: datetime = Field(default_factory=utc_now)


class ManagerEvent(StrictModel):
    cursor: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    operation_id: str | None = None
    component_id: str | None = None
    service_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ManagerStatus(StrictModel):
    manager_version: str
    api_versions: tuple[str, ...] = (API_VERSION,)
    schema_version: int = SCHEMA_VERSION
    instance_id: str | None = None
    workspace: str
    configuration_revision: int = Field(ge=0)
    ready: bool
    capabilities: tuple[str, ...] = ()
    active_operation_id: str | None = None


class LegacySourceFile(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class LegacyOwnershipCandidate(StrictModel):
    path: str
    owner_kind: Literal["legacy_component", "legacy_shared"]
    owner_id: str
    evidence: tuple[str, ...] = ()


class LegacyImportReport(StrictModel):
    source: str
    source_digest: str
    valid: bool
    already_imported: bool = False
    desired: dict[str, DesiredComponentState] = Field(default_factory=dict)
    inspections: dict[str, ComponentInspection] = Field(default_factory=dict)
    positively_identified: tuple[str, ...] = ()
    unknown_paths: tuple[str, ...] = ()
    legacy_data: dict[str, Any] = Field(default_factory=dict)
    source_files: tuple[LegacySourceFile, ...] = ()
    ownership: tuple[LegacyOwnershipCandidate, ...] = ()
    warnings: tuple[str, ...] = ()
