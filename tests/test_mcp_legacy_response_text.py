"""Native legacy submissions preserve opaque nullable text and existing limits."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.context import McpRuntime
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from tests.test_mcp_media_edit_registration import fixture_runtime as base_fixture_runtime

TOOL = "pandrator_submit_dispatch_batch"
KEY = "opaque:submit:1"
SENTINEL = "opaque legacy response fixture stdout"
JSON_RESPONSE = '{"kind":"correction","operations":[],"uncertainties":[]}'
BASE = {"batch_id": "batch-1", "lease_token": "fixture-lease", "idempotency_key": KEY}
DELTA = {"terminology": {}, "entities": {}, "style_rules": [], "decisions": [], "notes": []}
CORRECTION = {"kind": "correction", "operations": [], "uncertainties": []}
CASES = [
    ("object", {"response_text": JSON_RESPONSE}, JSON_RESPONSE, None),
    ("array", {"response_text": "[]"}, "[]", None),
    ("boolean-text", {"response_text": "true"}, "true", None),
    ("quoted-text", {"response_text": '"Fixture"'}, '"Fixture"', None),
    ("null-text", {"response_text": "null"}, "null", None),
    ("numeric-text", {"response_text": "123"}, "123", None),
    (
        "spaced-object",
        {"response_text": " \n" + JSON_RESPONSE + "\t "},
        " \n" + JSON_RESPONSE + "\t ",
        None,
    ),
    ("plain", {"response_text": "Fixture"}, "Fixture", None),
    ("null-result", {"result": None, "response_text": JSON_RESPONSE}, JSON_RESPONSE, None),
    (
        "null-text-with-result",
        {"result": {"kind": "correction"}, "response_text": None},
        None,
        CORRECTION,
    ),
    ("omitted-text-with-result", {"result": {"kind": "correction"}}, None, CORRECTION),
]


def fixture_runtime(root: Path) -> tuple[McpRuntime, list[dict[str, Any]], Any]:
    runtime, _, application = base_fixture_runtime(root)
    calls: list[dict[str, Any]] = []

    def submit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": list(args), "kwargs": kwargs, "headers": correlation_headers()})
        print(SENTINEL)
        return {"batch_id": "batch-1", "run_id": "run-1", "accepted": True}

    application.submit_dispatch_batch.side_effect = submit
    return runtime, calls, application


async def invoke(
    root: Path, arguments: dict[str, Any], protocol: str = "auto"
) -> tuple[Any, list[dict[str, Any]], Any]:
    runtime, calls, application = fixture_runtime(root)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    async with Client(build_server(runtime), mode=protocol, raise_exceptions=False) as client:
        result = await client.call_tool(TOOL, {**BASE, **arguments})
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    return result, calls, application


def assert_success(
    result: Any,
    calls: list[dict[str, Any]],
    application: Any,
    text: str | None,
    typed: dict[str, Any] | None,
) -> None:
    assert not result.is_error
    assert len(result.content) == 1 and isinstance(result.content[0], TextContent)
    value = json.loads(result.content[0].text)
    assert value == result.structured_content
    assert set(value) == {
        "schema_version",
        "request_id",
        "result",
        "work",
        "warnings",
        "next_actions",
    }
    assert value["schema_version"] == "1" and value["work"] is None and value["warnings"] == []
    assert len(calls) == len(application.mock_calls) == 1
    assert calls[0]["args"] == ["batch-1"]
    assert calls[0]["kwargs"] == {
        "lease_token": "fixture-lease",
        "result": typed,
        "response_text": text,
        "context_delta": DELTA,
        "idempotency_key": KEY,
    }
    assert list(calls[0]["kwargs"]) == [
        "lease_token",
        "result",
        "response_text",
        "context_delta",
        "idempotency_key",
    ]
    assert calls[0]["headers"]["X-Request-ID"] == value["request_id"]


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("name,arguments,text,typed", CASES, ids=[case[0] for case in CASES])
def test_native_legacy_response_text_is_opaque(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    protocol: str,
    name: str,
    arguments: dict[str, Any],
    text: str | None,
    typed: dict[str, Any] | None,
) -> None:
    result, calls, application = asyncio.run(invoke(tmp_path, arguments, protocol))
    assert_success(result, calls, application, text, typed)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1


def test_native_response_text_accepts_character_and_byte_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "x" * 524_288
    result, calls, application = asyncio.run(invoke(tmp_path, {"response_text": text}))
    assert_success(result, calls, application, text, None)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1


@pytest.mark.parametrize(
    "arguments,domain",
    [
        ({"response_text": "x" * 524_289}, False),
        ({"response_text": "é" * 262_145}, True),
        ({"result": None, "response_text": None}, True),
    ],
    ids=["character-overflow", "utf8-overflow", "both-null"],
)
def test_native_response_text_retains_admission_limits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: dict[str, Any],
    domain: bool,
) -> None:
    result, calls, application = asyncio.run(invoke(tmp_path, arguments))
    assert result.is_error and calls == application.mock_calls == []
    assert capsys.readouterr().out == ""
    if domain:
        assert len(result.content) == 1 and isinstance(result.content[0], TextContent)
        text = result.content[0].text
        prefix = f"Error executing tool {TOOL}: "
        assert text.startswith(prefix)
        failure = json.loads(text[len(prefix) :])
        assert failure["code"] == "validation_error" and failure["request_id"]
        assert all(
            key not in error
            for error in failure["details"]["errors"]
            for key in ("input", "ctx", "url")
        )
