"""Safe MCP handlers for inspecting and selecting exact workflow inputs."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas.workflow_inputs import (
    GetWorkflowInputsInput,
    SelectWorkflowInputInput,
)


def get_workflow_inputs(
    runtime: McpRuntime,
    arguments: GetWorkflowInputsInput,
) -> dict[str, Any]:
    """Return the compact, text-free input manifest for one session."""

    return runtime.require_application().get_workflow_inputs(arguments.session_id)


def select_workflow_input(
    runtime: McpRuntime,
    arguments: SelectWorkflowInputInput,
) -> dict[str, Any]:
    """Select an exact producer artifact with revision and idempotency guards."""

    return runtime.require_application().select_workflow_input(
        arguments.session_id,
        consumer=arguments.consumer,
        role=arguments.role,
        artifact_id=arguments.artifact_id,
        expected_outcome_revision=arguments.expected_outcome_revision,
        expected_selection_revision=arguments.expected_selection_revision,
        expected_translation_settings_revision=(arguments.expected_translation_settings_revision),
        idempotency_key=arguments.idempotency_key,
    )


__all__ = ["get_workflow_inputs", "select_workflow_input"]
