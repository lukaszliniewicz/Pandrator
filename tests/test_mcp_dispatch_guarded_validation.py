"""Native dispatch domain validation stays inside request and stdout guards."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import model_validator

import pandrator_mcp.server as adapter
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.schemas import GetSourceCleaningDispatchRunInput
from tests.test_mcp_media_edit_registration import fixture_runtime

MARKER = "private-dispatch-fixture-" * 8
KEY = "guard:dispatch:1"
LEASE = "fixture-lease"
SENTINEL = "dispatch guard validation stdout sentinel"
SOURCE_RESULT = {"kind": "source_cleaning", "phase": "metadata"}
SPEECH_RESULT = {
    "kind": "speech_optimization",
    "items": [{"unit_id": 1, "text": "Fixture"}],
}


@dataclass(frozen=True)
class Case:
    tool: str
    arguments: dict[str, Any]
    field: str
    private_value: str = MARKER


CASES = [
    Case(
        "pandrator_create_source_cleaning_dispatch_run",
        {"session_id": MARKER, "idempotency_key": KEY},
        "session_id",
    ),
    Case("pandrator_list_source_cleaning_dispatch_runs", {"session_id": MARKER}, "session_id"),
    Case("pandrator_get_source_cleaning_dispatch_run", {"run_id": MARKER}, "run_id"),
    Case(
        "pandrator_claim_source_cleaning_dispatch_batch",
        {"run_id": MARKER, "idempotency_key": KEY},
        "run_id",
    ),
    Case(
        "pandrator_renew_source_cleaning_dispatch_batch",
        {"batch_id": MARKER, "lease_token": LEASE, "idempotency_key": KEY},
        "batch_id",
    ),
    Case(
        "pandrator_release_source_cleaning_dispatch_batch",
        {"batch_id": MARKER, "lease_token": LEASE, "idempotency_key": KEY},
        "batch_id",
    ),
    Case(
        "pandrator_inspect_source_cleaning_dispatch_extraction",
        {
            "batch_id": MARKER,
            "lease_token": LEASE,
            "action": "batch",
            "arguments": {},
            "idempotency_key": KEY,
        },
        "batch_id",
    ),
    Case(
        "pandrator_submit_source_cleaning_dispatch_batch",
        {
            "batch_id": MARKER,
            "lease_token": LEASE,
            "result": SOURCE_RESULT,
            "idempotency_key": KEY,
        },
        "batch_id",
    ),
    Case("pandrator_list_speech_optimization_dispatch_runs", {"session_id": MARKER}, "session_id"),
    Case("pandrator_get_speech_optimization_dispatch_run", {"run_id": MARKER}, "run_id"),
    Case(
        "pandrator_claim_speech_optimization_dispatch_batch",
        {"run_id": MARKER, "idempotency_key": KEY},
        "run_id",
    ),
    Case(
        "pandrator_renew_speech_optimization_dispatch_batch",
        {"batch_id": MARKER, "lease_token": LEASE, "idempotency_key": KEY},
        "batch_id",
    ),
    Case(
        "pandrator_release_speech_optimization_dispatch_batch",
        {"batch_id": MARKER, "lease_token": LEASE, "idempotency_key": KEY},
        "batch_id",
    ),
    Case(
        "pandrator_submit_speech_optimization_dispatch_batch",
        {
            "batch_id": MARKER,
            "lease_token": LEASE,
            "result": SPEECH_RESULT,
            "idempotency_key": KEY,
        },
        "batch_id",
    ),
    Case(
        "pandrator_create_speech_optimization_dispatch_run",
        {"session_id": MARKER, "idempotency_key": KEY},
        "session_id",
    ),
    Case(
        "pandrator_submit_speech_optimization_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": LEASE,
            "result": SPEECH_RESULT,
            "idempotency_key": KEY,
            "character_proposals": [{"name": "fixture-private-proposal"} for _ in range(101)],
        },
        "character_proposals",
        "fixture-private-proposal",
    ),
]


async def invoke(runtime: McpRuntime, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(adapter.build_server(runtime), mode="auto", raise_exceptions=False) as client:
        return await client.call_tool(tool, arguments)


def safe_failure(result: Any, tool: str, field: str | None = None) -> dict[str, Any]:
    assert result.is_error
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    prefix = f"Error executing tool {tool}: "
    text = block.text.removeprefix(prefix)
    assert text.startswith("{"), f"Native failure body: {block.text}"
    failure = json.loads(text)
    assert failure["code"] == "validation_error", f"Native failure body: {block.text}"
    assert isinstance(failure["request_id"], str) and failure["request_id"]
    assert failure["retryable"] is False
    assert failure["next_actions"] == []
    errors = failure["details"]["errors"]
    assert errors
    if field is not None:
        assert any(error["loc"] == [field] for error in errors)
    assert all(key not in error for error in errors for key in ("input", "ctx", "url"))
    return failure


@pytest.mark.parametrize("case", CASES, ids=[f"{case.tool}-{case.field}" for case in CASES])
def test_native_dispatch_domain_validation_is_guarded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, case.tool, case.arguments))
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    failure = safe_failure(result, case.tool, case.field)
    assert case.private_value not in json.dumps(failure)


def test_source_get_validation_print_and_correlation_are_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[dict[str, str]] = []

    class PrintingInput(GetSourceCleaningDispatchRunInput):
        @model_validator(mode="before")
        @classmethod
        def probe(cls, value: Any) -> Any:
            print(SENTINEL)
            observed.append(correlation_headers())
            raise ValueError("Fixture guard validation")

    monkeypatch.setattr(adapter, "GetSourceCleaningDispatchRunInput", PrintingInput)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    tool = "pandrator_get_source_cleaning_dispatch_run"
    result = asyncio.run(invoke(runtime, tool, {"run_id": "run-1"}))
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err
    failure = safe_failure(result, tool)
    assert len(observed) == 1
    assert observed[0]["X-Request-ID"] == failure["request_id"]
