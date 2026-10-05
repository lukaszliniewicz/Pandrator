"""Session registrations preserve native calls, explicit changes, and setup boundaries."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import call

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import BaseModel

import pandrator_mcp.registrations.sessions as session_owner
import pandrator_mcp.server as adapter
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.results import ToolOutcome
from pandrator_mcp.schemas import MultilingualSetup, UpdateSessionInput
from tests.test_mcp_generic_dispatch_validation import failure_value
from tests.test_mcp_media_edit_registration import fixture_runtime

PROTOCOLS = ["2026-07-28", "legacy"]
SESSION = "session-1"
KEY = "registration:session:1"
SENTINEL = "session registration fixture stdout sentinel"
PRIVATE = "private-session-backend-payload"
MARKER = "private-session-domain-fixture-" * 8
BASE = {"session_id": SESSION, "expected_revision": 4, "idempotency_key": KEY}
SESSION_PAYLOAD = {
    "id": SESSION,
    "name": "Fixture",
    "workflow_kind": "audiobook",
    "source_language": "auto",
    "target_language": None,
    "workflow_preset": "custom",
    "included_stages": [],
    "status": "draft",
    "revision": 4,
    "private_fixture": PRIVATE,
}
ATTACHMENT_PAYLOAD = {
    "id": "attachment-1",
    "session_id": SESSION,
    "source_asset_id": "source-1",
    "role": "primary",
    "is_current": True,
    "revision": 2,
    "session_revision": 4,
    "private_fixture": PRIVATE,
}


@dataclass(frozen=True)
class Case:
    tool: str
    arguments: dict[str, Any]
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


CASES = [
    Case(
        "pandrator_list_sessions",
        {},
        "list_sessions",
        (),
        {"limit": 50, "query": None, "include_trashed": False},
    ),
    Case("pandrator_get_session", {"session_id": SESSION}, "get_session", (SESSION,), {}),
    Case(
        "pandrator_trash_session",
        {"session_id": SESSION, "expected_revision": 0},
        "trash_session",
        (SESSION,),
        {"expected_revision": 0},
    ),
    Case(
        "pandrator_restore_session",
        {"session_id": SESSION, "expected_revision": 0},
        "restore_session",
        (SESSION,),
        {"expected_revision": 0},
    ),
    Case(
        "pandrator_create_session",
        {"name": "Fixture", "idempotency_key": KEY},
        "create_session",
        (),
        {
            "name": "Fixture",
            "workflow_kind": "audiobook",
            "source_language": "auto",
            "target_language": None,
            "workflow_preset": "custom",
            "included_stages": (),
            "multilingual_setup": None,
            "idempotency_key": KEY,
        },
    ),
    Case(
        "pandrator_update_session",
        {**BASE, "name": "Renamed"},
        "update_session",
        (SESSION,),
        {"expected_revision": 4, "changes": {"name": "Renamed"}, "idempotency_key": KEY},
    ),
    Case(
        "pandrator_attach_existing_source",
        {
            "session_id": SESSION,
            "source_asset_id": "source-1",
            "expected_session_revision": 3,
            "idempotency_key": KEY,
        },
        "attach_existing_source",
        (SESSION,),
        {
            "source_asset_id": "source-1",
            "role": "primary",
            "expected_session_revision": 3,
            "idempotency_key": KEY,
        },
    ),
]

UPDATES = [
    ("omitted-setup", {"name": "Renamed"}, {"name": "Renamed"}),
    (
        "named-null-setup",
        {"name": "Renamed", "multilingual_setup": None},
        {"name": "Renamed", "multilingual_setup": None},
    ),
    ("only-null-setup", {"multilingual_setup": None}, {"multilingual_setup": None}),
    (
        "normalized-setup",
        {
            "multilingual_setup": {
                "target_languages": [" JA_jp "],
                "generate_voiceover": True,
                "keep_source_subtitles": False,
                "carry_source_subtitle_settings": True,
            }
        },
        {
            "multilingual_setup": {
                "target_languages": ["ja-jp"],
                "generate_voiceover": True,
                "keep_source_subtitles": False,
                "carry_source_subtitle_settings": True,
            }
        },
    ),
    ("empty-stages", {"included_stages": []}, {"included_stages": []}),
    (
        "ordinary-null-name-omitted",
        {"name": None, "multilingual_setup": None},
        {"multilingual_setup": None},
    ),
]

INVALID = [
    ("get-long-id", "pandrator_get_session", {"session_id": MARKER}, "session_id"),
    ("list-long-query", "pandrator_list_sessions", {"query": MARKER}, "query"),
    (
        "create-short-key",
        "pandrator_create_session",
        {"name": "Fixture", "idempotency_key": "x"},
        "idempotency_key",
    ),
    (
        "create-duplicate-stages",
        "pandrator_create_session",
        {
            "name": "Fixture",
            "idempotency_key": KEY,
            "included_stages": ["transcribe", "transcribe"],
        },
        "included_stages",
    ),
    ("update-no-changes", "pandrator_update_session", {**BASE, "name": None}, None),
    (
        "update-duplicate-stages",
        "pandrator_update_session",
        {**BASE, "included_stages": ["transcribe", "transcribe"]},
        None,
    ),
    (
        "attach-long-source-id",
        "pandrator_attach_existing_source",
        {
            "session_id": SESSION,
            "source_asset_id": MARKER,
            "expected_session_revision": 3,
            "idempotency_key": KEY,
        },
        "source_asset_id",
    ),
]

SETUPS = [
    ("twenty-languages", {"target_languages": [f"en-{index}" for index in range(20)]}, True),
    ("twenty-one-languages", {"target_languages": [f"en-{index}" for index in range(21)]}, False),
    ("normalized-duplicate", {"target_languages": ["en", "EN"]}, False),
    ("strict-voiceover-bool", {"target_languages": ["en"], "generate_voiceover": "true"}, False),
]


def application_fixture(
    root: Path,
) -> tuple[McpRuntime, list[dict[str, Any]], Any, list[dict[str, Any]]]:
    runtime, original_calls, application = fixture_runtime(root)
    calls: list[dict[str, Any]] = []

    def effect(method: str) -> Callable[..., dict[str, Any]]:
        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(
                {
                    "method": method,
                    "args": args,
                    "kwargs": kwargs,
                    "request_id": _REQUEST_ID.get(),
                    "trace_id": _TRACE_ID.get(),
                    "headers": correlation_headers(),
                }
            )
            print(SENTINEL)
            if method == "list_sessions":
                return {"items": [copy.deepcopy(SESSION_PAYLOAD)]}
            return copy.deepcopy(
                ATTACHMENT_PAYLOAD if method == "attach_existing_source" else SESSION_PAYLOAD
            )

        return invoke

    for case in CASES:
        getattr(application, case.method).side_effect = effect(case.method)
    return runtime, original_calls, application, calls


async def invoke(runtime: McpRuntime, protocol: str, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(
        adapter.build_server(runtime), mode=protocol, raise_exceptions=False
    ) as client:
        return await client.call_tool(tool, arguments)


_REQUEST_ID_TEXT = re.compile(
    r'("request_id"\s*:\s*")'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(")'
)


def capture_value(value: Any, *, headers: bool = False) -> Any:
    """Normalize generated identities without reserializing native TextContent JSON."""
    if isinstance(value, BaseModel):
        return {
            "type": type(value).__name__,
            "value": capture_value(value.model_dump(mode="json")),
            "model_fields_set": sorted(value.model_fields_set),
        }
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [capture_value(item) for item in value]}
    if isinstance(value, dict):
        return {
            key: "<request>"
            if key in {"request_id", "X-Request-ID"} and isinstance(item, str) and item
            else "<trace>"
            if (key == "trace_id" or key == "traceparent" and headers)
            and isinstance(item, str)
            and item
            else _REQUEST_ID_TEXT.sub(r"\1<request>\3", item)
            if key == "text" and isinstance(item, str)
            else capture_value(item, headers=key == "headers")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [capture_value(item) for item in value]
    return value


def record_native(
    record_property: Callable[[str, object], None],
    result: Any,
    calls: list[dict[str, Any]],
    *,
    updates: list[dict[str, Any]] | None = None,
) -> None:
    record_property(
        "native_capture",
        json.dumps(
            capture_value(
                {
                    "native_result": result.model_dump(mode="json", by_alias=True),
                    "calls": calls,
                    "updates": updates or [],
                }
            ),
            ensure_ascii=False,
        ),
    )


def assert_success(
    result: Any,
    case: Case,
    original_calls: list[dict[str, Any]],
    application: Any,
    calls: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
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
    assert value["schema_version"] == "1"
    assert PRIVATE not in json.dumps(result.model_dump(mode="json", by_alias=True))
    assert original_calls == []
    assert application.mock_calls == [getattr(call, case.method)(*case.args, **case.kwargs)]
    assert len(calls) == 1
    observed = calls[0]
    assert observed["method"] == case.method
    assert observed["args"] == case.args and observed["kwargs"] == case.kwargs
    assert observed["request_id"] and observed["trace_id"]
    assert value["request_id"] == observed["request_id"] == observed["headers"]["X-Request-ID"]
    traceparent = observed["headers"]["traceparent"]
    assert re.fullmatch(r"00-[a-f0-9]{32}-[a-f0-9]{16}-01", traceparent)
    assert traceparent.split("-")[1] == observed["trace_id"]
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == SENTINEL + "\n"


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case", CASES, ids=[case.tool for case in CASES])
def test_native_session_registration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case: Case,
) -> None:
    runtime, original_calls, application, calls = application_fixture(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, protocol, case.tool, case.arguments))
    record_native(record_property, result, calls)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert_success(result, case, original_calls, application, calls, capsys)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case_id,optional,changes", UPDATES, ids=[item[0] for item in UPDATES])
def test_native_session_update_explicit_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case_id: str,
    optional: dict[str, Any],
    changes: dict[str, Any],
) -> None:
    runtime, original_calls, application, calls = application_fixture(tmp_path)
    original = session_owner.update_session
    updates: list[dict[str, Any]] = []

    def trace(runtime: McpRuntime, arguments: UpdateSessionInput) -> ToolOutcome:
        updates.append(
            {"model_fields_set": sorted(arguments.model_fields_set), "changes": arguments.changes()}
        )
        return original(runtime, arguments)

    monkeypatch.setattr(session_owner, "update_session", trace)
    case = Case(
        "pandrator_update_session",
        {**BASE, **optional},
        "update_session",
        (SESSION,),
        {"expected_revision": 4, "changes": changes, "idempotency_key": KEY},
    )
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, protocol, case.tool, case.arguments))
    record_native(record_property, result, calls, updates=updates)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert_success(result, case, original_calls, application, calls, capsys)
    assert updates == [
        {"model_fields_set": sorted(set(BASE) | set(changes)), "changes": changes}
    ], case_id


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case_id,tool,arguments,field", INVALID, ids=[item[0] for item in INVALID])
def test_native_session_domain_rejection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case_id: str,
    tool: str,
    arguments: dict[str, Any],
    field: str | None,
) -> None:
    runtime, original_calls, application, calls = application_fixture(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, protocol, tool, arguments))
    record_native(record_property, result, calls)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    failure_value(result, tool, field)
    assert MARKER not in json.dumps(result.model_dump(mode="json", by_alias=True)), case_id
    assert original_calls == calls == application.mock_calls == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case_id,setup,accepted", SETUPS, ids=[item[0] for item in SETUPS])
def test_native_session_nested_setup_boundaries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case_id: str,
    setup: dict[str, Any],
    accepted: bool,
) -> None:
    runtime, original_calls, application, calls = application_fixture(tmp_path)
    arguments = {"name": "Fixture", "idempotency_key": KEY, "multilingual_setup": setup}
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, protocol, "pandrator_create_session", arguments))
    record_native(record_property, result, calls)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    if not accepted:
        assert result.is_error, case_id
        assert original_calls == calls == application.mock_calls == []
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""
        return
    expected_setup = MultilingualSetup(target_languages=[f"en-{index}" for index in range(20)])
    case = Case(
        "pandrator_create_session",
        arguments,
        "create_session",
        (),
        {
            "name": "Fixture",
            "workflow_kind": "audiobook",
            "source_language": "auto",
            "target_language": None,
            "workflow_preset": "custom",
            "included_stages": (),
            "multilingual_setup": expected_setup,
            "idempotency_key": KEY,
        },
    )
    assert_success(result, case, original_calls, application, calls, capsys)
    actual_setup = calls[0]["kwargs"]["multilingual_setup"]
    assert isinstance(actual_setup, MultilingualSetup)
    assert actual_setup.model_dump(mode="json") == {
        "target_languages": [f"en-{index}" for index in range(20)],
        "generate_voiceover": False,
        "keep_source_subtitles": True,
        "carry_source_subtitle_settings": False,
    }
