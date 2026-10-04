"""Native transcription registrations preserve bounded uploads and adapter guards."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
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
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.targets import LocalSourceRoot, TargetProfile

CONTENT = b"0123456789"
KEY = "registration:transcription:1"
IDENTIFIER = "transcription-1"
SENTINEL = "transcription registration fixture stdout"
INLINE = {
    "kind": "base64",
    "data": base64.b64encode(CONTENT).decode("ascii"),
    "filename": "clip.wav",
}
TRANSCRIBE = {"source": INLINE, "idempotency_key": KEY}
QUEUED = {"id": IDENTIFIER, "job_id": "job-1", "status": "queued", "progress": 0.0}


def call(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"method": method, "args": list(args), "kwargs": kwargs}


INITIALIZE = call(
    "initialize_transcription",
    filename="clip.wav",
    size_bytes=10,
    sha256=hashlib.sha256(CONTENT).hexdigest(),
    format="txt",
    language="auto",
    engine=None,
    model_quantization=None,
    compute_backend=None,
    qwen_asr_model=None,
    transcription_vocal_isolation=None,
    idempotency_key=KEY,
)
CHUNKS = [
    call("upload_transcription_chunk", IDENTIFIER, 0, b"0123"),
    call("upload_transcription_chunk", IDENTIFIER, 1, b"4567"),
    call("upload_transcription_chunk", IDENTIFIER, 2, b"89"),
]
START = call("start_transcription", IDENTIFIER, wait_seconds=30)
UPLOAD = [INITIALIZE, *CHUNKS, START]
GET = call("get_transcription", IDENTIFIER, format=None, wait_seconds=0)
RESULT = call("get_transcription_result", IDENTIFIER, format="txt", offset=0, limit=16_000)


@dataclass(frozen=True)
class Case:
    id: str
    tool: str
    arguments: dict[str, Any]
    expected_calls: list[dict[str, Any]]
    expected_result: dict[str, Any] | None = None
    mode: str = "normal"
    failure: str | None = None


CASES = [
    Case("transcribe-inline-defaults", "pandrator_transcribe", TRANSCRIBE, UPLOAD, QUEUED),
    Case(
        "get-defaults",
        "pandrator_transcription_get",
        {"id": IDENTIFIER},
        [GET],
        {"id": IDENTIFIER, "status": "succeeded", "format": None},
    ),
    Case(
        "result-defaults",
        "pandrator_transcription_result",
        {"id": IDENTIFIER},
        [RESULT],
        {
            "format": "txt",
            "content": "0123456789",
            "offset": 0,
            "total_chars": 10,
            "next_offset": None,
        },
    ),
    Case(
        "cancel",
        "pandrator_transcription_cancel",
        {"id": IDENTIFIER},
        [call("cancel_transcription", IDENTIFIER)],
        {"id": IDENTIFIER, "status": "cancelled"},
    ),
    Case(
        "delete",
        "pandrator_transcription_delete",
        {"id": IDENTIFIER},
        [call("delete_transcription", IDENTIFIER)],
        {"id": IDENTIFIER, "deleted": True},
    ),
]
INVALID = [
    Case(
        "invalid-source-filename",
        "pandrator_transcribe",
        {"source": {**INLINE, "filename": "clip.txt"}, "idempotency_key": KEY},
        [],
    ),
    Case("invalid-get-id", "pandrator_transcription_get", {"id": ""}, []),
    Case(
        "invalid-result-limit", "pandrator_transcription_result", {"id": IDENTIFIER, "limit": 0}, []
    ),
    Case("invalid-cancel-id", "pandrator_transcription_cancel", {"id": ""}, []),
    Case("invalid-delete-id", "pandrator_transcription_delete", {"id": "x" * 121}, []),
]
TRANSCRIBE_GET = call("get_transcription", IDENTIFIER, format="txt", wait_seconds=30)
EXTRAS = [
    Case(
        "local-same-bytes",
        "pandrator_transcribe",
        {
            "source": {"kind": "local_file", "root": "approved", "path": "clip.wav"},
            "idempotency_key": KEY,
        },
        UPLOAD,
        QUEUED,
    ),
    Case(
        "inline-resume",
        "pandrator_transcribe",
        TRANSCRIBE,
        [INITIALIZE, *CHUNKS[1:], START],
        QUEUED,
        mode="resume",
    ),
    Case(
        "initialize-already-queued",
        "pandrator_transcribe",
        TRANSCRIBE,
        [INITIALIZE, TRANSCRIBE_GET],
        {"id": IDENTIFIER, "status": "succeeded", "format": "txt"},
        mode="already_queued",
    ),
    Case(
        "final-chunk-already-queued",
        "pandrator_transcribe",
        TRANSCRIBE,
        [INITIALIZE, *CHUNKS, TRANSCRIBE_GET],
        {"id": IDENTIFIER, "status": "succeeded", "format": "txt"},
        mode="chunk_queued",
    ),
    Case(
        "get-selected-format-wait",
        "pandrator_transcription_get",
        {"id": IDENTIFIER, "format": "srt", "wait_seconds": 7},
        [call("get_transcription", IDENTIFIER, format="srt", wait_seconds=7)],
        {"id": IDENTIFIER, "status": "succeeded", "format": "srt"},
    ),
    Case(
        "result-selected-page",
        "pandrator_transcription_result",
        {"id": IDENTIFIER, "format": "srt", "offset": 5, "limit": 2},
        [call("get_transcription_result", IDENTIFIER, format="srt", offset=5, limit=2)],
        {"format": "srt", "content": "56", "offset": 5, "total_chars": 10, "next_offset": 7},
    ),
]
TIMEOUTS = [
    Case(
        "timeout-initialize",
        "pandrator_transcribe",
        TRANSCRIBE,
        UPLOAD[:1],
        failure="initialize_transcription",
    ),
    Case(
        "timeout-first-chunk",
        "pandrator_transcribe",
        TRANSCRIBE,
        UPLOAD[:2],
        failure="upload_transcription_chunk",
    ),
    Case(
        "timeout-start", "pandrator_transcribe", TRANSCRIBE, UPLOAD, failure="start_transcription"
    ),
]
DENIED = [
    Case(
        "denied-unknown-root",
        "pandrator_transcribe",
        {
            "source": {"kind": "local_file", "root": "unknown", "path": "clip.wav"},
            "idempotency_key": KEY,
        },
        [],
    ),
    Case(
        "denied-traversal",
        "pandrator_transcribe",
        {
            "source": {"kind": "local_file", "root": "approved", "path": "../outside.wav"},
            "idempotency_key": KEY,
        },
        [],
    ),
]


def fixture_runtime(root: Path, case: Case) -> tuple[McpRuntime, list[dict[str, Any]], Any]:
    root.mkdir(parents=True, exist_ok=True)
    approved = root / "approved"
    approved.mkdir(exist_ok=True)
    (approved / "clip.wav").write_bytes(CONTENT)
    (root / "outside.wav").write_bytes(CONTENT)
    runtime = build_runtime(
        McpSettings(target_name="unconfigured", configuration_path=root / "missing.json")
    )
    runtime.profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace=str(root),
        local_source_roots=(LocalSourceRoot(name="approved", path=str(approved)),),
    )
    application = create_autospec(ApplicationClient, instance=True, spec_set=True)
    calls: list[dict[str, Any]] = []

    def effect(method: str):
        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append({**call(method, *args, **kwargs), "headers": correlation_headers()})
            print(SENTINEL)
            if method == case.failure:
                raise PandratorMcpError(
                    "application_response_timeout",
                    "Fixture response timeout.",
                    details={"operation_outcome": "unknown"},
                    retryable=True,
                )
            if method == "initialize_transcription":
                return {
                    "id": IDENTIFIER,
                    "status": "queued" if case.mode == "already_queued" else "uploading",
                    "job_id": None,
                    "chunk_size": 4,
                    "next_chunk_index": 1 if case.mode == "resume" else 0,
                    "uploaded_bytes": 4 if case.mode == "resume" else 0,
                }
            if method == "upload_transcription_chunk":
                index = args[1]
                return {
                    "id": IDENTIFIER,
                    "status": "queued"
                    if case.mode == "chunk_queued" and index == 2
                    else "uploading",
                    "next_chunk_index": index + 1,
                    "uploaded_bytes": min((index + 1) * 4, 10),
                }
            if method == "start_transcription":
                return copy.deepcopy(QUEUED)
            if method == "get_transcription":
                return {"id": IDENTIFIER, "status": "succeeded", "format": kwargs["format"]}
            if method == "get_transcription_result":
                offset, limit = kwargs["offset"], kwargs["limit"]
                end = min(offset + limit, 10)
                return {
                    "format": kwargs["format"],
                    "content": CONTENT.decode()[offset:end],
                    "offset": offset,
                    "total_chars": 10,
                    "next_offset": end if end < 10 else None,
                }
            if method == "cancel_transcription":
                return {"id": IDENTIFIER, "status": "cancelled"}
            assert method == "delete_transcription"
            return {"id": IDENTIFIER, "deleted": True}

        return invoke

    for method in (
        "initialize_transcription",
        "upload_transcription_chunk",
        "start_transcription",
        "get_transcription",
        "get_transcription_result",
        "cancel_transcription",
        "delete_transcription",
    ):
        getattr(application, method).side_effect = effect(method)
    runtime.application = application
    runtime.startup_error = None
    return runtime, calls, application


def failure_envelope(result: Any, tool: str) -> dict[str, Any]:
    assert result.is_error
    block = next(item for item in result.content if isinstance(item, TextContent))
    prefix = f"Error executing tool {tool}: "
    assert block.text.startswith(prefix)
    return json.loads(block.text[len(prefix) :])


async def invoke_case(
    root: Path, case: Case, *, invalid: bool = False, denied: bool = False
) -> dict[str, Any]:
    runtime, calls, application = fixture_runtime(root, case)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    arguments = copy.deepcopy(case.arguments)
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        result = await client.call_tool(case.tool, arguments)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert [
        {k: v for k, v in item.items() if k != "headers"} for item in calls
    ] == case.expected_calls
    assert len(application.mock_calls) == len(calls)
    if invalid:
        assert result.is_error
        return {
            "id": case.id,
            "name": case.tool,
            "arguments": arguments,
            "result": result.model_dump(mode="json", by_alias=True),
        }
    if denied or case.failure is not None:
        value = failure_envelope(result, case.tool)
        if denied:
            assert value["code"] == (
                "not_found" if case.id == "denied-unknown-root" else "validation_error"
            )
        else:
            assert value["code"] == "application_response_timeout"
            assert value["message"] == "Fixture response timeout."
            assert value["details"] == {"operation_outcome": "unknown"}
            assert value["retryable"] is True
            assert value["next_actions"] == []
    else:
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
        assert value["result"] == case.expected_result
        assert value["work"] is None
        assert value["warnings"] == []
        assert value["next_actions"] == []
    for item in calls:
        assert item["headers"]["X-Request-ID"] == value["request_id"]
        assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", item["headers"]["traceparent"])
    assert len({item["headers"]["traceparent"][3:35] for item in calls}) <= 1
    return {
        "id": case.id,
        "name": case.tool,
        "arguments": arguments,
        "calls": calls,
        "envelope": value,
    }


def assert_stdio(capsys: pytest.CaptureFixture[str], count: int) -> None:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count(SENTINEL) == count


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_native_transcription_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, len(case.expected_calls))


@pytest.mark.parametrize("case", INVALID, ids=lambda case: case.id)
def test_native_transcription_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case, invalid=True))
    assert_stdio(capsys, 0)


@pytest.mark.parametrize("case", EXTRAS, ids=lambda case: case.id)
def test_native_transcription_extra(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, len(case.expected_calls))


@pytest.mark.parametrize("case", TIMEOUTS, ids=lambda case: case.id)
def test_native_transcription_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    asyncio.run(invoke_case(tmp_path, case))
    assert_stdio(capsys, len(case.expected_calls))


@pytest.mark.parametrize("case", DENIED, ids=lambda case: case.id)
def test_native_transcription_denied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: Case
) -> None:
    record = asyncio.run(invoke_case(tmp_path, case, denied=True))
    assert record["envelope"]["request_id"]
    assert_stdio(capsys, 0)


def capture_value(value: Any) -> Any:
    """Represent bytes as hex; normalize only generated correlation identities."""
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, dict):
        return {
            key: "<request>"
            if key in {"request_id", "X-Request-ID"}
            else "<trace>"
            if key == "traceparent"
            else capture_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [capture_value(item) for item in value]
    if isinstance(value, str) and value.startswith("Error executing tool ") and ": {" in value:
        prefix, body = value.split(": ", 1)
        return (
            prefix
            + ": "
            + json.dumps(capture_value(json.loads(body)), sort_keys=True, separators=(",", ":"))
        )
    return value


async def capture_contract(root: Path) -> dict[str, Any]:
    runtime, _, _ = fixture_runtime(root / "metadata", CASES[0])
    async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
        metadata = (await client.list_tools()).model_dump(mode="json", by_alias=True)
    assert len(metadata["tools"]) == 162
    groups: dict[str, list[dict[str, Any]]] = {}
    for name, cases in (
        ("records", CASES),
        ("invalid", INVALID),
        ("extras", EXTRAS),
        ("timeouts", TIMEOUTS),
        ("denied", DENIED),
    ):
        groups[name] = [
            await invoke_case(
                root / case.id, case, invalid=name == "invalid", denied=name == "denied"
            )
            for case in cases
        ]
    return {"metadata": metadata, **capture_value(groups)}
