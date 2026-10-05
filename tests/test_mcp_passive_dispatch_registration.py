"""Native passive-dispatch registrations preserve calls and guarded transport."""

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

SENTINEL = "passive dispatch registration fixture stdout"
MARKER = "private-passive-fixture-" * 8


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    arguments: dict[str, Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


CASES = [
    Case(
        "pandrator_create_source_cleaning_dispatch_run",
        "create_source_cleaning_dispatch_run",
        {"session_id": "session-1", "idempotency_key": "registration:dispatch:1"},
        ("session-1",),
        {
            "source_artifact_id": None,
            "instructions": "",
            "evidence_limit": 500,
            "remove_footnotes": None,
            "filter_citations": None,
            "pdf_ocr_mode": None,
            "pdf_ocr_language": None,
            "pdf_ocr_dpi": None,
            "pdf_remove_toc": None,
            "pdf_remove_repeated_marginals": None,
            "idempotency_key": "registration:dispatch:1",
        },
    ),
    Case(
        "pandrator_list_source_cleaning_dispatch_runs",
        "list_source_cleaning_dispatch_runs",
        {"session_id": "session-1"},
        ("session-1",),
        {"limit": 50},
    ),
    Case(
        "pandrator_get_source_cleaning_dispatch_run",
        "get_source_cleaning_dispatch_run",
        {"run_id": "run-1"},
        ("run-1",),
        {},
    ),
    Case(
        "pandrator_claim_source_cleaning_dispatch_batch",
        "claim_source_cleaning_dispatch_batch",
        {"run_id": "run-1", "idempotency_key": "registration:dispatch:1"},
        ("run-1",),
        {"lease_seconds": 900, "idempotency_key": "registration:dispatch:1"},
    ),
    Case(
        "pandrator_renew_source_cleaning_dispatch_batch",
        "renew_source_cleaning_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "lease_seconds": 900,
            "idempotency_key": "registration:dispatch:1",
        },
    ),
    Case(
        "pandrator_release_source_cleaning_dispatch_batch",
        "release_source_cleaning_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
        },
        ("batch-1",),
        {"lease_token": "fixture-lease", "idempotency_key": "registration:dispatch:1"},
    ),
    Case(
        "pandrator_inspect_source_cleaning_dispatch_extraction",
        "inspect_source_cleaning_dispatch_extraction",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
            "action": "batch",
            "arguments": {},
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "action": "batch",
            "arguments": {},
            "view": "working",
            "idempotency_key": "registration:dispatch:1",
        },
    ),
    Case(
        "pandrator_submit_source_cleaning_dispatch_batch",
        "submit_source_cleaning_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
            "result": {"kind": "source_cleaning", "phase": "metadata"},
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "result": {
                "kind": "source_cleaning",
                "phase": "metadata",
                "decisions": [],
                "operations": [],
                "summary": "",
                "confidence": 0.0,
            },
            "idempotency_key": "registration:dispatch:1",
        },
    ),
    Case(
        "pandrator_create_speech_optimization_dispatch_run",
        "create_speech_optimization_dispatch_run",
        {"session_id": "session-1", "idempotency_key": "registration:dispatch:1"},
        ("session-1",),
        {
            "source_artifact_id": None,
            "language": None,
            "voice_language": None,
            "tts_service": None,
            "instructions": "",
            "char_limit": 20000,
            "max_units_per_batch": 100,
            "context_before": 4,
            "context_after": 2,
            "include_timing": True,
            "annotation_mode": "off",
            "annotation_only": False,
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
            "idempotency_key": "registration:dispatch:1",
        },
    ),
    Case(
        "pandrator_list_speech_optimization_dispatch_runs",
        "list_speech_optimization_dispatch_runs",
        {"session_id": "session-1"},
        ("session-1",),
        {"limit": 50},
    ),
    Case(
        "pandrator_get_speech_optimization_dispatch_run",
        "get_speech_optimization_dispatch_run",
        {"run_id": "run-1"},
        ("run-1",),
        {},
    ),
    Case(
        "pandrator_claim_speech_optimization_dispatch_batch",
        "claim_speech_optimization_dispatch_batch",
        {"run_id": "run-1", "idempotency_key": "registration:dispatch:1"},
        ("run-1",),
        {"lease_seconds": 900, "idempotency_key": "registration:dispatch:1"},
    ),
    Case(
        "pandrator_renew_speech_optimization_dispatch_batch",
        "renew_speech_optimization_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "lease_seconds": 900,
            "idempotency_key": "registration:dispatch:1",
        },
    ),
    Case(
        "pandrator_release_speech_optimization_dispatch_batch",
        "release_speech_optimization_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
        },
        ("batch-1",),
        {"lease_token": "fixture-lease", "idempotency_key": "registration:dispatch:1"},
    ),
    Case(
        "pandrator_submit_speech_optimization_dispatch_batch",
        "submit_speech_optimization_dispatch_batch",
        {
            "batch_id": "batch-1",
            "lease_token": "fixture-lease",
            "idempotency_key": "registration:dispatch:1",
            "result": {"kind": "speech_optimization", "items": [{"unit_id": 1, "text": "Fixture"}]},
        },
        ("batch-1",),
        {
            "lease_token": "fixture-lease",
            "result": {
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Fixture", "speech_xml": None}],
            },
            "context_delta": {
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "character_proposals": [],
            "idempotency_key": "registration:dispatch:1",
        },
    ),
]


