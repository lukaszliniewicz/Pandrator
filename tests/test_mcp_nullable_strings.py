"""Nullable native strings retain text, source selection and field admission."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from tests.test_mcp_generic_dispatch_registration import CASES as DISPATCH_CASES
from tests.test_mcp_generic_dispatch_registration import PAYLOAD
from tests.test_mcp_media_edit_registration import fixture_runtime

KEY = "nullable:string:1"
SENTINEL = "nullable native string fixture stdout"
OMITTED = object()


@dataclass(frozen=True)
class Case:
    tool: str
    parameter: str
    method: str
    arguments: dict[str, Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    change: bool = False
    null_invalid: bool = False


CASES = [
    Case(
        "pandrator_update_generation_segment",
        "text",
        "update_generation_segment",
        {
            "session_id": "session-1",
            "segment_id": "segment-1",
            "expected_revision": 4,
            "idempotency_key": KEY,
            "removed": False,
        },
        ("segment-1",),
        {"changes": {"removed": False}, "expected_revision": 4, "idempotency_key": KEY},
        change=True,
    ),
    Case(
        "pandrator_update_generation_segment",
        "optimized_text",
        "update_generation_segment",
        {
            "session_id": "session-1",
            "segment_id": "segment-1",
            "expected_revision": 4,
            "idempotency_key": KEY,
            "text": "Fixture",
        },
        ("segment-1",),
        {"changes": {"text": "Fixture"}, "expected_revision": 4, "idempotency_key": KEY},
        change=True,
    ),
    Case(
        "pandrator_update_media_edit",
        "instructions",
        "update_media_edit",
        {
            "session_id": "session-1",
            "expected_revision": 2,
            "keep_ranges": [{"start_ms": 0, "end_ms": 1000}],
            "reviewed": False,
            "idempotency_key": KEY,
        },
        ("session-1",),
        {
            "expected_revision": 2,
            "idempotency_key": KEY,
            "keep_ranges": [{"start_ms": 0, "end_ms": 1000}],
            "reviewed": False,
        },
    ),
    Case(
        "pandrator_list_sessions",
        "query",
        "list_sessions",
        {},
        (),
        {"limit": 50, "query": None, "include_trashed": False},
    ),
    Case(
        "pandrator_resolve_subtitle_evidence",
        "text",
        "resolve_subtitle_evidence",
        {
            "session_id": "session-1",
            "evidence_id": "evidence-1",
            "action": "edited",
            "idempotency_key": KEY,
        },
        ("session-1", "evidence-1"),
        {
            "action": "edited",
            "candidate_id": None,
            "text": None,
            "note": "",
            "idempotency_key": KEY,
        },
        null_invalid=True,
    ),
    Case(
        "pandrator_update_session",
        "name",
        "update_session",
        {"session_id": "session-1", "expected_revision": 4, "idempotency_key": KEY},
        ("session-1",),
        {"expected_revision": 4, "changes": {}, "idempotency_key": KEY},
        change=True,
        null_invalid=True,
    ),
    Case(
        "pandrator_create_dispatch_run",
        "source_artifact_id",
        "create_dispatch_run",
        {"session_id": "session-1", "kind": "correction", "idempotency_key": KEY},
        ("session-1",),
        {**DISPATCH_CASES[2].kwargs, "idempotency_key": KEY},
    ),
]


async def invoke(
    root: Path, case: Case, value: Any, protocol: str = "auto"
) -> tuple[Any, list[dict[str, Any]], Any]:
    runtime, _, application = fixture_runtime(root)
    calls: list[dict[str, Any]] = []

    def effect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": list(args), "kwargs": kwargs, "headers": correlation_headers()})
        print(SENTINEL)
        return copy.deepcopy(PAYLOAD)

    getattr(application, case.method).side_effect = effect
    arguments = copy.deepcopy(case.arguments)
    if value is not OMITTED:
        arguments[case.parameter] = value
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    async with Client(build_server(runtime), mode=protocol, raise_exceptions=False) as client:
        result = await client.call_tool(case.tool, arguments)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    return result, calls, application


def assert_success(
    result: Any, calls: list[dict[str, Any]], application: Any, case: Case, value: Any
) -> None:
    assert not result.is_error
    assert len(result.content) == 1 and isinstance(result.content[0], TextContent)
    envelope = json.loads(result.content[0].text)
    assert envelope == result.structured_content
    assert set(envelope) == {
        "schema_version",
        "request_id",
        "result",
        "work",
        "warnings",
        "next_actions",
    }
    assert envelope["schema_version"] == "1" and envelope["request_id"]
    assert len(calls) == len(application.mock_calls) == 1
    expected = copy.deepcopy(case.kwargs)
    if value is not None and value is not OMITTED:
        if case.change:
            expected["changes"][case.parameter] = value
        else:
            expected[case.parameter] = value
    assert calls[0]["args"] == list(case.args)
    assert calls[0]["kwargs"] == expected
    assert calls[0]["headers"]["X-Request-ID"] == envelope["request_id"]
    assert calls[0]["headers"]["traceparent"].startswith("00-")


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.tool + ":" + c.parameter)
@pytest.mark.parametrize(
    "value",
    ["null", "[]", '{"text":"Fixture"}', "Fixture", None, OMITTED],
    ids=["null-text", "array-text", "object-text", "plain", "none", "omitted"],
)
def test_native_nullable_string_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], protocol: str, case: Case, value: Any
) -> None:
    result, calls, application = asyncio.run(invoke(tmp_path, case, value, protocol))
    if case.null_invalid and (value is None or value is OMITTED):
        assert result.is_error and calls == application.mock_calls == []
        captured = capsys.readouterr()
        assert captured.out == "" and SENTINEL not in captured.err
    else:
        assert_success(result, calls, application, case, value)
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err.count(SENTINEL) == 1


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.tool + ":" + c.parameter)
@pytest.mark.parametrize("value", [{}, True, 1], ids=["object", "boolean", "integer"])
def test_native_nullable_string_rejects_non_strings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case, value: Any
) -> None:
    result, calls, application = asyncio.run(invoke(tmp_path, case, value))
    assert result.is_error and calls == application.mock_calls == []
    assert capsys.readouterr().out == ""


LIMITS = [
    (0, ""),
    (0, " "),
    (0, "x" * 2001),
    (1, "x" * 2001),
    (2, "x" * 10001),
    (3, "x" * 101),
    (4, "x" * 16001),
    (5, ""),
    (5, "x" * 201),
    (6, ""),
    (6, "x" * 81),
]


@pytest.mark.parametrize(
    "index,value",
    LIMITS,
    ids=[
        "text-empty",
        "text-blank",
        "text-length",
        "speech-length",
        "instructions-length",
        "query-length",
        "evidence-length",
        "name-empty",
        "name-length",
        "source-empty",
        "source-length",
    ],
)
def test_native_nullable_string_preserves_field_limits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], index: int, value: str
) -> None:
    result, calls, application = asyncio.run(invoke(tmp_path, CASES[index], value))
    assert result.is_error and calls == application.mock_calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("index", [1, 2], ids=["clear-speech-text", "clear-instructions"])
def test_native_nullable_string_retains_empty_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], index: int
) -> None:
    case = CASES[index]
    result, calls, application = asyncio.run(invoke(tmp_path, case, ""))
    assert_success(result, calls, application, case, "")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
