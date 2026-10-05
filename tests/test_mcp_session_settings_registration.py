"""Session-settings native registrations preserve payloads and guarded boundaries."""

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

import pandrator_mcp.registrations.session_settings as settings_owner
import pandrator_mcp.server as adapter
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from tests.test_mcp_generic_dispatch_validation import failure_value
from tests.test_mcp_media_edit_registration import fixture_runtime
from tests.test_mcp_session_registration import capture_value

registration_owner = settings_owner
PROTOCOLS = ["2026-07-28", "legacy"]
SESSION = "session-1"
KEY = "settings:registration:1"
SENTINEL = "session settings registration fixture stdout sentinel"
PRIVATE = "private-settings-backend-fixture"
MARKER = "private-settings-domain-fixture"
BASE = {"session_id": SESSION, "section": "tts"}
WRITE_BASE = {**BASE, "expected_revision": 0, "idempotency_key": KEY}
REPRESENTATIVE = {"mode": "manual"}
NESTED = {
    "enabled": True,
    "choice": None,
    "nested": {"note": "null", "options": [None, False, 1, '{"x":2}']},
}
BOUNDARY = {"label": "é" * 65530}
OVER_BOUNDARY = {"label": "é" * 65531}
assert (
    len(
        json.dumps(BOUNDARY, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )
    == 131072
)
assert (
    len(
        json.dumps(
            OVER_BOUNDARY, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )
    == 131074
)
FORBIDDEN = {"nested": {"model-api-key": MARKER}}
DEPTH_ELEVEN: Any = 0
for _depth in range(11):
    DEPTH_ELEVEN = {"child": DEPTH_ELEVEN}

SAFE_OVERRIDE = {"mode": "stored", "nested": {"keep": None}}
SUBTITLE_FIELDS = {
    "subtitle_settings_provenance": {"cps": "automatic"},
    "subtitle_profiles": {"ja": {"cps": 7}},
    "subtitle_automatic_profiles": {"ja": True},
    "subtitle_profile_scope": "target",
}
WRITES = [
    ("pandrator_update_session_settings", "update_session_settings", "UpdateSessionSettingsInput"),
    ("pandrator_patch_session_settings", "patch_session_settings", "PatchSessionSettingsInput"),
]


@dataclass(frozen=True)
class Case:
    case_id: str
    tool: str
    method: str
    model: str
    arguments: dict[str, Any]
    error_field: str | None = None


CASES = [
    Case(
        "get-representative",
        "pandrator_get_session_settings",
        "get_session_settings",
        "GetSessionSettingsInput",
        BASE,
    ),
    *[
        Case(
            method + "-representative", tool, method, model, {**WRITE_BASE, "value": REPRESENTATIVE}
        )
        for tool, method, model in WRITES
    ],
    *[
        Case(method + "-empty", tool, method, model, {**WRITE_BASE, "value": {}})
        for tool, method, model in WRITES
    ],
    *[
        Case(method + "-nested", tool, method, model, {**WRITE_BASE, "value": NESTED})
        for tool, method, model in WRITES
    ],
    *[
        Case(method + "-utf8-boundary", tool, method, model, {**WRITE_BASE, "value": BOUNDARY})
        for tool, method, model in WRITES
    ],
    *[
        Case(
            method + "-utf8-over-boundary",
            tool,
            method,
            model,
            {**WRITE_BASE, "value": OVER_BOUNDARY},
            "value",
        )
        for tool, method, model in WRITES
    ],
    *[
        Case(
            method + "-forbidden-key",
            tool,
            method,
            model,
            {**WRITE_BASE, "value": FORBIDDEN},
            "value",
        )
        for tool, method, model in WRITES
    ],
    *[
        Case(
            method + "-depth-eleven",
            tool,
            method,
            model,
            {**WRITE_BASE, "value": DEPTH_ELEVEN},
            "value",
        )
        for tool, method, model in WRITES
    ],
    *[
        Case(
            method + "-short-key",
            tool,
            method,
            model,
            {**WRITE_BASE, "value": REPRESENTATIVE, "idempotency_key": "x"},
            "idempotency_key",
        )
        for tool, method, model in WRITES
    ],
    Case(
        "get-subtitles",
        "pandrator_get_session_settings",
        "get_session_settings",
        "GetSessionSettingsInput",
        {"session_id": SESSION, "section": "subtitles"},
    ),
]
assert len(CASES) == 18


def observe_context() -> dict[str, Any]:
    return {
        "request_id": _REQUEST_ID.get(),
        "trace_id": _TRACE_ID.get(),
        "headers": correlation_headers(),
    }


def backend_payload(section: str) -> dict[str, Any]:
    return {
        "section": section,
        "override": {
            "mode": "stored",
            "password": PRIVATE,
            "nested": {"keep": None, "api_key": PRIVATE},
        },
        "effective": {"enabled": True, "access_token": PRIVATE},
        "session_context": {"target_language": "ja", "secret": PRIVATE},
        "revision": 3,
        "global_revision": 7,
        "private_fixture": PRIVATE,
        **(copy.deepcopy(SUBTITLE_FIELDS) if section == "subtitles" else {}),
    }


def application_fixture(
    root: Path,
) -> tuple[McpRuntime, list[dict[str, Any]], Any, list[dict[str, Any]]]:
    runtime, original_calls, application = fixture_runtime(root)
    calls: list[dict[str, Any]] = []

    def effect(method: str) -> Callable[..., dict[str, Any]]:
        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append({"method": method, "args": args, "kwargs": kwargs, **observe_context()})
            print(SENTINEL)
            section = args[1] if method == "get_session_settings" else kwargs["section"]
            return backend_payload(section)

        return invoke

    for method in ["get_session_settings", "update_session_settings", "patch_session_settings"]:
        getattr(application, method).side_effect = effect(method)
    return runtime, original_calls, application, calls


def trace_handlers(monkeypatch: pytest.MonkeyPatch, observations: list[dict[str, Any]]) -> None:
    def trace_for(
        name: str, original: Callable[..., Any]
    ) -> Callable[[McpRuntime, BaseModel], Any]:
        def trace(runtime: McpRuntime, arguments: BaseModel) -> Any:
            observations.append(
                {
                    "handler": name,
                    "model": type(arguments).__name__,
                    "model_json": arguments.model_dump(mode="json"),
                    "model_fields_set": sorted(arguments.model_fields_set),
                    **observe_context(),
                }
            )
            return original(runtime, arguments)

        return trace

    for name in ["get_session_settings", "update_session_settings", "patch_session_settings"]:
        original = getattr(registration_owner, name)
        monkeypatch.setattr(registration_owner, name, trace_for(name, original))


async def invoke(runtime: McpRuntime, protocol: str, case: Case) -> Any:
    async with Client(
        adapter.build_server(runtime), mode=protocol, raise_exceptions=False
    ) as client:
        return await client.call_tool(case.tool, case.arguments)


def record_native(
    record_property: Callable[[str, object], None],
    result: Any,
    calls: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    record_property(
        "native_capture",
        json.dumps(
            capture_value(
                {
                    "native_result": result.model_dump(mode="json", by_alias=True),
                    "calls": calls,
                    "handlers": observations,
                }
            ),
            ensure_ascii=False,
        ),
    )


def assert_context(observed: dict[str, Any], request_id: str) -> None:
    assert observed["request_id"] and observed["trace_id"]
    assert request_id == observed["request_id"] == observed["headers"]["X-Request-ID"]
    traceparent = observed["headers"]["traceparent"]
    assert re.fullmatch(r"00-[a-f0-9]{32}-[a-f0-9]{16}-01", traceparent)
    assert traceparent.split("-")[1] == observed["trace_id"]


def expected_projection(case: Case) -> dict[str, Any]:
    section = case.arguments["section"]
    safe = {
        "schema_version": "1",
        "session_id": SESSION,
        "section": section,
        "override": SAFE_OVERRIDE,
        "revision": 3,
    }
    if case.method == "get_session_settings":
        safe.update(
            {
                "effective": {"enabled": True},
                "session_context": {"target_language": "ja"},
                "global_revision": 7,
                **(SUBTITLE_FIELDS if section == "subtitles" else {}),
            }
        )
    return safe


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_native_session_settings_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case: Case,
) -> None:
    runtime, original_calls, application, calls = application_fixture(tmp_path)
    observations: list[dict[str, Any]] = []
    trace_handlers(monkeypatch, observations)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, protocol, case))
    record_native(record_property, result, calls, observations)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert original_calls == []
    if case.error_field is not None:
        failure_value(result, case.tool, case.error_field)
        assert MARKER not in json.dumps(result.model_dump(mode="json", by_alias=True))
        assert application.mock_calls == calls == observations == []
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""
        return
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
    assert value["result"] == expected_projection(case)
    assert PRIVATE not in json.dumps(result.model_dump(mode="json", by_alias=True))
    if case.method == "get_session_settings":
        expected_args = (SESSION, case.arguments["section"])
        expected_kwargs: dict[str, Any] = {}
        fields = {"session_id", "section"}
    else:
        expected_args = (SESSION,)
        expected_kwargs = {
            "section": case.arguments["section"],
            "value": case.arguments["value"],
            "expected_revision": 0,
            "idempotency_key": KEY,
        }
        fields = {"session_id", "section", "value", "expected_revision", "idempotency_key"}
    assert application.mock_calls == [getattr(call, case.method)(*expected_args, **expected_kwargs)]
    assert len(calls) == len(observations) == 1
    observed = calls[0]
    assert observed["method"] == case.method
    assert observed["args"] == expected_args
    assert observed["kwargs"] == expected_kwargs
    typed = observations[0]
    assert typed["handler"] == case.method and typed["model"] == case.model
    assert typed["model_json"] == case.arguments
    assert typed["model_fields_set"] == sorted(fields)
    assert_context(observed, value["request_id"])
    assert_context(typed, value["request_id"])
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == SENTINEL + "\n"