def extra(case: Case, arguments: dict[str, Any], kwargs: dict[str, Any]) -> Case:
    return replace(
        case,
        arguments={**copy.deepcopy(case.arguments), **arguments},
        kwargs={**copy.deepcopy(case.kwargs), **kwargs},
    )


EXTRAS = [
    extra(
        CASES[0],
        {
            "instructions": "Fixture",
            "pdf_ocr_mode": "force",
            "pdf_ocr_language": "pol",
            "pdf_ocr_dpi": 300,
            "remove_footnotes": True,
        },
        {
            "instructions": "Fixture",
            "pdf_ocr_mode": "force",
            "pdf_ocr_language": "pol",
            "pdf_ocr_dpi": 300,
            "remove_footnotes": True,
        },
    ),
    extra(
        CASES[8],
        {
            "execution_mode": "parallel",
            "max_parallel_batches": 3,
            "context_capsule": {"entities": {"Alice": "narrator"}},
        },
        {
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
        },
    ),
    extra(
        CASES[6],
        {"action": "search", "arguments": {"query": "Fixture", "limit": 5}, "view": "baseline"},
        {"action": "search", "arguments": {"query": "Fixture", "limit": 5}, "view": "baseline"},
    ),
    extra(
        CASES[14],
        {
            "context_delta": {"entities": {"Alice": "narrator"}},
            "character_proposals": [{"character_key": "alice", "name": "Alice"}],
        },
        {
            "context_delta": {
                "terminology": {},
                "entities": {"Alice": "narrator"},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            "character_proposals": [{"character_key": "alice", "name": "Alice"}],
        },
    ),
]
TIMEOUTS = [CASES[2], CASES[14]]


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
            return copy.deepcopy(
                {
                    "id": "run-1",
                    "run_id": "run-1",
                    "batch_id": "batch-1",
                    "session_id": "session-1",
                    "status": "ready",
                    "accepted": True,
                    "lease_token": "fixture-lease",
                    "lease_expires_at": "2030-01-01T00:00:00Z",
                    "items": [{"id": "run-1", "status": "ready", "session_id": "session-1"}],
                    "task": {
                        "kind": "source_cleaning"
                        if "source_cleaning" in method
                        else "speech_optimization",
                        "instructions": "Fixture instructions",
                    },
                    "batch": {
                        "units": [{"unit_id": 1, "text": "Fixture"}],
                        "valid_unit_ids": [1],
                    },
                }
            )

        return invoke

    for case in CASES:
        getattr(application, case.method).side_effect = effect(case.method)
    return runtime, calls, application


def failure_value(result: Any, tool_name: str) -> dict[str, Any]:
    assert result.is_error
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    prefix = f"Error executing tool {tool_name}: "
    assert block.text.startswith(prefix)
    value = json.loads(block.text[len(prefix) :])
    assert isinstance(value["request_id"], str) and value["request_id"]
    return value


def envelope(result: Any) -> dict[str, Any]:
    assert not result.is_error
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    value = json.loads(block.text)
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
    assert isinstance(value["request_id"], str) and value["request_id"]
    assert value["work"] is None
    assert value["warnings"] == []
    assert isinstance(value["next_actions"], list)
    return value


async def invoke_case(
    root: Path, case: Case, *, invalid: bool = False, timeout: bool = False
) -> dict[str, Any]:
    runtime, calls, application = fixture_runtime(root, failure=case.method if timeout else None)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    arguments = copy.deepcopy(case.arguments)
    field = next(key for key in ("session_id", "run_id", "batch_id") if key in arguments)
    if invalid:
        arguments[field] = MARKER
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        result = await client.call_tool(case.name, arguments)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    if invalid:
        assert calls == application.mock_calls == []
        value = failure_value(result, case.name)
        assert value["code"] == "validation_error"
        assert value["retryable"] is False
        assert value["next_actions"] == []
        errors = value["details"]["errors"]
        assert errors and any(error["loc"] == [field] for error in errors)
        assert all(key not in error for error in errors for key in ("input", "ctx", "url"))
        assert MARKER not in json.dumps(value)
    else:
        assert len(calls) == len(application.mock_calls) == 1
        assert [
            {key: item for key, item in call.items() if key != "headers"} for call in calls
        ] == [{"method": case.method, "args": list(case.args), "kwargs": case.kwargs}]
        assert list(calls[0]["kwargs"]) == list(case.kwargs)
        if timeout:
            value = failure_value(result, case.name)
            assert value["code"] == "application_response_timeout"
            assert value["message"] == "Fixture response timeout."
            assert value["details"] == {"operation_outcome": "unknown"}
            assert value["retryable"] is True
            assert value["next_actions"] == []
        else:
            value = envelope(result)
        for call in calls:
            assert call["headers"]["X-Request-ID"] == value["request_id"]
            assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", call["headers"]["traceparent"])
    return {
        "name": case.name,
        "arguments": arguments,
        "invalid": invalid,
        "timeout": timeout,
        "calls": calls,
        "envelope": value,
        "native_result": result.model_dump(mode="json", by_alias=True),
    }


def assert_stdio(capsys: pytest.CaptureFixture[str], count: int) -> None:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count(SENTINEL) == count


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_passive_dispatch_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, 1)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_native_passive_dispatch_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, invalid=True))
    assert_stdio(capsys, 0)


