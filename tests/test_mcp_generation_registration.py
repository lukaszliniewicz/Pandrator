"""Generation registrations preserve native SDK calls and the shared adapter guard."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import McpRuntime, build_runtime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings

SESSION = "session-1"
KEY = "registration:fixture:1"
BASE = {"session_id": SESSION}
GUARD = {"session_id": SESSION, "idempotency_key": KEY}
SEGMENT = {
    "id": "segment-1",
    "ordinal": 0,
    "text": "Reviewed text",
    "revision": 5,
    "removed": False,
    "private": "omit",
}


@dataclass(frozen=True)
class Case:
    name: str
    arguments: dict[str, Any]
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


CASES = [
    Case(
        "pandrator_list_generation_runs",
        BASE,
        "list_generation_runs",
        (SESSION,),
        {"limit": 20, "include_repairs": False},
    ),
    Case(
        "pandrator_list_generation_segments",
        {**BASE, "view": "compact", "around_ordinal": 2},
        "list_generation_segments",
        (SESSION,),
        {
            "cursor": 0,
            "limit": 50,
            "generation_run_id": None,
            "view": "compact",
            "around_ordinal": 2,
            "radius": 2,
        },
    ),
    Case("pandrator_get_speech_plan_status", BASE, "get_speech_plan_status", (SESSION,), {}),
    Case(
        "pandrator_prepare_speech_plan",
        {**GUARD, "expected_revision": 4, "source_artifact_id": "source-1"},
        "prepare_speech_plan",
        (SESSION,),
        {
            "expected_revision": 4,
            "expected_plan_revision_id": None,
            "source_artifact_id": "source-1",
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_review_speech_plan",
        {**GUARD, "revision_id": "revision-1", "content_signature": "a" * 64},
        "review_speech_plan",
        (SESSION,),
        {"revision_id": "revision-1", "content_signature": "a" * 64, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_list_speech_plan_revisions",
        BASE,
        "list_speech_plan_revisions",
        (SESSION,),
        {"limit": 50, "before_revision_number": None},
    ),
    Case(
        "pandrator_revise_speech_block_plan_batch",
        {
            **GUARD,
            "expected_revision_id": "revision-1",
            "operations": [{"action": "split", "segment_id": "segment-1", "cursor": 4}],
        },
        "revise_generation_plan_topology_batch",
        (SESSION,),
        {
            "expected_revision_id": "revision-1",
            "operations": [
                {"action": "split", "text_layer": "display", "segment_id": "segment-1", "cursor": 4}
            ],
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_generate_speech_plan",
        {**GUARD, "speech_plan_revision_id": "revision-1", "stale_only": True},
        "start_generation_run",
        (SESSION,),
        {"speech_plan_revision_id": "revision-1", "stale_only": True, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_revise_speech_block_plan",
        {
            **GUARD,
            "expected_revision_id": "revision-1",
            "action": "split",
            "segment_id": "segment-1",
            "cursor": 4,
            "text_layer": "display",
        },
        "revise_generation_plan_topology",
        (SESSION,),
        {
            "expected_revision_id": "revision-1",
            "action": "split",
            "segment_id": "segment-1",
            "cursor": 4,
            "text_layer": "display",
            "left_segment_id": None,
            "right_segment_id": None,
            "target_revision_id": None,
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_update_generation_segment",
        {
            **GUARD,
            "segment_id": "segment-1",
            "expected_revision": 4,
            "text": "Reviewed text",
            "removed": False,
            "voice_id": None,
        },
        "update_generation_segment",
        ("segment-1",),
        {
            "changes": {"text": "Reviewed text", "removed": False},
            "expected_revision": 4,
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_update_generation_segments",
        {
            **GUARD,
            "updates": [
                {
                    "id": "segment-1",
                    "revision": 4,
                    "changes": {"voice_id": None, "optimized_text": ""},
                }
            ],
        },
        "update_generation_segments",
        (SESSION,),
        {
            "updates": [
                {
                    "id": "segment-1",
                    "revision": 4,
                    "changes": {"voice_id": None, "optimized_text": ""},
                }
            ],
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_select_take",
        {
            "segment_id": "segment-1",
            "take_id": "take-1",
            "expected_revision": 4,
            "idempotency_key": KEY,
        },
        "select_generation_take",
        ("segment-1", "take-1"),
        {"expected_revision": 4, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_regenerate_segments",
        {**GUARD, "segment_ids": ["segment-1"]},
        "start_generation_run",
        (SESSION,),
        {"segment_ids": ["segment-1"], "operation": "regenerate", "idempotency_key": KEY},
    ),
    Case(
        "pandrator_assemble_generation_run",
        GUARD,
        "create_output_assembly",
        (SESSION,),
        {"generation_run_id": None, "idempotency_key": KEY},
    ),
]

PAYLOADS = {
    "list_generation_runs": {
        "items": [
            {"id": "run-1", "status": "succeeded", "private": "omit"},
            {"id": "repair-1", "early_repair_parent_run_id": "run-1"},
        ]
    },
    "list_generation_segments": {
        "items": [SEGMENT],
        "active_plan_revision_id": "revision-1",
        "plan_revision_id": "revision-1",
        "next_cursor": None,
        "total": 1,
    },
    "get_speech_plan_status": {
        "session_id": SESSION,
        "selected_revision_id": "revision-1",
        "ready": True,
    },
    "prepare_speech_plan": {"selected_revision_id": "revision-2"},
    "review_speech_plan": {"reviewed": True},
    "list_speech_plan_revisions": {"items": [{"id": "revision-1", "revision_number": 1}]},
    "revise_generation_plan_topology_batch": {"plan_revision_id": "revision-2"},
    "revise_generation_plan_topology": {"plan_revision_id": "revision-2"},
    "start_generation_run": {
        "id": "run-1",
        "job_id": "job-1",
        "run_id": "run-1",
        "status": "queued",
    },
    "update_generation_segment": SEGMENT,
    "update_generation_segments": {"items": [SEGMENT], "updated_count": 1, "private": "omit"},
    "select_generation_take": SEGMENT,
    "create_output_assembly": {"id": "assembly-1", "job_id": "job-2", "status": "queued"},
}


def fixture_runtime(
    root: Path, failure: str | None = None
) -> tuple[McpRuntime, list[dict[str, Any]]]:
    runtime = build_runtime(
        McpSettings(target_name="unconfigured", configuration_path=root / "absent.json")
    )
    application = create_autospec(ApplicationClient, instance=True)
    calls: list[dict[str, Any]] = []

    def effect(method: str):
        def invoke(*args, **kwargs):
            calls.append(
                {
                    "method": method,
                    "args": list(args),
                    "kwargs": kwargs,
                    "headers": correlation_headers(),
                }
            )
            print("generation registration fixture stdout")
            if method == failure:
                raise PandratorMcpError(
                    "application_response_timeout",
                    "Fixture response timeout.",
                    details={"operation_outcome": "unknown"},
                    retryable=True,
                )
            return copy.deepcopy(PAYLOADS[method])

        return invoke

    for method in PAYLOADS:
        getattr(application, method).side_effect = effect(method)
    runtime.application = application
    runtime.startup_error = None
    return runtime, calls


def envelope(result) -> dict[str, Any]:
    assert not result.is_error
    block = next(item for item in result.content if isinstance(item, TextContent))
    value = json.loads(block.text)
    assert result.structured_content == value
    assert set(value) == {
        "schema_version",
        "request_id",
        "result",
        "work",
        "warnings",
        "next_actions",
    }
    assert value["schema_version"] == "1"
    return value


async def invoke_case(
    root: Path, case: Case, *, invalid: bool = False, timeout: bool = False
) -> dict[str, Any]:
    runtime, calls = fixture_runtime(root, failure=case.method if timeout else None)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    arguments = copy.deepcopy(case.arguments)
    if invalid:
        arguments["segment_id" if case.name == "pandrator_select_take" else "session_id"] = ""
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        result = await client.call_tool(case.name, arguments)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    if invalid:
        assert result.is_error
        assert calls == []
        return {
            "name": case.name,
            "invalid": True,
            "result": result.model_dump(mode="json", by_alias=True),
        }
    assert len(calls) == 1
    assert calls[0]["method"] == case.method
    assert calls[0]["args"] == list(case.args)
    assert calls[0]["kwargs"] == case.kwargs
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", calls[0]["headers"]["traceparent"])
    if timeout:
        assert result.is_error
        block = next(item for item in result.content if isinstance(item, TextContent))
        prefix = f"Error executing tool {case.name}: "
        assert block.text.startswith(prefix)
        value = json.loads(block.text[len(prefix) :])
        assert value["code"] == "application_response_timeout"
        assert value["details"] == {"operation_outcome": "unknown"}
        assert value["retryable"] is True
        assert value["next_actions"][1]["arguments"] == arguments
    else:
        value = envelope(result)
        assert isinstance(value["result"], dict)
        if case.name == "pandrator_list_generation_runs":
            assert value["result"]["items"] == [{"id": "run-1", "status": "succeeded"}]
        if case.name == "pandrator_update_generation_segments":
            assert value["result"]["updated_count"] == 1
            assert "private" not in value["result"]
            assert "private" not in value["result"]["items"][0]
    assert value["request_id"] == calls[0]["headers"]["X-Request-ID"]
    return {"name": case.name, "calls": calls, "envelope": value, "timeout": timeout}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_registered_generation_call(tmp_path: Path, capsys, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "generation registration fixture stdout" in captured.err


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_registered_generation_invalid_input(tmp_path: Path, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case, invalid=True))


@pytest.mark.parametrize("case", [CASES[6], CASES[7]], ids=lambda case: case.name)
def test_native_generation_timeout_preserves_replay_request(tmp_path: Path, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case, timeout=True))


async def capture_contract(root: Path) -> dict[str, Any]:
    runtime, _ = fixture_runtime(root)
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        metadata = (await client.list_tools()).model_dump(mode="json", by_alias=True)
    records = [await invoke_case(root, case) for case in CASES]
    invalid = [await invoke_case(root, case, invalid=True) for case in CASES]
    timeouts = [await invoke_case(root, case, timeout=True) for case in [CASES[6], CASES[7]]]
    return {"metadata": metadata, "records": records, "invalid": invalid, "timeouts": timeouts}
