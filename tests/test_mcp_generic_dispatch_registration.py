"""Generic dispatch registrations preserve native calls, projections and guards."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from tests.test_mcp_media_edit_registration import fixture_runtime as base_fixture_runtime

SENTINEL = "generic dispatch registration fixture stdout"
MARKER = "private-generic-fixture-" * 8
MANIFEST_HASH = "fb6cc309f434761518f479c62ea9dae872aa8793d7fe12c149c9af30d5c41494"


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    arguments: dict[str, Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


CASES = [
    Case(
        "pandrator_get_dispatch_preview",
        "get_dispatch_preview",
        {"run_id": "run-1"},
        (),
        {"run_id": "run-1", "batch_ordinal": None, "offset": 0, "limit": 20},
    ),
    Case(
        "pandrator_terminate_dispatch_run",
        "terminate_dispatch_run",
        {
            "run_id": "run-1",
            "expected_status": "ready",
            "action": "cancelled",
            "reason": "Fixture",
            "idempotency_key": "registration:generic:1",
        },
        (),
        {
            "run_id": "run-1",
            "expected_status": "ready",
            "action": "cancelled",
            "replacement_run_id": None,
            "reason": "Fixture",
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_create_dispatch_run",
        "create_dispatch_run",
        {
            "session_id": "session-1",
            "kind": "correction",
            "idempotency_key": "registration:generic:1",
        },
        ("session-1",),
        {
            "kind": "correction",
            "source_artifact_id": None,
            "source_language": None,
            "target_language": None,
            "instructions": "",
            "char_limit": 6000,
            "max_segments_per_batch": 40,
            "no_remove_subtitles": False,
            "correction_style": "publishable",
            "context_before": 8,
            "context_after": 2,
            "timing_context_mode": "full",
            "substantial_gap_ms": 2000,
            "glossary": {},
            "execution_mode": "serial",
            "max_parallel_batches": 1,
            "context_capsule": {
                "overview": "",
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_list_dispatch_runs",
        "list_dispatch_runs",
        {"session_id": "session-1"},
        ("session-1",),
        {"limit": 50},
    ),
    Case(
        "pandrator_get_dispatch_run",
        "get_dispatch_run",
        {"run_id": "run-1"},
        ("run-1",),
        {},
    ),
    Case(
        "pandrator_inspect_dispatch_split_boundaries",
        "inspect_dispatch_split_boundaries",
        {"batch_id": "batch-1", "lease_token": "fixture-lease", "cue_id": 1},
        (),
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "cue_id": 1,
            "offset": 0,
            "limit": 30,
        },
    ),
    Case(
        "pandrator_claim_dispatch_batch",
        "claim_dispatch_batch",
        {"run_id": "run-1", "idempotency_key": "registration:generic:1"},
        ("run-1",),
        {"lease_seconds": 900, "idempotency_key": "registration:generic:1"},
    ),
    Case(
        "pandrator_renew_dispatch_batch",
        "renew_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:generic:1",
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "lease_seconds": 900,
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_release_dispatch_batch",
        "release_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:generic:1",
        },
        ("batch-1",),
        {"lease_token": "fixture-lease", "idempotency_key": "registration:generic:1"},
    ),
    Case(
        "pandrator_submit_dispatch_batch",
        "submit_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:generic:1",
            "result": {"kind": "correction"},
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "result": {"kind": "correction", "operations": [], "uncertainties": []},
            "response_text": None,
            "context_delta": {
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "idempotency_key": "registration:generic:1",
        },
    ),
]

EXTRAS = [
    Case(
        "pandrator_get_dispatch_preview",
        "get_dispatch_preview",
        {"run_id": "run-1", "batch_ordinal": 2, "offset": 3, "limit": 5},
        (),
        {"run_id": "run-1", "batch_ordinal": 2, "offset": 3, "limit": 5},
    ),
    Case(
        "pandrator_terminate_dispatch_run",
        "terminate_dispatch_run",
        {
            "run_id": "run-1",
            "expected_status": "ready",
            "action": "superseded",
            "reason": "Fixture",
            "idempotency_key": "registration:generic:1",
            "replacement_run_id": "run-2",
        },
        (),
        {
            "run_id": "run-1",
            "expected_status": "ready",
            "action": "superseded",
            "replacement_run_id": "run-2",
            "reason": "Fixture",
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_create_dispatch_run",
        "create_dispatch_run",
        {
            "session_id": "session-1",
            "kind": "translation",
            "idempotency_key": "registration:generic:1",
            "source_language": "en",
            "target_language": "fr",
            "glossary": {"hello": "bonjour"},
            "execution_mode": "parallel",
            "max_parallel_batches": 3,
            "context_capsule": {"entities": {"Alice": "narrator"}},
        },
        ("session-1",),
        {
            "kind": "translation",
            "source_artifact_id": None,
            "source_language": "en",
            "target_language": "fr",
            "instructions": "",
            "char_limit": 6000,
            "max_segments_per_batch": 40,
            "no_remove_subtitles": False,
            "correction_style": "publishable",
            "context_before": 8,
            "context_after": 2,
            "timing_context_mode": "full",
            "substantial_gap_ms": 2000,
            "glossary": {"hello": "bonjour"},
            "execution_mode": "parallel",
            "max_parallel_batches": 3,
            "context_capsule": {
                "overview": "",
                "terminology": {},
                "entities": {"Alice": "narrator"},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_claim_dispatch_batch",
        "claim_dispatch_batch",
        {
            "run_id": "run-1",
            "idempotency_key": "registration:generic:1",
            "packet_format": "compact",
            "known_manifest_hash": "fb6cc309f434761518f479c62ea9dae872aa8793d7fe12c149c9af30d5c41494",
        },
        ("run-1",),
        {"lease_seconds": 900, "idempotency_key": "registration:generic:1"},
    ),
    Case(
        "pandrator_claim_dispatch_batch",
        "claim_dispatch_batch",
        {
            "run_id": "run-1",
            "idempotency_key": "registration:generic:1",
            "packet_format": "compact",
            "response_mode": "structured",
        },
        ("run-1",),
        {"lease_seconds": 900, "idempotency_key": "registration:generic:1"},
    ),
    Case(
        "pandrator_submit_dispatch_batch",
        "submit_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:generic:1",
            "result": {"kind": "correction", "edits": [{"cue_id": 1, "text": "Edited"}]},
            "context_delta": {"entities": {"Alice": "narrator"}},
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "result": {
                "kind": "correction",
                "operations": [
                    {
                        "split_boundary_ids": [],
                        "starts_new_turn": False,
                        "action": "edit",
                        "cue_ids": [1],
                        "texts": ["Edited"],
                        "speakers": [],
                    }
                ],
                "uncertainties": [],
            },
            "response_text": None,
            "context_delta": {
                "terminology": {},
                "entities": {"Alice": "narrator"},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_submit_dispatch_batch",
        "submit_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:generic:1",
            "result": {"kind": "translation", "items": [{"cue_id": 1, "text": "Bonjour"}]},
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "result": {
                "kind": "translation",
                "translations": [
                    {"cue_id": 1, "cue_ids": None, "text": "Bonjour", "speaker": None}
                ],
                "glossary_updates": {},
            },
            "response_text": None,
            "context_delta": {
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "idempotency_key": "registration:generic:1",
        },
    ),
    Case(
        "pandrator_submit_dispatch_batch",
        "submit_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:generic:1",
            "result": None,
            "response_text": '{"kind":"correction","operations":[],"uncertainties":[]}',
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "result": None,
            "response_text": '{"kind":"correction","operations":[],"uncertainties":[]}',
            "context_delta": {
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "idempotency_key": "registration:generic:1",
        },
    ),
]

PAYLOAD = {
    "id": "run-1",
    "run_id": "run-1",
    "batch_id": "batch-1",
    "session_id": "session-1",
    "status": "ready",
    "accepted": True,
    "lease_token": "fixture-lease",
    "lease_expires_at": "2030-01-01T00:00:00+00:00",
    "items": [{"id": "run-1", "status": "ready", "session_id": "session-1"}],
    "task": {
        "session_id": "session-1",
        "kind": "correction",
        "source_artifact_id": "artifact-1",
        "source_content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "instructions": "Fixture instructions",
        "result_contract": {"kind": "correction", "version": 1},
    },
    "batch": {
        "id_namespace": "source_revision_cue",
        "source_revision_id": "revision-1",
        "cue_count": 1,
        "valid_cue_ids": [1],
        "cues": [
            {
                "cue_id": 1,
                "text": "Fixture",
                "speaker": "Alice",
                "timing": {"start_ms": 0, "end_ms": 1000},
            }
        ],
    },
    "delegation": {
        "execution_mode": "serial",
        "max_parallel_batches": 1,
        "context_capsule": {
            "overview": "",
            "terminology": {},
            "entities": {},
            "style_rules": [],
            "decisions": [],
            "notes": [],
        },
    },
}

TIMEOUTS = [CASES[2], CASES[9]]
SDK_INVALID = [
    replace(CASES[5], arguments={**CASES[5].arguments, **{"cue_id": True}}),
    replace(CASES[5], arguments={**CASES[5].arguments, **{"cue_id": "1"}}),
    replace(CASES[5], arguments={**CASES[5].arguments, **{"cue_id": 1.0}}),
    replace(
        CASES[6],
        arguments={
            **CASES[6].arguments,
            **{
                "known_manifest_hash": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            },
        },
    ),
]

POLICY_INVALID = [
    replace(
        CASES[2],
        arguments={**CASES[2].arguments, **{"execution_mode": "serial", "max_parallel_batches": 2}},
    ),
    replace(
        CASES[2],
        arguments={
            **CASES[2].arguments,
            **{"execution_mode": "parallel", "max_parallel_batches": 1},
        },
    ),
]


def fixture_runtime(
    root: Path, failure: str | None = None
) -> tuple[McpRuntime, list[dict[str, Any]], Any]:
    runtime, _, application = base_fixture_runtime(root)
    calls: list[dict[str, Any]] = []

    def effect(method: str):
        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
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
            return copy.deepcopy(PAYLOAD)

        return invoke

    for case in CASES:
        getattr(application, case.method).side_effect = effect(case.method)
    return runtime, calls, application


def text_block(result: Any) -> TextContent:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block


def failure_value(result: Any, name: str) -> dict[str, Any]:
    assert result.is_error
    block = text_block(result)
    prefix = f"Error executing tool {name}: "
    assert block.text.startswith(prefix)
    value = json.loads(block.text[len(prefix) :])
    assert isinstance(value["request_id"], str) and value["request_id"]
    return value


def envelope(result: Any, *, structured: bool = False) -> dict[str, Any]:
    assert not result.is_error
    text = json.loads(text_block(result).text)
    value = result.structured_content
    assert set(value) == {
        "schema_version",
        "request_id",
        "result",
        "work",
        "warnings",
        "next_actions",
    }
    assert value["schema_version"] == "1"
    assert isinstance(value["request_id"], str) and value["request_id"]
    assert value["work"] is None
    assert value["warnings"] == []
    assert isinstance(value["next_actions"], list)
    if structured:
        assert text == {
            "schema_version": "1",
            "request_id": value["request_id"],
            "data": "structuredContent",
            "run_id": value["result"]["run_id"],
            "batch_id": value["result"]["batch_id"],
            "status": value["result"]["status"],
            "manifest_hash": value["result"]["manifest_hash"],
        }
    else:
        assert text == value
    return value


async def invoke_case(root: Path, case: Case, *, mode: str = "valid") -> dict[str, Any]:
    runtime, calls, application = fixture_runtime(
        root, failure=case.method if mode == "timeout" else None
    )
    arguments = copy.deepcopy(case.arguments)
    identifier = next(key for key in ("session_id", "run_id", "batch_id") if key in arguments)
    if mode == "invalid":
        arguments[identifier] = MARKER
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        result = await client.call_tool(case.name, arguments)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    if mode in {"invalid", "sdk_invalid", "policy_invalid"}:
        assert calls == application.mock_calls == []
        assert result.is_error
        block = text_block(result)
        if mode == "sdk_invalid":
            value = None
        else:
            value = failure_value(result, case.name)
            assert value["code"] == "validation_error"
            assert value["retryable"] is False
            assert value["next_actions"] == []
            errors = value["details"]["errors"]
            assert errors
            if mode == "invalid":
                assert any(error["loc"] == [identifier] for error in errors)
            assert all(key not in error for error in errors for key in ("input", "ctx", "url"))
            assert MARKER not in json.dumps(value)
        assert isinstance(block.text, str) and block.text
    else:
        assert len(calls) == len(application.mock_calls) == 1
        assert [
            {key: item for key, item in call.items() if key != "headers"} for call in calls
        ] == [{"method": case.method, "args": list(case.args), "kwargs": case.kwargs}]
        assert list(calls[0]["kwargs"]) == list(case.kwargs)
        if mode == "timeout":
            value = failure_value(result, case.name)
            assert value["code"] == "application_response_timeout"
            assert value["message"] == "Fixture response timeout."
            assert value["details"] == {"operation_outcome": "unknown"}
            assert value["retryable"] is True
            assert value["next_actions"] == []
        else:
            structured = arguments.get("response_mode") == "structured"
            value = envelope(result, structured=structured)
            if arguments.get("known_manifest_hash") == MANIFEST_HASH:
                assert value["result"]["packet_format"] == "compact-v1"
                assert value["result"]["manifest_hash"] == MANIFEST_HASH
                assert "manifest" not in value["result"]
            if structured:
                assert value["result"]["packet_format"] == "compact-v1"
                assert value["result"]["manifest_hash"] == MANIFEST_HASH
                assert "manifest" in value["result"]
        for call in calls:
            assert value["request_id"] == call["headers"]["X-Request-ID"]
            assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", call["headers"]["traceparent"])
    return {
        "name": case.name,
        "arguments": arguments,
        "mode": mode,
        "calls": calls,
        "envelope": value,
        "native_result": result.model_dump(mode="json", by_alias=True),
    }


def assert_stdio(capsys: pytest.CaptureFixture[str], count: int) -> None:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count(SENTINEL) == count


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_generic_dispatch_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, 1)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_generic_dispatch_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, mode="invalid"))
    assert_stdio(capsys, 0)


@pytest.mark.parametrize("case", TIMEOUTS, ids=lambda case: case.name)
def test_native_generic_dispatch_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, mode="timeout"))
    assert_stdio(capsys, 1)


@pytest.mark.parametrize("case", EXTRAS, ids=lambda case: case.name)
def test_native_generic_dispatch_extra(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, 1)


@pytest.mark.parametrize("case", SDK_INVALID, ids=lambda case: case.name)
def test_native_generic_dispatch_sdk_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, mode="sdk_invalid"))
    assert_stdio(capsys, 0)


@pytest.mark.parametrize("case", POLICY_INVALID, ids=lambda case: case.name)
def test_native_generic_dispatch_policy_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, mode="policy_invalid"))
    assert_stdio(capsys, 0)


async def tool_metadata(root: Path) -> dict[str, Any]:
    runtime, calls, application = fixture_runtime(root)
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        metadata = (await client.list_tools()).model_dump(mode="json", by_alias=True)
    assert calls == application.mock_calls == []
    assert len(metadata["tools"]) == 162
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in metadata["tools"])
    selected = [tool for tool in metadata["tools"] if tool["name"] in {case.name for case in CASES}]
    assert [tool["name"] for tool in selected] == [case.name for case in CASES]
    for tool in selected:
        read_only = tool["name"] in {CASES[index].name for index in (0, 3, 4, 5)}
        assert tool["annotations"]["readOnlyHint"] is read_only
        assert tool["annotations"]["openWorldHint"] is False
        if not read_only:
            assert tool["annotations"]["destructiveHint"] is False
            assert tool["annotations"]["idempotentHint"] is True
    create = next(tool for tool in selected if tool["name"] == CASES[2].name)
    assert create["inputSchema"]["allOf"] == [
        {
            "if": {"properties": {"execution_mode": {"const": "serial"}}},
            "then": {"properties": {"max_parallel_batches": {"const": 1}}},
        },
        {
            "if": {
                "properties": {"execution_mode": {"const": "parallel"}},
                "required": ["execution_mode"],
            },
            "then": {
                "properties": {"max_parallel_batches": {"minimum": 2, "maximum": 8}},
                "required": ["max_parallel_batches"],
            },
        },
    ]
    return metadata


def test_native_generic_dispatch_metadata(tmp_path: Path) -> None:
    asyncio.run(tool_metadata(tmp_path))


_REQUEST_ID_TEXT = re.compile(
    r'("request_id"\s*:\s*")'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(")'
)


def capture_value(value: Any, *, headers: bool = False) -> Any:
    """Normalize only generated identities, preserving all other native text bytes."""
    if isinstance(value, dict):
        return {
            key: "<request>"
            if key in {"request_id", "X-Request-ID"}
            else "<trace>"
            if key == "traceparent" and headers
            else _REQUEST_ID_TEXT.sub(r"\1<request>\3", item)
            if key == "text" and isinstance(item, str)
            else capture_value(item, headers=key == "headers")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [capture_value(item) for item in value]
    return value


async def capture_contract(root: Path) -> dict[str, Any]:
    metadata = await tool_metadata(root / "metadata")
    groups: dict[str, list[dict[str, Any]]] = {}
    for name, cases, mode in (
        ("records", CASES, "valid"),
        ("invalid", CASES, "invalid"),
        ("timeouts", TIMEOUTS, "timeout"),
        ("extras", EXTRAS, "valid"),
        ("sdk_invalid", SDK_INVALID, "sdk_invalid"),
        ("policy_invalid", POLICY_INVALID, "policy_invalid"),
    ):
        groups[name] = [
            await invoke_case(root / f"{name}-{index}", case, mode=mode)
            for index, case in enumerate(cases)
        ]
    return {"metadata": metadata, **capture_value(groups)}