@pytest.mark.parametrize("case", TIMEOUTS, ids=lambda case: case.name)
def test_native_passive_dispatch_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, timeout=True))
    assert_stdio(capsys, 1)


@pytest.mark.parametrize("case", EXTRAS, ids=lambda case: case.name)
def test_native_passive_dispatch_extra(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, 1)


async def tool_metadata(root: Path) -> dict[str, Any]:
    runtime, calls, application = fixture_runtime(root)
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        metadata = (await client.list_tools()).model_dump(mode="json", by_alias=True)
    assert calls == application.mock_calls == []
    assert len(metadata["tools"]) == 162
    selected = [tool for tool in metadata["tools"] if tool["name"] in {case.name for case in CASES}]
    assert [tool["name"] for tool in selected] == [case.name for case in CASES]
    for tool in selected:
        read_only = tool["name"] in {CASES[index].name for index in (1, 2, 9, 10)}
        assert tool["annotations"]["readOnlyHint"] is read_only
        assert tool["annotations"]["openWorldHint"] is False
        if not read_only:
            assert tool["annotations"]["destructiveHint"] is False
            assert tool["annotations"]["idempotentHint"] is True
    return metadata


def test_native_passive_dispatch_metadata(tmp_path: Path) -> None:
    asyncio.run(tool_metadata(tmp_path))


_REQUEST_ID_TEXT = re.compile(
    r'("request_id"\s*:\s*")'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(")'
)


def capture_value(value: Any, *, headers: bool = False) -> Any:
    """Normalize only correlation keys and UUID request IDs in native JSON text."""
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
    groups = {
        "records": [
            await invoke_case(root / f"valid-{index}", case) for index, case in enumerate(CASES)
        ],
        "invalid": [
            await invoke_case(root / f"invalid-{index}", case, invalid=True)
            for index, case in enumerate(CASES)
        ],
        "timeouts": [
            await invoke_case(root / f"timeout-{index}", case, timeout=True)
            for index, case in enumerate(TIMEOUTS)
        ],
        "extras": [
            await invoke_case(root / f"extra-{index}", case) for index, case in enumerate(EXTRAS)
        ],
    }
    return {"metadata": metadata, **capture_value(groups)}
