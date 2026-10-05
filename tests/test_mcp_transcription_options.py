"""Published transcription options reach the guarded adapter instead of disappearing."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.context import McpRuntime
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.server import build_server
from pandrator_mcp.targets import LocalSourceRoot, TargetProfile
from tests.test_mcp_media_edit_registration import SENTINEL, envelope, fixture_runtime

CONTENT = b"0123456789"
KEY = "transcribe:options:1"
OPTIONS = {"qwen_asr_model": "qwen3_asr_1_7b", "transcription_vocal_isolation": "htdemucs"}


def option_runtime(root: Path) -> tuple[McpRuntime, list[dict[str, Any]], Any]:
    runtime, calls, application = fixture_runtime(root)
    runtime.profile = TargetProfile(
        name="fixture",
        mode=TargetMode.LOCAL_MANAGED,
        workspace=str(root),
        local_source_roots=(LocalSourceRoot(name="approved", path=str(root)),),
    )
    (root / "clip.wav").write_bytes(CONTENT)

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
            return {"id": "transcription-1", "status": "queued"}

        return invoke

    application.initialize_transcription.side_effect = effect("initialize_transcription")
    application.get_transcription.side_effect = effect("get_transcription")
    return runtime, calls, application


def source(kind: str) -> dict[str, Any]:
    if kind == "local_file":
        return {"kind": kind, "root": "approved", "path": "clip.wav"}
    return {
        "kind": "base64",
        "filename": "clip.wav",
        "data": base64.b64encode(CONTENT).decode("ascii"),
    }


@pytest.mark.parametrize("kind", ["local_file", "base64"])
@pytest.mark.parametrize("with_options", [False, True])
def test_native_transcription_options_forwarded(
    tmp_path: Path, capsys, kind: str, with_options: bool
) -> None:
    runtime, calls, application = option_runtime(tmp_path)
    arguments = {"source": source(kind), "engine": "qwen3", "idempotency_key": KEY}
    if with_options:
        arguments.update(OPTIONS)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(build_server(runtime), raise_exceptions=False) as client:
            return await client.call_tool("pandrator_transcribe", arguments)

    result = asyncio.run(call())
    value = envelope(result)
    assert value["result"] == {"id": "transcription-1", "status": "queued"}
    assert value["work"] is None
    assert value["warnings"] == value["next_actions"] == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert [c["method"] for c in calls] == ["initialize_transcription", "get_transcription"]
    assert calls[0]["args"] == []
    assert calls[0]["kwargs"] == {
        "filename": "clip.wav",
        "size_bytes": len(CONTENT),
        "sha256": hashlib.sha256(CONTENT).hexdigest(),
        "format": "txt",
        "language": "auto",
        "engine": "qwen3",
        "model_quantization": None,
        "compute_backend": None,
        "qwen_asr_model": OPTIONS["qwen_asr_model"] if with_options else None,
        "transcription_vocal_isolation": OPTIONS["transcription_vocal_isolation"]
        if with_options
        else None,
        "idempotency_key": KEY,
    }
    assert calls[1]["args"] == ["transcription-1"]
    assert calls[1]["kwargs"] == {"format": "txt", "wait_seconds": 30}
    assert len(application.mock_calls) == len(calls) == 2
    assert all(c["headers"]["X-Request-ID"] == value["request_id"] for c in calls)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


@pytest.mark.parametrize(
    ("field", "invalid"),
    [("qwen_asr_model", "qwen3_asr_9_9b"), ("transcription_vocal_isolation", "demucs")],
)
def test_invalid_transcription_option_rejected(tmp_path: Path, field: str, invalid: str) -> None:
    runtime, calls, application = option_runtime(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(build_server(runtime), raise_exceptions=False) as client:
            return await client.call_tool(
                "pandrator_transcribe",
                {"source": source("base64"), "idempotency_key": KEY, field: invalid},
            )

    result = asyncio.run(call())
    assert result.is_error
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert any(isinstance(item, TextContent) and field in item.text for item in result.content)


def test_discovered_transcription_options_are_canonical_and_optional(tmp_path: Path) -> None:
    runtime, calls, application = option_runtime(tmp_path)

    async def inventory():
        async with Client(build_server(runtime)) as client:
            return await client.list_tools()

    tools = asyncio.run(inventory()).tools
    assert len(tools) == 162
    schema = next(tool.input_schema for tool in tools if tool.name == "pandrator_transcribe")
    for field, expected in {
        "qwen_asr_model": {"qwen3_asr_0_6b", "qwen3_asr_1_7b"},
        "transcription_vocal_isolation": {"off", "bs_roformer", "mel_band_roformer", "htdemucs"},
    }.items():
        assert field in schema["properties"]
        property_schema = schema["properties"][field]
        assert property_schema["default"] is None
        assert field not in schema["required"]
        choices = property_schema.get("anyOf", [property_schema])
        assert {value for choice in choices for value in choice.get("enum", [])} == expected
    assert calls == application.mock_calls == []
