"""Native contracts for compact discovery and export-only planning."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.context import build_runtime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas import (
    PlanOrchestratedWorkflowInput,
    PlanWorkflowInput,
    RegenerateSegmentsInput,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.generation import regenerate_segments
from pandrator_mcp.tools.workflow import plan_orchestrated_workflow, plan_workflow
from tests.test_mcp_orchestration import _Application


def test_selected_run_export_does_not_request_upstream_generation():
    runtime = SimpleNamespace(require_application=lambda: _Application())
    outcome = plan_orchestrated_workflow(
        runtime,
        PlanOrchestratedWorkflowInput(
            session_id="s",
            goal="Export the selected dubbing",
            generation_run_id="run-1",
            audio_version_side="before",
            subtitle_mode="soft",
        ),
    )
    action = outcome.next_actions[0]
    assert action.arguments["continuation"] is False
    assert action.arguments["overrides"]["output"]["generation_run_id"] == "run-1"
    assert action.arguments["overrides"]["output"]["audio_version_side"] == "before"
    assert outcome.result["phases"][0]["monitor_arguments"]["include_events"] is False
    for extra in ({"passive_stages": ("translation",)}, {"final_stage": "generate_audio"}):
        with pytest.raises(ValidationError):
            PlanOrchestratedWorkflowInput(
                session_id="s", goal="Export", generation_run_id="run-1", **extra
            )


def test_workflow_direct_mode_and_canonical_execution_action():
    app = Mock()
    app.get_workflow.return_value = {"stages": []}
    app.create_workflow_plan.return_value = {
        "plan_id": "p",
        "plan_digest": "digest",
        "required_confirmations": ["provider"],
    }
    runtime = SimpleNamespace(require_application=lambda: app)
    outcome = plan_workflow(
        runtime, PlanWorkflowInput(session_id="s", target_stage="export", continuation=False)
    )
    assert app.create_workflow_plan.call_args.kwargs["continuation"] is False
    assert outcome.next_actions[0].arguments == {
        "plan_id": "p",
        "plan_digest": "digest",
        "accepted_confirmations": ["provider"],
        "idempotency_key": "workflow-plan:p",
    }


def test_regeneration_timeout_keeps_operation_and_exact_retry_inputs():
    error = PandratorMcpError(
        "application_response_timeout", "Timed out", details={"job_id": "admission-1"}
    )
    app = Mock()
    app.start_generation_run.side_effect = error
    arguments = RegenerateSegmentsInput(
        session_id="s", segment_ids=["cue-2"], idempotency_key="regeneration:retry:1"
    )
    with pytest.raises(PandratorMcpError):
        regenerate_segments(SimpleNamespace(require_application=lambda: app), arguments)
    assert error.next_actions[0].tool == "pandrator_get_generation_operation"
    assert error.next_actions[0].arguments == {"operation_id": "admission-1"}
    assert error.next_actions[1].arguments == arguments.model_dump(mode="json", exclude_unset=True)


def test_native_plan_metadata_and_structured_payload(tmp_path):
    asyncio.run(_native_plan_metadata_and_structured_payload(tmp_path))


async def _native_plan_metadata_and_structured_payload(tmp_path):
    runtime = build_runtime(
        McpSettings(target_name="missing", configuration_path=tmp_path / "missing.json")
    )
    server = build_server(runtime)
    data = {"plan_id": "p", "plan_digest": "d", "steps": [{"settings": "large-settings " * 1000}]}
    async with Client(server, mode="auto", raise_exceptions=True) as client:
        for name, handler in (
            ("pandrator_plan_workflow", "plan_workflow"),
            ("pandrator_plan_export_variant", "plan_export_variant"),
        ):
            with patch("pandrator_mcp.server." + handler, return_value=data):
                full = await client.call_tool(name, {"session_id": "s"})
                compact = await client.call_tool(
                    name, {"session_id": "s", "response_mode": "structured"}
                )
            assert compact.structured_content["result"] == full.structured_content["result"] == data
            assert len(compact.content[0].text) < len(full.content[0].text) / 10
            tool = server._tool_manager.get_tool(name)
            assert tool.annotations.read_only_hint is False
            assert ACTION_CATALOG.get(name).risk == RiskClass.WRITE
        for name in (
            "pandrator_create_dispatch_run",
            "pandrator_create_speech_optimization_dispatch_run",
            "pandrator_plan_orchestrated_workflow",
        ):
            schema = server._tool_manager.get_tool(name).parameters
            assert schema["type"] == "object" and "allOf" not in schema
            assert schema["x-pandrator-execution-policy"]["enforced"] == "runtime"
        fields = server._tool_manager.get_tool("pandrator_get_voice_catalog").parameters[
            "properties"
        ]
        assert fields["view"]["enum"] == ["full", "compact"]
        assert "fields" in fields
