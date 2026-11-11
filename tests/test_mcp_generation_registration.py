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
        {"limit": 20, "include_repairs": False, "view": "compact"},
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
            "audio_version_side": "after",
            "view": "compact",
            "around_ordinal": 2,
            "radius": 2,
        },
    ),
    Case(
        "pandrator_get_speech_plan_status",
        BASE,
        "get_speech_plan_status",
        (SESSION,),
        {"summary": True},
    ),
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
        {"limit": 50, "before_revision_number": None, "summary": True},
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
        {
            **GUARD,
            "speech_plan_revision_id": "revision-1",
            "missing_only": True,
            "settings_source": "run",
            "settings_source_run_id": "settings-source-run-1",
        },
        "start_generation_run",
        (SESSION,),
        {
            "speech_plan_revision_id": "revision-1",
            "stale_only": False,
            "missing_only": True,
            "idempotency_key": KEY,
            "view": "compact",
            "settings_source": "run",
            "settings_source_run_id": "settings-source-run-1",
            "admission": True,
        },
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
        {
            **GUARD,
            "segment_ids": ["segment-1"],
            "generation_run_id": "audio-origin-run-1",
            "settings_source": "current",
        },
        "start_generation_run",
        (SESSION,),
        {
            "segment_ids": ["segment-1"],
            "generation_run_id": "audio-origin-run-1",
            "operation": "regenerate",
            "idempotency_key": KEY,
            "settings_source": "current",
            "settings_source_run_id": None,
            "admission": True,
        },
    ),
    Case(
        "pandrator_assemble_generation_run",
        GUARD,
        "create_output_assembly",
        (SESSION,),
        {"generation_run_id": None, "idempotency_key": KEY, "audio_version_side": "after"},
    ),
]

