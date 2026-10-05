"""Resource registrations borrow the adapter's request and stdout guard."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from ..context import McpRuntime
from ..schemas import (
    CapabilitiesInput,
    GetWorkflowInput,
    GetWorkInput,
    SystemStatusInput,
    TargetStatusInput,
)
from ..tools import capabilities, get_work, get_workflow, system_status, target_status


def register_resources(
    server: Any,
    runtime: McpRuntime,
    _resource_call: Callable[..., str],
) -> None:
    """Register resources at their existing inventory position."""

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
            lambda: target_status(
                runtime,
                TargetStatusInput(include_authenticated_identity=False),
            ),
        )

    @server.resource("pandrator://live/status")
    def live_status_resource() -> str:
        """Inspect current application and Manager status."""

        return _resource_call(lambda: system_status(runtime, SystemStatusInput()))

    @server.resource("pandrator://live/capabilities")
    def live_capabilities_resource() -> str:
        """Inspect current capabilities."""

        return _resource_call(lambda: capabilities(runtime, CapabilitiesInput()))

    @server.resource("pandrator://sessions/{session_id}/workflow")
    def workflow_resource(session_id: str) -> str:
        """Inspect one live workflow snapshot."""

        return _resource_call(
            lambda: get_workflow(
                runtime,
                GetWorkflowInput(session_id=session_id),
            ),
        )

    @server.resource("pandrator://work/{work_type}/{work_id}")
    def work_resource(
        work_type: Literal["job", "manager_operation"],
        work_id: str,
    ) -> str:
        """Inspect one application or Manager work item."""

        return _resource_call(
            lambda: get_work(
                runtime,
                GetWorkInput(
                    work_type=work_type,
                    work_id=work_id,
                    include_events=False,
                ),
            ),
        )
