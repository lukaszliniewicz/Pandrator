"""Pure MCP handler functions, separated from protocol registration."""

from .dispatch import (
    claim_dispatch_batch,
    create_dispatch_run,
    get_dispatch_run,
    list_dispatch_runs,
    release_dispatch_batch,
    renew_dispatch_batch,
    submit_dispatch_batch,
)
from .guidance import explain_system
from .inventory import list_artifacts, provider_status, voice_catalog
from .manager import (
    control_runtime,
    execute_component_plan,
    manager_doctor,
    manager_status,
    plan_component_change,
)
from .recommendations import recommend_next_steps
from .sessions import (
    attach_existing_source,
    create_session,
    get_session,
    get_session_settings,
    get_workflow,
    list_sessions,
    list_sources,
    update_session,
    update_session_settings,
)
from .source_cleaning_dispatch import (
    claim_source_cleaning_dispatch_batch,
    create_source_cleaning_dispatch_run,
    get_source_cleaning_dispatch_run,
    inspect_source_cleaning_dispatch_extraction,
    list_source_cleaning_dispatch_runs,
    release_source_cleaning_dispatch_batch,
    renew_source_cleaning_dispatch_batch,
    submit_source_cleaning_dispatch_batch,
)
from .system import capabilities, system_status, target_status
from .work import cancel_work, get_work, get_work_log, list_work
from .workflow import execute_workflow_plan, plan_workflow

__all__ = [
    "capabilities",
    "claim_dispatch_batch",
    "claim_source_cleaning_dispatch_batch",
    "cancel_work",
    "attach_existing_source",
    "control_runtime",
    "create_session",
    "create_dispatch_run",
    "create_source_cleaning_dispatch_run",
    "explain_system",
    "execute_workflow_plan",
    "execute_component_plan",
    "get_session",
    "get_dispatch_run",
    "get_source_cleaning_dispatch_run",
    "inspect_source_cleaning_dispatch_extraction",
    "get_session_settings",
    "get_work",
    "get_work_log",
    "get_workflow",
    "list_artifacts",
    "list_dispatch_runs",
    "list_source_cleaning_dispatch_runs",
    "list_sessions",
    "list_sources",
    "list_work",
    "manager_doctor",
    "manager_status",
    "plan_workflow",
    "plan_component_change",
    "provider_status",
    "recommend_next_steps",
    "release_dispatch_batch",
    "release_source_cleaning_dispatch_batch",
    "renew_dispatch_batch",
    "renew_source_cleaning_dispatch_batch",
    "system_status",
    "submit_dispatch_batch",
    "submit_source_cleaning_dispatch_batch",
    "target_status",
    "update_session",
    "update_session_settings",
    "voice_catalog",
]