PAYLOADS = {
    "list_generation_runs": {
        "items": [
            {"id": "run-1", "status": "succeeded", "settings_snapshot": {"tts": {}}},
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
        "id": "admission-job-1",
        "job_id": "admission-job-1",
        "session_id": SESSION,
        "status": "queued",
        "stage": "preparing",
        "speech_plan_revision_id": "revision-1",
        "request_id": "request-1",
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
        expected_replay = dict(arguments)
        if case.name == "pandrator_generate_speech_plan":
            # The native SDK fills signature defaults before validated dispatch.
            expected_replay.setdefault("stale_only", False)
            expected_replay.setdefault("view", "compact")
            expected_replay.setdefault("admission", True)
        assert value["next_actions"][-1]["arguments"] == expected_replay
    else:
        value = envelope(result)
        assert isinstance(value["result"], dict)
        if case.name == "pandrator_list_generation_runs":
            assert value["result"]["items"] == [{"id": "run-1", "status": "succeeded"}]
        if case.name == "pandrator_update_generation_segments":
            assert value["result"]["updated_count"] == 1
            assert "private" not in value["result"]
            assert "private" not in value["result"]["items"][0]
        if case.name in {"pandrator_generate_speech_plan", "pandrator_regenerate_segments"}:
            assert value["work"]["id"] == "admission-job-1"
            assert value["result"]["generation_operation_id"] == "admission-job-1"
            assert [action["tool"] for action in value["next_actions"]] == ["pandrator_get_generation_operation"]
            assert value["next_actions"][0]["arguments"] == {"operation_id": "admission-job-1"}
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


def test_registered_generation_admission_schema_defaults_true(tmp_path: Path) -> None:
    async def capture() -> dict[str, Any]:
        runtime, _ = fixture_runtime(tmp_path)
        async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
            result = await client.list_tools()
        return result.model_dump(mode="json", by_alias=True)

    metadata = asyncio.run(capture())
    tools = {tool["name"]: tool for tool in metadata["tools"]}
    for tool_name in ("pandrator_generate_speech_plan", "pandrator_regenerate_segments"):
        schema = tools[tool_name]["inputSchema"]
        properties = schema["properties"]
        admission = schema["properties"]["admission"]
        assert admission["type"] == "boolean"
        assert admission["default"] is True
        assert properties["settings_source"]["default"] == "current"
        assert properties["settings_source"]["enum"] == [
            "current",
            "matching_run",
            "run",
        ]
        assert "settings_source_run_id" in properties
        if tool_name == "pandrator_generate_speech_plan":
            assert properties["missing_only"]["default"] is False
        else:
            assert "generation_run_id" in properties


REUSE = {
    "reusable_segment_count": 7,
    "stale_segment_count": 2,
    "audio_settings_stale_segment_count": 1,
    "audio_identity_unknown_segment_count": 0,
    "audio_reuse_checked": True,
}


def _unchecked(revision_id: str) -> dict[str, Any]:
    return {
        "id": revision_id,
        "reusable_segment_count": None,
        "stale_segment_count": None,
        "audio_settings_stale_segment_count": None,
        "audio_identity_unknown_segment_count": None,
        "audio_reuse_checked": False,
    }


def _reuse_runtime(
    root: Path,
    *,
    summary: dict[str, Any],
    detail_error: str | None = None,
) -> tuple[
    McpRuntime, list[tuple[str, tuple[Any, ...], dict[str, Any]]], list[dict[str, str]]
]:
    runtime = build_runtime(
        McpSettings(target_name="unconfigured", configuration_path=root / "absent.json")
    )
    application = create_autospec(ApplicationClient, instance=True)
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    headers: list[dict[str, str]] = []

    def summary_call(method: str):
        def invoke(*args, **kwargs):
            calls.append((method, args, kwargs))
            headers.append(correlation_headers())
            return copy.deepcopy(summary)

        return invoke

    def detail(*args, **kwargs):
        calls.append(("get_speech_plan_revision", args, kwargs))
        headers.append(correlation_headers())
        if detail_error:
            raise PandratorMcpError(detail_error, "Fixture detail failure.")
        return {"revision": {"id": args[1], **REUSE}, "active_revision_id": args[1]}

    application.get_speech_plan_status.side_effect = summary_call("get_speech_plan_status")
    application.list_speech_plan_revisions.side_effect = summary_call("list_speech_plan_revisions")
    application.get_speech_plan_revision.side_effect = detail
    runtime.application = application
    runtime.startup_error = None
    return runtime, calls, headers


def _call_with_headers(root: Path, tool: str, arguments: dict[str, Any], **kwargs):
    runtime, calls, headers = _reuse_runtime(root, **kwargs)

    async def run():
        async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(run()), calls, headers


def _call(root: Path, tool: str, arguments: dict[str, Any], **kwargs):
    result, calls, _ = _call_with_headers(root, tool, arguments, **kwargs)
    return result, calls


ACTIVE_SUMMARY = {
    "active_revision_id": "rev-2",
    "selected_revision_id": "rev-2",
    "audio_reuse_checked": False,
    "items": [_unchecked("rev-3"), _unchecked("rev-2"), _unchecked("rev-1")],
}


def test_status_merges_reuse_counts_for_active_revision_only(tmp_path: Path) -> None:
    result, calls = _call(tmp_path, "pandrator_get_speech_plan_status", BASE, summary=ACTIVE_SUMMARY)
    value = envelope(result)
    assert calls == [
        ("get_speech_plan_status", (SESSION,), {"summary": True}),
        ("get_speech_plan_revision", (SESSION, "rev-2"), {}),
    ]
    items = {item["id"]: item for item in value["result"]["items"]}
    assert {key: items["rev-2"][key] for key in REUSE} == REUSE
    assert items["rev-3"] == _unchecked("rev-3")
    assert items["rev-1"] == _unchecked("rev-1")
    assert value["result"]["audio_reuse_checked"] is False
    assert not value.get("warnings")


def test_list_passes_paging_and_merges_active_counts(tmp_path: Path) -> None:
    result, calls = _call(
        tmp_path,
        "pandrator_list_speech_plan_revisions",
        {**BASE, "limit": 3, "before_revision_number": 9},
        summary=ACTIVE_SUMMARY,
    )
    value = envelope(result)
    assert calls == [
        (
            "list_speech_plan_revisions",
            (SESSION,),
            {"limit": 3, "before_revision_number": 9, "summary": True},
        ),
        ("get_speech_plan_revision", (SESSION, "rev-2"), {}),
    ]
    items = {item["id"]: item for item in value["result"]["items"]}
    assert items["rev-2"]["audio_reuse_checked"] is True
    assert items["rev-3"] == _unchecked("rev-3")
    assert items["rev-1"] == _unchecked("rev-1")
    assert value["result"]["audio_reuse_checked"] is False
    assert value["result"]["session_id"] == SESSION


@pytest.mark.parametrize(
    "summary",
    [
        {"active_revision_id": None, "selected_revision_id": None, "items": []},
        {"active_revision_id": None, "selected_revision_id": None, "items": [_unchecked("rev-1")]},
        {"active_revision_id": "rev-9", "selected_revision_id": "rev-9", "items": [_unchecked("rev-1")]},
    ],
    ids=["no-plan", "null-active-with-page", "active-on-another-page"],
)
@pytest.mark.parametrize(
    "tool", ["pandrator_get_speech_plan_status", "pandrator_list_speech_plan_revisions"]
)
def test_no_detail_request_without_active_item(tmp_path: Path, tool: str, summary) -> None:
    result, calls = _call(tmp_path, tool, BASE, summary=summary)
    envelope(result)
    assert "get_speech_plan_revision" not in [call[0] for call in calls]
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["not_found", "application_response_timeout"])
@pytest.mark.parametrize(
    "tool", ["pandrator_get_speech_plan_status", "pandrator_list_speech_plan_revisions"]
)
def test_detail_race_or_timeout_degrades_with_warning(tmp_path: Path, tool: str, code: str) -> None:
    result, calls = _call(tmp_path, tool, BASE, summary=ACTIVE_SUMMARY, detail_error=code)
    value = envelope(result)
    assert len(calls) == 2
    assert [item["audio_reuse_checked"] for item in value["result"]["items"]] == [False] * 3
    assert [warning["code"] for warning in value["warnings"]] == [code]
    assert "Fixture detail failure." in value["warnings"][0]["message"]


@pytest.mark.parametrize(
    "tool", ["pandrator_get_speech_plan_status", "pandrator_list_speech_plan_revisions"]
)
def test_detail_request_shares_the_tool_calls_correlation_headers(
    tmp_path: Path, tool: str
) -> None:
    result, calls, headers = _call_with_headers(tmp_path, tool, BASE, summary=ACTIVE_SUMMARY)
    envelope(result)
    assert [call[0] for call in calls][1:] == ["get_speech_plan_revision"]
    traceparent = re.compile(r"00-([0-9a-f]{32})-[0-9a-f]{16}-01")
    summary_headers, detail_headers = headers
    summary_trace = traceparent.fullmatch(summary_headers["traceparent"])
    detail_trace = traceparent.fullmatch(detail_headers["traceparent"])
    assert summary_trace and detail_trace
    assert detail_trace.group(1) == summary_trace.group(1)
    assert detail_headers["X-Request-ID"] == summary_headers["X-Request-ID"]


@pytest.mark.parametrize(
    "tool", ["pandrator_get_speech_plan_status", "pandrator_list_speech_plan_revisions"]
)
def test_detail_other_failure_propagates(tmp_path: Path, tool: str) -> None:
    result, calls = _call(tmp_path, tool, BASE, summary=ACTIVE_SUMMARY, detail_error="scope_denied")
    assert result.is_error
    assert len(calls) == 2
    block = next(item for item in result.content if isinstance(item, TextContent))
    prefix = f"Error executing tool {tool}: "
    assert block.text.startswith(prefix)
    assert json.loads(block.text[len(prefix) :])["code"] == "scope_denied"
