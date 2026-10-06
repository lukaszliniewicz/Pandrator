"""Portable client and actual MCP protocol contracts for pSSML tools."""

import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.catalog import ACTION_CATALOG
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import build_runtime
from pandrator_mcp.performance_actions import PERFORMANCE_ACTIONS
from pandrator_mcp.schemas.performance import (
    PERFORMANCE_INPUT_MODELS,
    ClaimPerformanceBatchInput,
    CreatePerformancePlanInput,
    EditPerformancePlanInput,
    GetPerformancePlanInput,
    SubmitPerformanceBatchInput,
)
from pandrator_mcp.server import _response
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.performance import (
    performance_action,
    register_performance_tools,
)

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
    assert application.performance_plan_request.call_args.args[1]["purpose"] == "delivery"


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
    with pytest.raises(ValidationError):
        CreatePerformancePlanInput(
            session_id="s",
            expected_plan_revision_id="r",
            idempotency_key="performance-create",
            purpose="speakers",
        )
    speaker_plan = CreatePerformancePlanInput(
        session_id="s",
        expected_plan_revision_id="r",
        idempotency_key="performance-create",
        purpose="combined",
        annotation_format="xml",
    )
    assert speaker_plan.purpose == "combined"

    submit = SubmitPerformanceBatchInput(
        session_id="s",
        plan_id="p",
        batch_id="b",
        lease_token="a" * 48,
        idempotency_key="performance-submit",
        items=[{"segment_id": "segment", "annotation": {"decision": "none"}}],
    )
    assert submit.character_proposals == []
    assert len(
        SubmitPerformanceBatchInput(
            session_id="s",
            plan_id="p",
            batch_id="b",
            lease_token="a" * 48,
            idempotency_key="performance-submit",
            items=[{"segment_id": "segment", "annotation": {"decision": "none"}}],
            character_proposals=[{}] * 100,
        ).character_proposals
    ) == 100
    with pytest.raises(ValidationError):
        SubmitPerformanceBatchInput(
            session_id="s",
            plan_id="p",
            batch_id="b",
            lease_token="a" * 48,
            idempotency_key="performance-submit",
            items=[{"segment_id": "segment", "annotation": {"decision": "none"}}],
            character_proposals=[{}] * 101,
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
        Server(), None, lambda *args: args, _response, read_only="read", write_action="write"
    )
    models = {model.__name__: model for model in PERFORMANCE_INPUT_MODELS}
    for action, name, _title, model, *_rest in PERFORMANCE_ACTIONS:
        signature = inspect.signature(recorded[name])
        assert set(signature.parameters) == set(models[model].model_fields) | (
            {"response_mode"} if action in {"claim", "create", "get"} else set()
        )
        assert "kwargs" not in signature.parameters


def test_performance_registration_pops_transport_mode_before_strict_validation():
    recorded = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def store(func):
                recorded[name] = func
                return func
            return store

    def validate(handler, runtime, model, values):
        validated = model.model_validate(values)
        return {"schema_version": "1", "request_id": "fixed", "result": validated.model_dump()}

    response = Mock(side_effect=lambda envelope, mode: (envelope, mode))
    register_performance_tools(Server(), None, validate, response, read_only="read", write_action="write")
    values_by_action = {
        "create": {"session_id": "session", "expected_plan_revision_id": "revision",
                   "idempotency_key": "performance-create"},
        "get": {"session_id": "session", "plan_id": "plan"},
        "claim": {"session_id": "session", "plan_id": "plan", "idempotency_key": "performance-claim"},
    }
    for action, values in values_by_action.items():
        name = "pandrator_claim_performance_batch" if action == "claim" else f"pandrator_{action}_performance_plan"
        envelope, mode = recorded[name](**values, response_mode="structured")
        assert mode == "structured"
        assert "response_mode" not in envelope["result"]
    with pytest.raises(ValidationError):
        recorded["pandrator_list_performance_plans"](session_id="session", response_mode="structured")


def _performance_claim_payload():
    return {
        "session_id": "session", "plan_id": "plan", "version": 3,
        "batch_id": "batch", "lease_token": "a" * 48,
        "lease_expires_at": "2030-01-01T00:00:00Z", "complete": False,
        "batch": {
            "kind": "performance", "purpose": "combined", "annotation_format": "xml",
            "instructions": "Keep immutable text.", "policy": {"allow_vocalizations": False},
            "capability_snapshot": {"services": ["xtts"]},
            "annotation_schema": {"type": "object", "properties": {"decision": {"enum": ["none"]}}},
            "speech_xml_schema": {"root": "speech", "attributes": {"speaker": "character id"}},
            "character_dictionary_revision": 4,
            "character_dictionary": [{"id": "alice", "display_name": "Alice", "voice_category": "female"}],
            "items": [{
                "segment_id": "segment-1", "text": "Hello.", "speaker": "Alice",
                "timing": {"start_ms": 0, "end_ms": 1000},
                "context": {"before": [{"text": "Before"}], "after": [{"text": "After"}]},
                "annotation": {"decision": "none"}, "locked": False,
            }],
        },
    }


