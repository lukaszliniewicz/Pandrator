"""Portable client and actual MCP protocol contracts for pSSML tools."""

import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
import pytest

from pandrator_mcp.catalog import ACTION_CATALOG
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import build_runtime
from pandrator_mcp.performance_actions import PERFORMANCE_ACTIONS
from pandrator_mcp.schemas.performance import (
    PERFORMANCE_INPUT_MODELS,
    CreatePerformancePlanInput,
    EditPerformancePlanInput,
    SubmitPerformanceBatchInput,
)
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.performance import (
    performance_action,
    register_performance_tools,
)
from pandrator.web.openapi import build_openapi_document

try:
    from mcp import Client
except ImportError:
    Client = None


def test_manifest_matches_openapi_and_strict_input_models():
    document = build_openapi_document()
    models = {model.__name__: model for model in PERFORMANCE_INPUT_MODELS}
    for (
        _action,
        name,
        _title,
        model,
        _risk,
        scope,
        operation,
        method,
        _suffix,
    ) in PERFORMANCE_ACTIONS:
        spec = ACTION_CATALOG.get(name)
        assert spec.input_model in models
        api = document["paths"][spec.path][method.lower()]
        assert api["operationId"] == operation
        assert {"nativeOAuth": [scope]} in api["security"]
        assert models[model].model_json_schema()["additionalProperties"] is False


def test_client_routes_are_encoded_and_only_allowlisted_actions_exist():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"ok": True})
    client.performance_plan_request(
        "submit",
        {
            "session_id": "session/a",
            "plan_id": "plan/b",
            "batch_id": "batch/c",
            "lease_token": "a" * 48,
            "items": [],
            "idempotency_key": "performance-client-key",
        },
    )
    args, kwargs = client._request_json.call_args
    assert args[0].endswith(
        "session%2Fa/performance-plans/plan%2Fb/batches/batch%2Fc/submit"
    )
    assert kwargs["method"] == "POST"
    assert kwargs["body"] == {"lease_token": "a" * 48, "items": []}
    with pytest.raises(ValueError):
        client.performance_plan_request("delete_everything", {})
    with pytest.raises(ValueError):
        client.performance_plan_request("edit", {"session_id": "test"})


def test_tool_forwards_portable_data_and_supplies_next_action():
    application = Mock()
    application.performance_plan_request.return_value = {
        "id": "plan",
        "status": "draft",
    }
    runtime = SimpleNamespace(require_application=lambda: application)
    args = CreatePerformancePlanInput(
        session_id="session",
        expected_plan_revision_id="revision",
        idempotency_key="performance-create",
    )
    outcome = performance_action(runtime, "create", args)
    assert outcome.next_actions[0].tool == "pandrator_claim_performance_batch"
    assert application.performance_plan_request.call_args.args[1]["mode"] == "passive"


def test_edit_and_worker_input_bounds():
    with pytest.raises(ValidationError):
        SubmitPerformanceBatchInput(
            session_id="s",
            plan_id="p",
            batch_id="b",
            lease_token="short",
            idempotency_key="test-1234",
            items=[],
        )
    with pytest.raises(ValidationError):
        EditPerformancePlanInput(
            session_id="s",
            plan_id="p",
            expected_version=0,
            idempotency_key="test-1234",
            items=[],
        )
    with pytest.raises(ValidationError):
        CreatePerformancePlanInput(
            session_id="s",
            expected_plan_revision_id="r",
            idempotency_key="test-1234",
            context_before=99,
        )


def test_registered_signatures_are_flat_and_match_input_models():
    recorded = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def store(func):
                recorded[name] = func
                return func

            return store

    register_performance_tools(
        Server(), None, lambda *args: args, read_only="read", write_action="write"
    )
    models = {model.__name__: model for model in PERFORMANCE_INPUT_MODELS}
    for _action, name, _title, model, *_rest in PERFORMANCE_ACTIONS:
        signature = inspect.signature(recorded[name])
        assert set(signature.parameters) == set(models[model].model_fields)
        assert "kwargs" not in signature.parameters


@unittest.skipIf(
    Client is None, "This interpreter does not have the configured MCP SDK."
)
class PerformanceProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_submit_tools_over_real_in_memory_transport(self):
        from pandrator_mcp.server import build_server

        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="unconfigured",
                    configuration_path=Path(directory) / "missing-targets.json",
                )
            )
            application = Mock()
            application.performance_plan_request.return_value = {
                "id": "plan-1",
                "status": "draft",
            }
            with patch(
                "pandrator_mcp.context.McpRuntime.require_application",
                return_value=application,
            ):
                async with Client(
                    build_server(runtime), mode="auto", raise_exceptions=True
                ) as client:
                    tools = {
                        tool.name: tool for tool in (await client.list_tools()).tools
                    }
                    create = tools["pandrator_create_performance_plan"]
                    assert (
                        "expected_plan_revision_id" in create.input_schema["properties"]
                    )
                    assert "arguments" not in create.input_schema["properties"]
                    result = await client.call_tool(
                        "pandrator_create_performance_plan",
                        {
                            "session_id": "s",
                            "expected_plan_revision_id": "r",
                            "idempotency_key": "performance-sdk-test",
                        },
                    )
                    assert not result.is_error
                    assert (
                        application.performance_plan_request.call_args.args[0]
                        == "create"
                    )
                    result = await client.call_tool(
                        "pandrator_submit_performance_batch",
                        {
                            "session_id": "s",
                            "plan_id": "p",
                            "batch_id": "b",
                            "lease_token": "a" * 48,
                            "idempotency_key": "performance-sdk-submit",
                            "items": [
                                {
                                    "segment_id": "segment",
                                    "annotation": {"decision": "none"},
                                }
                            ],
                        },
                    )
                    assert not result.is_error
                    assert application.performance_plan_request.call_args.args[1][
                        "items"
                    ][0]["annotation"] == {"decision": "none"}
