"""Optional managed voice selection for MCP audition requests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import (
    AuditionVoiceInput,
    VoiceCatalogInput,
    _RevisionWriteInput,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import audition_voice


def _arguments(**overrides: Any) -> dict[str, Any]:
    return {
        "text": "Hello from the audition.",
        "service_id": "service-a",
        "model": "model-a",
        "voice": "voice-a",
        "language": "en",
        "idempotency_key": "audition:voice:retained:1",
    } | overrides


def test_audition_voice_id_is_optional_strict_trimmed_and_bounded():
    base = _arguments()
    assert AuditionVoiceInput.model_validate(base).voice_id is None
    assert AuditionVoiceInput.model_validate(base | {"voice_id": None}).voice_id is None
    assert (
        AuditionVoiceInput.model_validate(base | {"voice_id": "  bundled:voice-1  "}).voice_id
        == "bundled:voice-1"
    )
    assert "voice_id" in AuditionVoiceInput.model_fields
    assert "voice_id" not in _RevisionWriteInput.model_fields
    assert "voice_id" not in VoiceCatalogInput.model_fields

    for invalid in ("", "   ", True, 7, ["voice"], "v" * 81):
        with pytest.raises(ValidationError):
            AuditionVoiceInput.model_validate(base | {"voice_id": invalid})


class _RecordingApplication(ApplicationVoiceMethods):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((path, kwargs))
        return {"id": "job-audition", "state": "queued"}

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Unexpected binary request: {path}")


def test_request_builder_adds_voice_id_only_when_provided():
    application = _RecordingApplication()
    application.audition_voice(
        service_id="service/one",
        text="Hello",
        model="model-a",
        voice="voice-a",
        language="en",
        generation_prompt=None,
        seed=None,
        idempotency_key="audition:voice:omitted:1",
    )
    application.audition_voice(
        service_id="service/one",
        text="Hello again",
        model="model-b",
        voice="voice-b",
        language="en-GB",
        generation_prompt="",
        seed=0,
        idempotency_key="audition:voice:selected:1",
        workspace_id="workspace-1",
        voice_id="bundled:voice-1",
    )

    assert application.calls == [
        (
            "/api/v1/services/tts/service%2Fone/preview",
            {
                "method": "POST",
                "body": {
                    "text": "Hello",
                    "model": "model-a",
                    "voice": "voice-a",
                    "language": "en",
                },
                "idempotency_key": "audition:voice:omitted:1",
                "maximum_body_bytes": 64 * 1024,
            },
        ),
        (
            "/api/v1/services/tts/service%2Fone/preview",
            {
                "method": "POST",
                "body": {
                    "text": "Hello again",
                    "model": "model-b",
                    "voice": "voice-b",
                    "language": "en-GB",
                    "voice_id": "bundled:voice-1",
                    "generation_prompt": "",
                    "seed": 0,
                },
                "idempotency_key": "audition:voice:selected:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
    ]


@pytest.mark.parametrize(
    ("provided_voice_id", "expected_voice_id"),
    [(False, None), (True, "bundled:voice-2")],
)
def test_tool_forwards_only_provided_voice_id_and_keeps_safe_work_projection(
    provided_voice_id: bool,
    expected_voice_id: str | None,
):
    application = Mock()
    application.audition_voice.return_value = {
        "id": "job-audition",
        "state": "queued",
        "payload": {"credential": "private-sentinel", "voice_id": "private-voice"},
    }
    runtime = cast(Any, SimpleNamespace(require_application=lambda: application))
    values = _arguments(workspace_id="workspace-1")
    if provided_voice_id:
        values["voice_id"] = "bundled:voice-2"

    outcome = audition_voice(runtime, AuditionVoiceInput.model_validate(values))

    expected_arguments = {
        "service_id": "service-a",
        "text": "Hello from the audition.",
        "model": "model-a",
        "voice": "voice-a",
        "language": "en",
        "generation_prompt": None,
        "seed": None,
        "idempotency_key": "audition:voice:retained:1",
        "workspace_id": "workspace-1",
    }
    if expected_voice_id is not None:
        expected_arguments["voice_id"] = expected_voice_id
    application.audition_voice.assert_called_once_with(**expected_arguments)
    assert outcome.work is not None
    assert outcome.work.id == "job-audition"
    assert outcome.next_actions[0].tool == "pandrator_get_work"
    assert "private-sentinel" not in str(outcome)
    assert "private-voice" not in str(outcome)


def test_mcp_schema_advertises_optional_bounded_voice_id_on_audition_only():
    application = _RecordingApplication()
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    server = build_server(runtime)

    async def list_tools():
        async with Client(server, mode="auto") as client:
            return await client.list_tools()

    tools = asyncio.run(list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    audition_properties = by_name["pandrator_audition_voice"].input_schema["properties"]
    voice_id_schema = audition_properties["voice_id"]
    if voice_id_schema.get("type") != "string":
        voice_id_schema = next(
            branch for branch in voice_id_schema["anyOf"] if branch.get("type") == "string"
        )
    assert voice_id_schema["minLength"] == 1
    assert voice_id_schema["maxLength"] == 80
    assert "voice_id" not in by_name["pandrator_audition_voice"].input_schema.get("required", [])
    assert "voice_id" not in by_name["pandrator_get_voice_catalog"].input_schema["properties"]
