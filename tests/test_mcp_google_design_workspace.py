"""Workspace scope for the existing Google designed-voice MCP action."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas import TOOL_INPUT_MODELS, DesignGoogleVoiceInput
from pandrator_mcp.schemas.voice_lifecycle import AuditionVoiceInput
from pandrator_mcp.schemas.work import GetWorkInput
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import design_google_voice


class RecordingApplication(ApplicationVoiceMethods):
    """Run the real request builder while recording its transport boundary."""

    def __init__(self, *responses: dict[str, Any]):
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
        "service_id": "gemini",
        "model": "gemini-3.8-flash-tts",
        "name": "Rowan",
        "prompt": "Warm, clear narrator.",
        "idempotency_key": "google:design:workspace:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return values


def test_google_design_workspace_input_is_optional_strict_trimmed_and_bounded():
    values = _arguments()
    assert DesignGoogleVoiceInput.model_validate(values).workspace_id is None
    assert (
        DesignGoogleVoiceInput.model_validate(values | {"workspace_id": None}).workspace_id
        is None
    )
    assert (
        DesignGoogleVoiceInput.model_validate(
            values | {"workspace_id": "  workspace-1  "}
        ).workspace_id
        == "workspace-1"
    )
    for invalid in ("", "   ", True, 7, ["workspace"], "w" * 81):
        with pytest.raises(ValidationError):
            DesignGoogleVoiceInput.model_validate(values | {"workspace_id": invalid})
    with pytest.raises(ValidationError):
        DesignGoogleVoiceInput.model_validate(values | {"session_id": "session-1"})

    assert "workspace_id" in DesignGoogleVoiceInput.model_fields
    assert "session_id" not in DesignGoogleVoiceInput.model_fields
    assert "workspace_id" in AuditionVoiceInput.model_fields
    assert DesignGoogleVoiceInput in TOOL_INPUT_MODELS


def test_google_design_request_builder_adds_only_the_optional_query():
    application = RecordingApplication()
    application.design_google_voice(
        service_id="vertex_ai",
        model="gemini-3.8-flash-lite-tts",
        name="Rowan",
        prompt="Warm narrator",
        language="en-GB",
        voice_category="androgynous",
        idempotency_key="google:design:query:1",
        workspace_id="  workspace-1  ",
    )
    application.design_google_voice(
        service_id="gemini",
        model="gemini-3.8-flash-tts",
        name="Rowan",
        prompt="Warm narrator",
        language="en",
        voice_category="unspecified",
        idempotency_key="google:design:legacy:1",
    )

    expected_body = {
        "service_id": "vertex_ai",
        "model": "gemini-3.8-flash-lite-tts",
        "name": "Rowan",
        "prompt": "Warm narrator",
        "language": "en-GB",
        "voice_category": "androgynous",
    }
    assert application.calls == [
        (
            "/api/v1/voices/design/google",
            {
                "method": "POST",
                "body": expected_body,
                "idempotency_key": "google:design:query:1",
                "maximum_body_bytes": 16 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/voices/design/google",
            {
                "method": "POST",
                "body": {
                    "service_id": "gemini",
                    "model": "gemini-3.8-flash-tts",
                    "name": "Rowan",
                    "prompt": "Warm narrator",
                    "language": "en",
                    "voice_category": "unspecified",
                },
                "idempotency_key": "google:design:legacy:1",
                "maximum_body_bytes": 16 * 1024,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("backend_workspace", "requested_workspace", "expected_workspace"),
    [
        ("backend-workspace", "requested-workspace", "backend-workspace"),
        (None, "requested-workspace", "requested-workspace"),
        (7, "requested-workspace", "requested-workspace"),
        (None, None, None),
    ],
)
def test_google_design_tool_projects_safe_scope_and_rootless_poll(
    backend_workspace: Any,
    requested_workspace: str | None,
    expected_workspace: str | None,
):
    payload: dict[str, Any] = {
        "id": "job-google-1",
        "state": "queued",
        "result": {"voice_id": "voice-google-1"},
        "can_edit": False,
        "path": "/private/google-voice.json",
        "provider_secret": "secret-sentinel",
        "private_effect_token": "effect-sentinel",
        "fingerprint": "fingerprint-sentinel",
        "endpoint": "https://private.example",
    }
    if backend_workspace is not None:
        payload["workspace_id"] = backend_workspace
    application = RecordingApplication(payload)
    arguments = DesignGoogleVoiceInput.model_validate(_arguments(requested_workspace))

    outcome = design_google_voice(_runtime(application), arguments)

    assert len(application.calls) == 1
    path, request = application.calls[0]
    assert path == "/api/v1/voices/design/google"
    if requested_workspace is None:
        assert "parameters" not in request
    else:
        assert request["parameters"] == {"workspace_id": requested_workspace}

    if isinstance(backend_workspace, str) and backend_workspace.strip():
        assert outcome.result["workspace_id"] == backend_workspace
    else:
        assert "workspace_id" not in outcome.result
    assert outcome.result["can_edit"] is False
    assert outcome.result["voice_id"] == "voice-google-1"
    assert all(
        marker not in str(outcome)
        for marker in (
            "/private/google-voice.json",
            "secret-sentinel",
            "effect-sentinel",
            "fingerprint-sentinel",
            "https://private.example",
        )
    )

    assert [action.tool for action in outcome.next_actions] == [
        "pandrator_get_work",
        "pandrator_get_voice_catalog",
    ]
    poll_action = outcome.next_actions[0]
    assert poll_action.arguments == {"work_id": "job-google-1", "work_type": "job"}
    GetWorkInput.model_validate(poll_action.arguments)
    assert "workspace_id" not in poll_action.arguments

    catalog_action = outcome.next_actions[1]
    expected_catalog_arguments = {
        "query": "voice-google-1",
        "kind": "managed",
    }
    if expected_workspace is not None:
        expected_catalog_arguments["workspace_id"] = expected_workspace
    assert catalog_action.arguments == expected_catalog_arguments


def test_build_server_exposes_callable_typed_google_design_tool():
    application = RecordingApplication(
        {
            "id": "job-google-rooted",
            "state": "queued",
            "result": {"voice_id": "voice-rooted"},
            "workspace_id": "backend-workspace",
        },
        {"id": "job-google-legacy", "state": "queued", "result": {"voice_id": "voice-legacy"}},
    )
    server = build_server(_runtime(application))

    async def list_and_call():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            listed = await client.list_tools()
            rooted = await client.call_tool(
                "pandrator_design_google_voice",
                _arguments("workspace-1"),
            )
            legacy = await client.call_tool(
                "pandrator_design_google_voice",
                _arguments(),
            )
            return listed.tools, rooted, legacy

    tools, rooted, legacy = asyncio.run(list_and_call())
    tool = next(item for item in tools if item.name == "pandrator_design_google_voice")
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
    assert tool.title == "Create and save a Gemini or Vertex designed voice"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert not rooted.is_error
    assert not legacy.is_error
    assert application.calls[0][1]["parameters"] == {"workspace_id": "workspace-1"}
    assert "parameters" not in application.calls[1][1]