def test_compact_performance_claim_preserves_all_semantics_and_cache_invalidation():
    payload = _performance_claim_payload()
    before = copy.deepcopy(payload)
    application = Mock()
    application.performance_plan_request.return_value = payload
    runtime = SimpleNamespace(require_application=lambda: application)

    def claim(**values):
        return performance_action(runtime, "claim", ClaimPerformanceBatchInput(
            session_id="session", plan_id="plan", idempotency_key="performance-claim", **values
        )).result

    standard = claim(packet_format="standard")
    compact = claim()
    assert compact["packet_format"] == "compact-v1"
    assert compact["batch"] == {"items": standard["batch"]["items"]}
    assert compact["manifest"]["annotation_schema"] == payload["batch"]["annotation_schema"]
    assert compact["manifest"]["speech_xml_schema"] == payload["batch"]["speech_xml_schema"]
    restored = {k: v for k, v in compact.items()
                if k not in {"manifest", "manifest_hash", "packet_format"}}
    restored["batch"] = {**restored["batch"], **compact["manifest"]}
    assert restored == standard
    assert payload == before
    assert claim(known_manifest_hash=compact["manifest_hash"]) == {
        k: v for k, v in compact.items() if k != "manifest"
    }
    assert claim(known_manifest_hash="0" * 64) == compact
    for key, replacement in (
        ("character_dictionary_revision", 5),
        ("character_dictionary", []),
        ("capability_snapshot", {"services": ["other"]}),
        ("policy", {"allow_vocalizations": True}),
    ):
        payload["batch"][key] = replacement
        changed = claim(known_manifest_hash=compact["manifest_hash"])
        assert changed["manifest_hash"] != compact["manifest_hash"]
        assert changed["manifest"][key] == replacement
        payload["batch"][key] = copy.deepcopy(before["batch"][key])
    payload["batch"]["character_dictionary"][0]["display_name"] = "Alicia"
    assert claim()["manifest_hash"] != compact["manifest_hash"]
    for call in application.performance_plan_request.call_args_list:
        assert set(call.args[1]) == {"session_id", "plan_id", "idempotency_key", "lease_seconds"}
    for invalid in ({"packet_format": "compact-v1"}, {"known_manifest_hash": "bad"}):
        with pytest.raises(ValidationError):
            ClaimPerformanceBatchInput(
                session_id="session", plan_id="plan", idempotency_key="performance-bad", **invalid
            )


@pytest.mark.parametrize("action", ["create", "get"])
def test_performance_plan_metadata_default_and_full_units_on_demand(action):
    payload = {
        "id": "plan", "version": 3, "status": "draft", "item_count": 1,
        "settings": {"batch_size": 12}, "batches": [{"id": "batch", "status": "ready"}],
        "items": [{"segment_id": "segment-1", "text": "Hello."}],
        "offset": 0, "limit": 25, "filter": "all",
    }
    before = copy.deepcopy(payload)
    application = Mock()
    application.performance_plan_request.return_value = payload
    runtime = SimpleNamespace(require_application=lambda: application)
    model = CreatePerformancePlanInput if action == "create" else GetPerformancePlanInput
    values = {"session_id": "session"}
    values.update({"expected_plan_revision_id": "revision", "idempotency_key": "performance-create"}
                  if action == "create" else {"plan_id": "plan"})
    metadata = performance_action(runtime, action, model(**values)).result
    full = performance_action(runtime, action, model(**values, include_units=True)).result
    assert metadata == {"schema_version": "1", **{
        k: v for k, v in payload.items() if k not in {"items", "offset", "limit", "filter"}
    }}
    assert full == {"schema_version": "1", **payload}
    assert payload == before
    assert all("include_units" not in call.args[1]
               for call in application.performance_plan_request.call_args_list)


