"""Media-edit registrations preserve native transport and guarded application calls."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass, replace
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
RUN = "run-1"
BATCH = "batch-1"
LEASE = "fixture-lease"
KEY = "registration:fixture:1"
SENTINEL = "media-edit registration fixture stdout"
BASE = {"session_id": SESSION}
GUARD = {**BASE, "idempotency_key": KEY}
BATCH_GUARD = {"batch_id": BATCH, "lease_token": LEASE, "idempotency_key": KEY}
EMPTY_RESULT = {"kind": "media_edit", "cuts": []}
CUT_RESULT = {
    "kind": "media_edit",
    "cuts": [{"start_cue_id": "cue-1", "end_cue_id": "cue-2", "reason": "Setup"}],
}
FILLED_CUT_RESULT = {
    "kind": "media_edit",
    "cuts": [
        {
            "start_cue_id": "cue-1",
            "start_at_media_start": False,
            "end_cue_id": "cue-2",
            "end_at_media_end": False,
            "reason": "Setup",
        }
    ],
}


@dataclass(frozen=True)
class Case:
    name: str
    arguments: dict[str, Any]
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


CASES = [
    Case("pandrator_get_media_edit", {**BASE, "view": "summary"}, "get_media_edit", (SESSION,), {}),
    Case(
        "pandrator_list_media_edit_cuts",
        BASE,
        "list_media_edit_cuts",
        (SESSION,),
        {"revision": None},
    ),
    Case(
        "pandrator_inspect_media_edit_boundary",
        {**BASE, "cut_index": 1, "edge": "end"},
        "inspect_media_edit_boundary",
        (SESSION,),
        {"cut_index": 1, "edge": "end", "revision": None, "context_ms": 5000, "cue_limit": 40},
    ),
    Case(
        "pandrator_plan_media_edit_workflow",
        {**BASE, "instructions": "Trim setup chatter."},
        "get_session",
        (SESSION,),
        {},
    ),
    Case(
        "pandrator_prepare_media_edit",
        GUARD,
        "prepare_media_edit",
        (SESSION,),
        {"force": False, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_update_media_edit",
        {
            **GUARD,
            "expected_revision": 2,
            "keep_ranges": [{"start_ms": 0, "end_ms": 1000}],
            "reviewed": False,
        },
        "update_media_edit",
        (SESSION,),
        {
            "expected_revision": 2,
            "keep_ranges": [{"start_ms": 0, "end_ms": 1000}],
            "idempotency_key": KEY,
            "reviewed": False,
        },
    ),
    Case(
        "pandrator_refine_media_edit_boundary",
        {**GUARD, "expected_revision": 2, "cut_index": 1, "edge": "end", "delta_ms": 50},
        "refine_media_edit_boundary",
        (SESSION,),
        {
            "expected_revision": 2,
            "cut_index": 1,
            "edge": "end",
            "position_ms": None,
            "delta_ms": 50,
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_propose_media_edit",
        {
            **GUARD,
            "revision": 2,
            "instructions": " Trim setup chatter. ",
            "model": " fixture-model ",
            "wait": False,
        },
        "propose_media_edit",
        (SESSION,),
        {
            "revision": 2,
            "instructions": "Trim setup chatter.",
            "model": "fixture-model",
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_render_media_edit",
        {**GUARD, "revision": 2, "subtitles_only": True, "wait": False},
        "render_media_edit",
        (SESSION,),
        {"revision": 2, "subtitles_only": True, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_create_media_edit_dispatch_run",
        {**GUARD, "revision": 2, "instructions": " Trim setup chatter. "},
        "create_media_edit_dispatch_run",
        (SESSION,),
        {"revision": 2, "instructions": "Trim setup chatter.", "idempotency_key": KEY},
    ),
    Case(
        "pandrator_list_media_edit_dispatch_runs",
        BASE,
        "list_media_edit_dispatch_runs",
        (SESSION,),
        {"limit": 50},
    ),
    Case(
        "pandrator_get_media_edit_dispatch_run",
        {"run_id": RUN},
        "get_media_edit_dispatch_run",
        (RUN,),
        {},
    ),
    Case(
        "pandrator_claim_media_edit_dispatch_batch",
        {"run_id": RUN, "idempotency_key": KEY},
        "claim_media_edit_dispatch_batch",
        (RUN,),
        {"lease_seconds": 900, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_renew_media_edit_dispatch_batch",
        BATCH_GUARD,
        "renew_media_edit_dispatch_batch",
        (BATCH,),
        {"lease_token": LEASE, "lease_seconds": 900, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_release_media_edit_dispatch_batch",
        BATCH_GUARD,
        "release_media_edit_dispatch_batch",
        (BATCH,),
        {"lease_token": LEASE, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_submit_media_edit_dispatch_batch",
        {**BATCH_GUARD, "result": EMPTY_RESULT},
        "submit_media_edit_dispatch_batch",
        (BATCH,),
        {"lease_token": LEASE, "result": EMPTY_RESULT, "idempotency_key": KEY},
    ),
]

RUN_PAYLOAD = {
    "id": RUN,
    "session_id": SESSION,
    "status": "ready",
    "source_revision": 2,
    "private": "omit",
    "batches": [{"private": "omit"}],
}
PAYLOADS = {
    "get_media_edit": {
        "readiness": {},
        "plan": {
            "revision": 2,
            "revision_id": "revision-2",
            "reviewed": False,
            "cues": [{"id": "cue-1"}],
            "keep_ranges": [{"start_ms": 0, "end_ms": 1000}],
            "instructions": "Trim setup chatter.",
            "evidence": {"private": "omit"},
        },
    },
    "get_session": {
        "id": SESSION,
        "workflow_kind": "media_edit",
        "revision": 4,
        "name": "Media edit",
        "status": "ready",
    },
    "get_workflow": {"revision": 4, "stages": []},
    "get_session_settings": {"revision": 3, "effective": {}, "override": {}},
    "list_media_edit_cuts": {"revision": 2, "cuts": []},
    "inspect_media_edit_boundary": {"revision": 2, "cut_index": 1, "edge": "end", "cues": []},
    "prepare_media_edit": {"revision": 3},
    "update_media_edit": {"revision": 3},
    "refine_media_edit_boundary": {"current_revision": {"revision": 3}, "affected_cut_index": 1},
    "propose_media_edit": {
        "id": "job-1",
        "status": "queued",
        "kind": "edit_media",
        "session_id": SESSION,
    },
    "render_media_edit": {
        "id": "job-1",
        "status": "queued",
        "kind": "render_media_edit",
        "session_id": SESSION,
    },
    "wait_for_job": {
        "id": "job-1",
        "status": "succeeded",
        "kind": "edit_media",
        "session_id": SESSION,
    },
    "create_media_edit_dispatch_run": RUN_PAYLOAD,
    "list_media_edit_dispatch_runs": {"items": [RUN_PAYLOAD]},
    "get_media_edit_dispatch_run": RUN_PAYLOAD,
    "claim_media_edit_dispatch_batch": {
        "run_id": RUN,
        "batch_id": BATCH,
        "status": "leased",
        "lease_token": LEASE,
        "lease_expires_at": "2030-01-01T00:00:00Z",
        "task": {"kind": "media_edit"},
        "batch": {"cues": []},
        "private": "omit",
    },
    "renew_media_edit_dispatch_batch": {"batch_id": BATCH, "status": "leased"},
    "release_media_edit_dispatch_batch": {"batch_id": BATCH, "status": "ready"},
    "submit_media_edit_dispatch_batch": {
        "run_id": RUN,
        "batch_id": BATCH,
        "session_id": SESSION,
        "status": "finalizing",
        "accepted": True,
        "source_revision": 2,
        "remaining_batch_count": 0,
    },
}

EXTRAS = [
    replace(CASES[0], arguments={**BASE, "response_mode": "structured"}),
    replace(CASES[0], arguments={**BASE, "view": "full"}),
    replace(CASES[0], arguments={**BASE, "view": "full", "response_mode": "structured"}),
    replace(CASES[7], arguments={**CASES[7].arguments, "wait": True, "timeout_seconds": 7}),
    replace(CASES[8], arguments={**CASES[8].arguments, "wait": True, "timeout_seconds": 7}),
    replace(
        CASES[15],
        arguments={**BATCH_GUARD, "result": CUT_RESULT},
        kwargs={"lease_token": LEASE, "result": FILLED_CUT_RESULT, "idempotency_key": KEY},
    ),
]


def fixture_runtime(
    root: Path, failure: str | None = None
) -> tuple[McpRuntime, list[dict[str, Any]], Any]:
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
            print(SENTINEL)
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
    return runtime, calls, application


def envelope(result, structured: bool = False) -> dict[str, Any]:
    assert not result.is_error
    block = next(item for item in result.content if isinstance(item, TextContent))
    text = json.loads(block.text)
    value = result.structured_content
    if structured:
        assert text == {
            "schema_version": "1",
            "request_id": value["request_id"],
            "data": "structuredContent",
        }
    else:
        assert text == value
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


def expected_calls(case: Case) -> list[dict[str, Any]]:
    result = [{"method": case.method, "args": list(case.args), "kwargs": case.kwargs}]
    if case.name == "pandrator_plan_media_edit_workflow":
        result.extend(
            [
                {"method": "get_workflow", "args": [SESSION], "kwargs": {}},
                {"method": "get_media_edit", "args": [SESSION], "kwargs": {}},
                {"method": "get_session_settings", "args": [SESSION, "stt"], "kwargs": {}},
            ]
        )
    if case.arguments.get("wait") is True:
        result.append(
            {"method": "wait_for_job", "args": ["job-1"], "kwargs": {"timeout_seconds": 7}}
        )
    return result


def assert_projection(case: Case, value: dict[str, Any]) -> None:
    result = value["result"]
    if case.name == "pandrator_get_media_edit":
        if case.arguments.get("view") == "full":
            assert result == PAYLOADS["get_media_edit"]
        else:
            assert result == {
                "readiness": {},
                "plan": {
                    "revision": 2,
                    "revision_id": "revision-2",
                    "reviewed": False,
                    "cue_count": 1,
                    "keep_range_count": 1,
                    "kept_duration_ms": 1000,
                },
            }
    if case.name in {
        "pandrator_create_media_edit_dispatch_run",
        "pandrator_get_media_edit_dispatch_run",
    }:
        assert result == {
            "schema_version": "1",
            "id": RUN,
            "session_id": SESSION,
            "status": "ready",
            "source_revision": 2,
        }
    if case.name == "pandrator_list_media_edit_dispatch_runs":
        assert result == {
            "schema_version": "1",
            "items": [
                {
                    "schema_version": "1",
                    "id": RUN,
                    "session_id": SESSION,
                    "status": "ready",
                    "source_revision": 2,
                }
            ],
        }
    if case.name == "pandrator_claim_media_edit_dispatch_batch":
        assert result == {
            "schema_version": "1",
            "run_id": RUN,
            "batch_id": BATCH,
            "status": "leased",
            "lease_token": LEASE,
            "lease_expires_at": "2030-01-01T00:00:00Z",
            "task": {"kind": "media_edit"},
            "batch": {"cues": []},
        }
    if case.name == "pandrator_submit_media_edit_dispatch_batch":
        assert value["next_actions"][0]["tool"] == case.name
        assert value["next_actions"][0]["arguments"] == {
            "batch_id": BATCH,
            "lease_token": LEASE,
            "idempotency_key": KEY,
            "result": case.kwargs["result"],
        }
    if case.arguments.get("wait") is True:
        assert result == PAYLOADS["wait_for_job"]
        assert value["next_actions"] == []


async def invoke_case(
    root: Path, case: Case, *, invalid: bool = False, timeout: bool = False
) -> dict[str, Any]:
    runtime, calls, application = fixture_runtime(root, failure=case.method if timeout else None)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    arguments = copy.deepcopy(case.arguments)
    if invalid:
        identifier = next(key for key in ("session_id", "run_id", "batch_id") if key in arguments)
        arguments[identifier] = ""
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        result = await client.call_tool(case.name, arguments)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    if invalid:
        assert result.is_error
        assert calls == []
        assert application.mock_calls == []
        return {
            "name": case.name,
            "invalid": True,
            "result": result.model_dump(mode="json", by_alias=True),
        }
    assert [{k: v for k, v in call.items() if k != "headers"} for call in calls] == expected_calls(
        case
    )
    assert len(application.mock_calls) == len(calls)
    for call in calls:
        assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", call["headers"]["traceparent"])
    if timeout:
        assert result.is_error
        block = next(item for item in result.content if isinstance(item, TextContent))
        prefix = f"Error executing tool {case.name}: "
        assert block.text.startswith(prefix)
        value = json.loads(block.text[len(prefix) :])
        assert value["code"] == "application_response_timeout"
        assert value["details"] == {"operation_outcome": "unknown"}
        assert value["retryable"] is True
        # Original media-edit enqueue failures have no authored replay action.
        assert value["next_actions"] == []
    else:
        value = envelope(result, structured=arguments.get("response_mode") == "structured")
        assert isinstance(value["result"], dict)
        assert_projection(case, value)
    for call in calls:
        assert value["request_id"] == call["headers"]["X-Request-ID"]
    return {
        "name": case.name,
        "arguments": arguments,
        "calls": calls,
        "envelope": value,
        "timeout": timeout,
    }


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_registered_media_edit_call(tmp_path: Path, capsys, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_registered_media_edit_invalid_input(tmp_path: Path, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case, invalid=True))


@pytest.mark.parametrize("case", EXTRAS, ids=lambda case: case.name)
def test_native_media_edit_extra_contract(tmp_path: Path, capsys, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


@pytest.mark.parametrize("case", [CASES[7], CASES[8]], ids=lambda case: case.name)
def test_native_media_edit_enqueue_timeout(tmp_path: Path, capsys, case: Case) -> None:
    asyncio.run(invoke_case(tmp_path, case, timeout=True))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


async def capture_contract(root: Path) -> dict[str, Any]:
    runtime, _, _ = fixture_runtime(root)
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        metadata = (await client.list_tools()).model_dump(mode="json", by_alias=True)
    assert len(metadata["tools"]) == 162
    records = [await invoke_case(root, case) for case in CASES]
    invalid = [await invoke_case(root, case, invalid=True) for case in CASES]
    extras = [await invoke_case(root, case) for case in EXTRAS]
    # Compare full envelopes across both native modes, ignoring only request IDs.
    for standard, structured in ((records[0], extras[0]), (extras[1], extras[2])):
        assert {k: v for k, v in standard["envelope"].items() if k != "request_id"} == {
            k: v for k, v in structured["envelope"].items() if k != "request_id"
        }
    timeouts = [await invoke_case(root, case, timeout=True) for case in [CASES[7], CASES[8]]]
    return {
        "metadata": metadata,
        "records": records,
        "invalid": invalid,
        "extras": extras,
        "timeouts": timeouts,
    }
