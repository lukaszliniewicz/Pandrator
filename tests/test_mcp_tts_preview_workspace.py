"""Workspace scope for the existing generic TTS audition MCP action."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import AuditionVoiceInput, _RevisionWriteInput
from pandrator_mcp.schemas.work import GetWorkInput
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import audition_voice


class RecordingApplication(ApplicationVoiceMethods):
    """Run the real request builder while recording its transport boundary."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((path, kwargs))
        return self.responses.pop(0) if self.responses else {}

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Unexpected binary request: {path}")


def _runtime(application: Any) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


def _arguments(workspace_id: str | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "text": "Hello from the audition.",
        "service_id": "service/one",
        "model": "model-a",
        "voice": "voice-a",
        "language": "en",
        "idempotency_key": "audition:voice:workspace:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return values


def test_audition_workspace_input_is_optional_strict_trimmed_and_bounded():
    values = _arguments()
    assert AuditionVoiceInput.model_validate(values).workspace_id is None
    assert (
        AuditionVoiceInput.model_validate(values | {"workspace_id": None}).workspace_id
        is None
    )
    assert (
        AuditionVoiceInput.model_validate(
            values | {"workspace_id": "  workspace-1  "}
        ).workspace_id
        == "workspace-1"
    )
    assert AuditionVoiceInput.model_validate(values).generation_prompt is None
    assert (
        AuditionVoiceInput.model_validate(values | {"generation_prompt": ""})
        .generation_prompt
        == ""
    )
    for invalid in ("", "   ", True, 7, ["workspace"], "w" * 81):
        with pytest.raises(ValidationError):
            AuditionVoiceInput.model_validate(values | {"workspace_id": invalid})
    with pytest.raises(ValidationError):
        AuditionVoiceInput.model_validate(values | {"session_id": "session-1"})

    assert "workspace_id" in AuditionVoiceInput.model_fields
    assert "workspace_id" not in _RevisionWriteInput.model_fields
    assert "session_id" not in AuditionVoiceInput.model_fields


def test_audition_request_builder_keeps_route_body_key_and_legacy_omission():
    application = RecordingApplication()
    application.audition_voice(
        service_id="service/one two",
        text="Hello",
        model="model-a",
        voice="voice-a",
        language="en",
        generation_prompt=None,
        seed=None,
        idempotency_key="audition:voice:scoped:1",
        workspace_id="  workspace-1  ",
    )
    application.audition_voice(
        service_id="legacy service",
        text="Hello again",
        model="model-b",
        voice="voice-b",
        language="en-GB",
        generation_prompt="",
        seed=0,
        idempotency_key="audition:voice:legacy:1",
    )

    assert application.calls == [
        (
            "/api/v1/services/tts/service%2Fone%20two/preview",
            {
                "method": "POST",
                "body": {
                    "text": "Hello",
                    "model": "model-a",
                    "voice": "voice-a",
                    "language": "en",
                },
                "idempotency_key": "audition:voice:scoped:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/services/tts/legacy%20service/preview",
            {
                "method": "POST",
                "body": {
                    "text": "Hello again",
                    "model": "model-b",
                    "voice": "voice-b",
                    "language": "en-GB",
                    "generation_prompt": "",
                    "seed": 0,
                },
                "idempotency_key": "audition:voice:legacy:1",
                "maximum_body_bytes": 64 * 1024,
            },
        ),
    ]


def test_audition_tool_forwards_scope_and_returns_safe_rootless_work_action():
    application = RecordingApplication(
        {
            "id": "job-1",
            "state": "queued",
            "workspace_id": "backend-workspace",
            "can_edit": False,
            "payload": {
                "credential": "credential-sentinel",
                "private_effect_token": "effect-token-sentinel",
            },
        }
    )
    arguments = AuditionVoiceInput.model_validate(_arguments("  workspace-1  "))

    outcome = audition_voice(_runtime(application), arguments)

    assert application.calls == [
        (
            "/api/v1/services/tts/service%2Fone/preview",
            {
                "method": "POST",
                "body": {
                    "text": "Hello from the audition.",
                    "model": "model-a",
                    "voice": "voice-a",
                    "language": "en",
                },
                "idempotency_key": "audition:voice:workspace:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        )
    ]
    assert outcome.result["workspace_id"] == "backend-workspace"
    assert outcome.result["can_edit"] is False
    assert "credential-sentinel" not in str(outcome)
    assert "effect-token-sentinel" not in str(outcome)
    assert outcome.next_actions[0].tool == "pandrator_get_work"
    assert outcome.next_actions[0].arguments == {
        "work_id": "job-1",
        "work_type": "job",
    }
    GetWorkInput.model_validate(outcome.next_actions[0].arguments)
    assert "workspace_id" not in outcome.next_actions[0].arguments


def test_build_server_exposes_callable_typed_audition_tool():
    application = RecordingApplication(
        {
            "id": "job-rooted",
            "state": "queued",
            "workspace_id": "backend-workspace",
        },
        {"id": "job-legacy", "state": "queued"},
    )
    server = build_server(_runtime(application))

    async def list_and_call():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            listed = await client.list_tools()
            rooted = await client.call_tool(
                "pandrator_audition_voice",
                _arguments("workspace-1"),
            )
            legacy = await client.call_tool(
                "pandrator_audition_voice",
                _arguments(),
            )
            return listed.tools, rooted, legacy

    tools, rooted, legacy = asyncio.run(list_and_call())
    tool = next(item for item in tools if item.name == "pandrator_audition_voice")
    properties = tool.input_schema["properties"]
    workspace_schema = properties["workspace_id"]
    if workspace_schema.get("type") != "string":
        workspace_schema = next(
            branch
            for branch in workspace_schema["anyOf"]
            if branch.get("type") == "string"
        )
    assert workspace_schema["minLength"] == 1
    assert workspace_schema["maxLength"] == 80
    assert "workspace_id" not in tool.input_schema.get("required", [])
    assert "session_id" not in properties
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is True
    assert not rooted.is_error
    assert not legacy.is_error
    assert application.calls[0][1]["parameters"] == {"workspace_id": "workspace-1"}
    assert "parameters" not in application.calls[1][1]