def test_performance_submit_next_claim_then_complete_review_and_terminal_shape():
    application = Mock()
    runtime = SimpleNamespace(require_application=lambda: application)
    application.performance_plan_request.return_value = {"plan_id": "plan", "accepted": True}
    submission = SubmitPerformanceBatchInput(
        session_id="session", plan_id="plan", batch_id="batch", lease_token="a" * 48,
        idempotency_key="performance-submit",
        items=[{"segment_id": "segment-1", "annotation": {"decision": "none"}}],
    )
    submitted = performance_action(runtime, "submit", submission)
    assert submitted.next_actions[0].tool == "pandrator_claim_performance_batch"
    next_arguments = submitted.next_actions[0].arguments
    assert next_arguments["session_id"] == "session" and next_arguments["plan_id"] == "plan"
    ClaimPerformanceBatchInput.model_validate(next_arguments)
    assert submitted.next_actions == performance_action(runtime, "submit", submission).next_actions
    assert next_arguments["idempotency_key"] != performance_action(
        runtime, "submit", submission.model_copy(update={"idempotency_key": "performance-other"})
    ).next_actions[0].arguments["idempotency_key"]
    application.performance_plan_request.return_value = _performance_claim_payload()
    claimed = performance_action(runtime, "claim", ClaimPerformanceBatchInput(
        session_id="session", plan_id="plan", idempotency_key="performance-next"
    ))
    assert claimed.result["batch"]["items"]
    assert claimed.next_actions == []
    terminal = {"plan_id": "plan", "batch": None, "complete": True}
    application.performance_plan_request.return_value = terminal
    outcomes = [performance_action(runtime, "claim", ClaimPerformanceBatchInput(
        session_id="session", plan_id="plan", idempotency_key="performance-complete",
        packet_format=mode,
    )) for mode in ("standard", "compact")]
    assert outcomes[0].result == outcomes[1].result == {"schema_version": "1", **terminal}
    assert len(outcomes[1].next_actions) == 1
    assert outcomes[1].next_actions[0].tool == "pandrator_get_performance_plan"
    assert outcomes[1].next_actions[0].arguments["include_units"] is True
    application.performance_plan_request.return_value = {"id": "plan", "status": "draft"}
    reviewed = performance_action(runtime, "get", GetPerformancePlanInput(
        session_id="session", plan_id="plan"
    ))
    assert reviewed.next_actions == []


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
                    purpose_schema = create.input_schema["properties"]["purpose"]
                    assert purpose_schema["enum"] == [
                        "delivery",
                        "speakers",
                        "combined",
                    ]
                    assert purpose_schema["default"] == "delivery"
                    assert "arguments" not in create.input_schema["properties"]
                    for name in ("pandrator_create_performance_plan", "pandrator_get_performance_plan",
                                 "pandrator_claim_performance_batch"):
                        response_schema = tools[name].input_schema["properties"]["response_mode"]
                        assert response_schema["enum"] == ["standard", "structured"]
                        assert response_schema["default"] == "standard"
                    assert create.input_schema["properties"]["include_units"]["default"] is False
                    claim_schema = tools["pandrator_claim_performance_batch"].input_schema["properties"]
                    assert claim_schema["packet_format"]["default"] == "compact"
                    assert claim_schema["packet_format"]["enum"] == ["standard", "compact"]
                    assert claim_schema["known_manifest_hash"]["anyOf"] == [
                        {"type": "string", "pattern": "^[a-f0-9]{64}$"}, {"type": "null"},
                    ]
                    assert claim_schema["known_manifest_hash"]["default"] is None

                    proposals_schema = tools[
                        "pandrator_submit_performance_batch"
                    ].input_schema["properties"]["character_proposals"]
                    assert proposals_schema["maxItems"] == 100
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
                    assert application.performance_plan_request.call_args.args[1][
                        "character_proposals"
                    ] == []

                    application.performance_plan_request.return_value = _performance_claim_payload()
                    claims = []
                    for mode in ("standard", "structured"):
                        result = await client.call_tool("pandrator_claim_performance_batch", {
                            "session_id": "session", "plan_id": "plan",
                            "idempotency_key": "performance-sdk-claim", "response_mode": mode,
                        })
                        assert not result.is_error
                        envelope = result.structured_content
                        assert envelope["result"]["packet_format"] == "compact-v1"
                        text = json.loads(result.content[0].text)
                        if mode == "standard":
                            assert text == envelope
                        else:
                            assert text["data"] == "structuredContent"
                            assert "batch" not in text
                        claims.append({k: v for k, v in envelope.items() if k != "request_id"})
                        assert "response_mode" not in application.performance_plan_request.call_args.args[1]
                    assert claims[0] == claims[1]
