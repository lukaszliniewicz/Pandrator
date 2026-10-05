"""Native speech submissions preserve the context delta supported by the application."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from tests.test_mcp_media_edit_registration import SENTINEL, envelope, fixture_runtime

TOOL = "pandrator_submit_speech_optimization_dispatch_batch"
EMPTY = {"terminology": {}, "entities": {}, "style_rules": [], "decisions": [], "notes": []}
BASE = {
    "batch_id": "batch-1",
    "lease_token": "lease-capability",
    "idempotency_key": "speech:submit-1",
    "result": {"kind": "speech_optimization", "items": [{"unit_id": 1, "text": "Doctor Jones"}]},
    "character_proposals": [{"character_key": "alice", "name": "Alice"}],
}


def speech_runtime(root: Path):
    runtime, calls, application = fixture_runtime(root)

    def submit(*args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs, "headers": correlation_headers()})
        print(SENTINEL)
        return {"run_id": "run-1", "batch_id": "batch-1", "status": "running", "accepted": True}

    application.submit_speech_optimization_dispatch_batch.side_effect = submit
    return runtime, calls, application


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("delta", ["omitted", "null", "empty", "nonempty"])
def test_native_speech_context_forwarding(tmp_path: Path, capsys, mode: str, delta: str) -> None:
    runtime, calls, application = speech_runtime(tmp_path)
    arguments: dict[str, Any] = copy.deepcopy(BASE)
    expected = copy.deepcopy(EMPTY)
    if delta == "null":
        arguments["context_delta"] = None
    elif delta == "empty":
        arguments["context_delta"] = {}
    elif delta == "nonempty":
        arguments["context_delta"] = {"entities": {"Alice": "narrator"}, "notes": ["Keep names."]}
        expected.update(arguments["context_delta"])
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(build_server(runtime), mode=mode, raise_exceptions=False) as client:
            return await client.call_tool(TOOL, arguments)

    value = envelope(asyncio.run(call()))
    assert value["result"] == {
        "schema_version": "1",
        "run_id": "run-1",
        "batch_id": "batch-1",
        "status": "running",
        "accepted": True,
    }
    assert value["next_actions"][0]["tool"] == "pandrator_claim_speech_optimization_dispatch_batch"
    assert len(calls) == len(application.mock_calls) == 1
    assert calls[0]["args"] == ["batch-1"]
    assert calls[0]["kwargs"] == {
        "lease_token": "lease-capability",
        "idempotency_key": "speech:submit-1",
        "result": {
            "kind": "speech_optimization",
            "items": [{"unit_id": 1, "text": "Doctor Jones", "speech_xml": None}],
        },
        "context_delta": expected,
        "character_proposals": BASE["character_proposals"],
    }
    assert calls[0]["headers"]["X-Request-ID"] == value["request_id"]
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("delta", [{"entities": {"Alice": ""}}, {"unexpected": "field"}])
def test_native_invalid_context_does_not_submit(tmp_path: Path, mode: str, delta) -> None:
    runtime, calls, application = speech_runtime(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(build_server(runtime), mode=mode, raise_exceptions=False) as client:
            return await client.call_tool(TOOL, {**BASE, "context_delta": delta})

    result = asyncio.run(call())
    assert result.is_error
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert any(
        isinstance(item, TextContent) and "context_delta" in item.text for item in result.content
    )


def test_native_context_schema_is_optional_and_strict(tmp_path: Path) -> None:
    runtime, calls, application = speech_runtime(tmp_path)

    async def discover():
        async with Client(build_server(runtime)) as client:
            return await client.list_tools()

    tools = asyncio.run(discover()).tools
    assert len(tools) == 162
    schema = next(t.input_schema for t in tools if t.name == TOOL)
    assert schema["additionalProperties"] is False
    assert "context_delta" not in schema["required"]
    field = schema["properties"]["context_delta"]
    assert field["default"] is None
    choices = field["anyOf"]
    assert any(choice == {"type": "null"} for choice in choices)
    ref = next(choice["$ref"] for choice in choices if "$ref" in choice)
    definition = schema["$defs"][ref.rsplit("/", 1)[-1]]
    assert definition["additionalProperties"] is False
    assert set(definition["properties"]) == set(EMPTY)
    assert calls == application.mock_calls == []
