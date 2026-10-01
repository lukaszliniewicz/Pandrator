"""Native durable workflow lifecycle adapters."""

from typing import Any

from ..context import McpRuntime
from ..schemas.workflow_controls import GetDispatchPreviewInput, TerminateDispatchRunInput


def terminate_dispatch_run(runtime: McpRuntime, arguments: TerminateDispatchRunInput) -> dict[str, Any]:
    return runtime.require_application().terminate_dispatch_run(**arguments.model_dump())


def get_dispatch_preview(runtime: McpRuntime, arguments: GetDispatchPreviewInput) -> dict[str, Any]:
    return runtime.require_application().get_dispatch_preview(**arguments.model_dump())
