"""Native enum strings cannot silently become nullable default selections."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from pandrator_mcp.server import build_server
from tests.test_mcp_media_edit_registration import fixture_runtime
from tests.test_mcp_transcription_registration import CASES as TRANSCRIPTION_CASES
from tests.test_mcp_transcription_registration import INITIALIZE, TRANSCRIBE
from tests.test_mcp_transcription_registration import fixture_runtime as transcription_runtime

KEY = "nullable:enum:1"
BASE = {"session_id": "session-1"}
OMITTED = object()
CASES = [
    ("pandrator_list_sessions", "workflow_kind", {}),
    ("pandrator_describe_parameters", "workflow_kind", {"query": "tts"}),
    (
        "pandrator_update_session",
        "workflow_kind",
        {**BASE, "name": "Fixture", "expected_revision": 1, "idempotency_key": KEY},
    ),
    ("pandrator_preview_subtitles", "stage", BASE),
    ("pandrator_transcribe", "compute_backend", TRANSCRIBE),
    ("pandrator_transcribe", "qwen_asr_model", TRANSCRIBE),
    ("pandrator_transcribe", "transcription_vocal_isolation", TRANSCRIBE),
    ("pandrator_transcription_get", "format", {"id": "transcription-1"}),
    (
        "pandrator_create_source_cleaning_dispatch_run",
        "pdf_ocr_mode",
        {**BASE, "idempotency_key": KEY},
    ),
    (
        "pandrator_configure_tts",
        "tts_context_mode",
        {**BASE, "service_id": "fixture", "expected_revision": 1, "idempotency_key": KEY},
    ),
    (
        "pandrator_revise_speech_block_plan",
        "text_layer",
        {
            **BASE,
            "expected_revision_id": "revision-1",
            "action": "merge",
            "left_segment_id": "left-1",
            "right_segment_id": "right-1",
            "idempotency_key": KEY,
        },
    ),
    (
        "pandrator_preview_performance_plan",
        "context_mode",
        {**BASE, "plan_id": "plan-1", "segment_id": "segment-1"},
    ),
]


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("tool,parameter,arguments", CASES, ids=[f"{t}:{p}" for t, p, _ in CASES])
@pytest.mark.parametrize("value", ["null", "invalid-choice"], ids=["null-text", "unknown-choice"])
def test_native_nullable_enum_rejects_invalid_token_before_application(
    tmp_path: Path, protocol: str, tool: str, parameter: str, arguments: dict[str, Any], value: str
) -> None:
    runtime, _, application = fixture_runtime(tmp_path)

    async def invoke():
        async with Client(build_server(runtime), mode=protocol, raise_exceptions=False) as client:
            return await client.call_tool(tool, {**arguments, parameter: value})

    result = asyncio.run(invoke())
    assert result.is_error
    assert application.mock_calls == []


TRANSCRIPTION_VALUES = (
    [
        ("compute_backend", value)
        for value in ["auto", "cpu", "cuda", "vulkan", "metal", None, OMITTED]
    ]
    + [("qwen_asr_model", value) for value in ["qwen3_asr_0_6b", "qwen3_asr_1_7b", None, OMITTED]]
    + [
        ("transcription_vocal_isolation", value)
        for value in ["off", "bs_roformer", "mel_band_roformer", "htdemucs", None, OMITTED]
    ]
)


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("parameter,value", TRANSCRIPTION_VALUES)
def test_native_transcription_enum_retains_choice_and_nullable_defaults(
    tmp_path: Path, protocol: str, parameter: str, value: Any
) -> None:
    runtime, calls, application = transcription_runtime(tmp_path, TRANSCRIPTION_CASES[0])
    arguments = copy.deepcopy(TRANSCRIBE)
    if value is not OMITTED:
        arguments[parameter] = value

    async def invoke():
        async with Client(build_server(runtime), mode=protocol, raise_exceptions=False) as client:
            return await client.call_tool("pandrator_transcribe", arguments)

    result = asyncio.run(invoke())
    assert not result.is_error
    assert len(calls) == len(application.mock_calls) == 5
    expected = copy.deepcopy(INITIALIZE["kwargs"])
    expected[parameter] = None if value is OMITTED else value
    assert calls[0]["method"] == "initialize_transcription"
    assert calls[0]["args"] == [] and calls[0]["kwargs"] == expected


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("value", ["off", "before", "both", None, OMITTED])
def test_generated_performance_enum_retains_choice_and_nullable_defaults(
    tmp_path: Path, protocol: str, value: Any
) -> None:
    runtime, _, application = fixture_runtime(tmp_path)
    application.performance_plan_request.return_value = {"id": "plan-1", "status": "draft"}
    arguments = {**BASE, "plan_id": "plan-1", "segment_id": "segment-1"}
    if value is not OMITTED:
        arguments["context_mode"] = value

    async def invoke():
        async with Client(build_server(runtime), mode=protocol, raise_exceptions=False) as client:
            return await client.call_tool("pandrator_preview_performance_plan", arguments)

    result = asyncio.run(invoke())
    assert not result.is_error
    expected = {**BASE, "plan_id": "plan-1", "segment_id": "segment-1"}
    if value is not OMITTED and value is not None:
        expected["context_mode"] = value
    application.performance_plan_request.assert_called_once_with("preview", expected)
    assert len(application.mock_calls) == 1


@pytest.mark.parametrize("tool,parameter,arguments", CASES, ids=[f"{t}:{p}" for t, p, _ in CASES])
def test_sdk_convenience_call_also_rejects_null_enum_text(
    tmp_path: Path, tool: str, parameter: str, arguments: dict[str, Any]
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    runtime, _, application = fixture_runtime(tmp_path)
    server = build_server(runtime)
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool, {**arguments, parameter: "null"}))
    assert application.mock_calls == []
