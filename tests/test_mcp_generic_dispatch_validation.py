"""Generic-dispatch domain validation runs inside request and stdout guards."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import model_validator

import pandrator_mcp.registrations.dispatch as dispatch_owner
import pandrator_mcp.server as adapter
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.schemas import GetDispatchRunInput
from tests.test_mcp_media_edit_registration import fixture_runtime

MARKER = "private-generic-dispatch-fixture-" * 8
KEY = "guard:generic:1"
LEASE = "fixture-lease"
SENTINEL = "generic dispatch validation stdout sentinel"

CASES = [
    ("pandrator_get_dispatch_preview", {"run_id": MARKER}, "run_id"),
    (
        "pandrator_terminate_dispatch_run",
        {
            "run_id": MARKER,
            "expected_status": "ready",
            "action": "cancelled",
            "reason": "Fixture",
            "idempotency_key": KEY,
        },
        "run_id",
    ),
    (
        "pandrator_create_dispatch_run",
        {"session_id": MARKER, "kind": "correction", "idempotency_key": KEY},
        "session_id",
    ),
    ("pandrator_list_dispatch_runs", {"session_id": MARKER}, "session_id"),
    ("pandrator_get_dispatch_run", {"run_id": MARKER}, "run_id"),
    (
        "pandrator_inspect_dispatch_split_boundaries",
        {"batch_id": MARKER, "lease_token": LEASE, "cue_id": 1},
        "batch_id",
    ),
    (
        "pandrator_claim_dispatch_batch",
        {"run_id": MARKER, "idempotency_key": KEY},
        "run_id",
    ),
    (
        "pandrator_renew_dispatch_batch",
        {"batch_id": MARKER, "lease_token": LEASE, "idempotency_key": KEY},
        "batch_id",
    ),
    (
        "pandrator_release_dispatch_batch",
        {"batch_id": MARKER, "lease_token": LEASE, "idempotency_key": KEY},
        "batch_id",
    ),
    (
        "pandrator_submit_dispatch_batch",
        {
            "batch_id": MARKER,
            "lease_token": LEASE,
            "idempotency_key": KEY,
            "response_text": "Fixture",
        },
        "batch_id",
    ),
]


async def invoke(runtime: McpRuntime, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(adapter.build_server(runtime), raise_exceptions=False) as client:
        return await client.call_tool(tool, arguments)


def failure_value(result: Any, tool: str, field: str | None = None) -> dict[str, Any]:
    assert result.is_error and len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    prefix = f"Error executing tool {tool}: "
    assert block.text.startswith(prefix)
    failure = json.loads(block.text[len(prefix) :])
    assert failure["code"] == "validation_error"
    assert isinstance(failure["request_id"], str) and failure["request_id"]
    assert failure["retryable"] is False and failure["next_actions"] == []
    errors = failure["details"]["errors"]
    assert errors
    if field is not None:
        assert any(error["loc"] == [field] for error in errors)
    assert all(key not in error for error in errors for key in ("input", "ctx", "url"))
    return failure


@pytest.mark.parametrize("tool,arguments,field", CASES, ids=[case[0] for case in CASES])
def test_generic_dispatch_domain_validation_is_guarded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    arguments: dict[str, Any],
    field: str,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, tool, arguments))
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    failure = failure_value(result, tool, field)
    assert MARKER not in json.dumps(failure)


def test_generic_get_validator_stdout_and_correlation_are_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[dict[str, str]] = []

    class PrintingInput(GetDispatchRunInput):
        @model_validator(mode="before")
        @classmethod
        def probe(cls, value: Any) -> Any:
            print(SENTINEL)
            observed.append(correlation_headers())
            raise ValueError("Fixture guarded generic validation")

    monkeypatch.setattr(dispatch_owner, "GetDispatchRunInput", PrintingInput)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    tool = "pandrator_get_dispatch_run"
    result = asyncio.run(invoke(runtime, tool, {"run_id": "run-1"}))
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
    failure = failure_value(result, tool)
    assert len(observed) == 1
    assert observed[0]["X-Request-ID"] == failure["request_id"]
