"""Workspace scope and fixed-route contracts for provider voice copies."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas import TOOL_INPUT_MODELS, PublishVoiceInput, UnpublishVoiceInput
from pandrator_mcp.schemas.voice_lifecycle import AuditionVoiceInput, _RevisionWriteInput
from pandrator_mcp.schemas.work import GetWorkInput
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import publish_voice, unpublish_voice

ProviderInput = TypeVar("ProviderInput", PublishVoiceInput, UnpublishVoiceInput)


class RecordingApplication(ApplicationVoiceMethods):
    """Run actual JSON request builders and record their transport boundary."""

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


def _provider_arguments(
    model: type[ProviderInput], workspace_id: str | None = None
) -> ProviderInput:
    values: dict[str, Any] = {
        "voice_id": "voice-1",
        "service_id": "service-1",
        "expected_revision": 4,
        "idempotency_key": "provider:voice:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return model.model_validate(values)


@pytest.mark.parametrize("model", [PublishVoiceInput, UnpublishVoiceInput])
def test_provider_workspace_inputs_are_optional_strict_trimmed_and_bounded(model):
    values = {
        "voice_id": "voice-1",
        "service_id": "service-1",
        "expected_revision": 1,
        "idempotency_key": "provider:voice:1",
    }
    assert model.model_validate(values).workspace_id is None
    assert model.model_validate(values | {"workspace_id": None}).workspace_id is None
    assert model.model_validate(values | {"workspace_id": "  workspace-1  "}).workspace_id == (
        "workspace-1"
    )
    for invalid in ("", "   ", True, 7, ["workspace"], "w" * 81):
        with pytest.raises(ValidationError):
            model.model_validate(values | {"workspace_id": invalid})
    with pytest.raises(ValidationError):
        model.model_validate(values | {"session_id": "session-1"})
    with pytest.raises(ValidationError):
        model.model_validate(values | {"expected_revision": 0})
    with pytest.raises(ValidationError):
        model.model_validate({key: value for key, value in values.items() if key != "idempotency_key"})
    assert "workspace_id" not in _RevisionWriteInput.model_fields
    assert "workspace_id" in AuditionVoiceInput.model_fields
    assert "session_id" not in model.model_fields

    if model is UnpublishVoiceInput:
        assert model in TOOL_INPUT_MODELS


def test_provider_request_builders_keep_fixed_quoted_routes_and_legacy_omission():
    application = RecordingApplication()
    application.publish_voice(
        "voice/one",
        "service one",
        expected_revision=6,
        idempotency_key="publish:voice:1",
        sample_id="sample-1",
        sample_sha256="a" * 64,
        recipe_signature="recipe-1",
        workspace_id="  workspace-publish  ",
    )
    application.unpublish_voice(
        "voice/one",
        "service one",
        expected_revision=7,
        idempotency_key="remove:voice:1",
        workspace_id="  workspace-remove  ",
    )
    application.publish_voice(
        "voice-two",
        "service-two",
        expected_revision=8,
        idempotency_key="publish:voice:2",
    )
    application.unpublish_voice(
        "voice-two",
        "service-two",
        expected_revision=9,
        idempotency_key="remove:voice:2",
    )

    fixed_path = "/api/v1/voices/voice%2Fone/providers/service%20one"
    assert application.calls == [
        (
            fixed_path,
            {
                "method": "POST",
                "body": {
                    "sample_id": "sample-1",
                    "sample_sha256": "a" * 64,
                    "recipe_signature": "recipe-1",
                },
                "idempotency_key": "publish:voice:1",
                "if_match_revision": 6,
                "parameters": {"workspace_id": "workspace-publish"},
            },
        ),
        (
            fixed_path,
            {
                "method": "DELETE",
                "idempotency_key": "remove:voice:1",
                "if_match_revision": 7,
                "parameters": {"workspace_id": "workspace-remove"},
            },
        ),
        (
            "/api/v1/voices/voice-two/providers/service-two",
            {
                "method": "POST",
                "body": {},
                "idempotency_key": "publish:voice:2",
                "if_match_revision": 8,
            },
        ),
        (
            "/api/v1/voices/voice-two/providers/service-two",
            {
                "method": "DELETE",
                "idempotency_key": "remove:voice:2",
                "if_match_revision": 9,
            },
        ),
    ]
    assert all("body" not in kwargs for _, kwargs in application.calls if kwargs["method"] == "DELETE")


@pytest.mark.parametrize("operation", ["publish", "unpublish"])
@pytest.mark.parametrize(
    ("backend_workspace", "requested_workspace", "expected_workspace"),
    [
        ("backend-workspace", "requested-workspace", "backend-workspace"),
        (None, "requested-workspace", "requested-workspace"),
        (7, "requested-workspace", "requested-workspace"),
        (None, None, None),
    ],
)
def test_provider_tools_project_safe_scope_and_rootless_poll(
    operation: str,
    backend_workspace: Any,
    requested_workspace: str | None,
    expected_workspace: str | None,
):
    payload: dict[str, Any] = {
        "work_id": "job-1",
        "state": "queued",
        "progress": 0.25,
        "voice_id": "voice-1",
        "service_id": "service-1",
        "can_edit": False,
        "path": "/private/voice.json",
        "credential": "secret-sentinel",
        "private_effect_token": "private-effect-sentinel",
        "fingerprint": "fingerprint-sentinel",
    }
    if backend_workspace is not None:
        payload["workspace_id"] = backend_workspace
    application = RecordingApplication(payload)
    if operation == "publish":
        arguments = _provider_arguments(PublishVoiceInput, requested_workspace)
        outcome = publish_voice(_runtime(application), arguments)
        expected_reason = "Inspect provider compatibility after publication completes."
        expected_method = "POST"
    else:
        arguments = _provider_arguments(UnpublishVoiceInput, requested_workspace)
        outcome = unpublish_voice(_runtime(application), arguments)
        expected_reason = "Inspect provider compatibility after removal completes."
        expected_method = "DELETE"

    assert len(application.calls) == 1
    _, request = application.calls[0]
    assert request["method"] == expected_method
    if requested_workspace is None:
        assert "parameters" not in request
    else:
        assert request["parameters"] == {"workspace_id": requested_workspace}
    assert "session_id" not in request.get("parameters", {})

    if isinstance(backend_workspace, str) and backend_workspace.strip():
        assert outcome.result["workspace_id"] == backend_workspace
    else:
        assert "workspace_id" not in outcome.result
    assert outcome.result["can_edit"] is False
    assert all(
        marker not in str(outcome)
        for marker in (
            "/private/voice.json",
            "secret-sentinel",
            "private-effect-sentinel",
            "fingerprint-sentinel",
        )
    )

    poll_action = next(action for action in outcome.next_actions if action.tool == "pandrator_get_work")
    assert poll_action.arguments == {"work_id": "job-1", "work_type": "job"}
    GetWorkInput.model_validate(poll_action.arguments)
    assert "workspace_id" not in poll_action.arguments

    catalogue_action = next(
        action
        for action in outcome.next_actions
        if action.tool == "pandrator_get_voice_catalog"
    )
    expected_action_arguments = (
        {"workspace_id": expected_workspace}
        if expected_workspace is not None
        else {}
    )
    assert catalogue_action.arguments == expected_action_arguments
    assert catalogue_action.reason == expected_reason


def test_build_server_registers_callable_typed_provider_tools_matching_catalog():
    application = RecordingApplication(
        {"work_id": "job-publish", "state": "succeeded", "voice_id": "voice-1"},
        {"work_id": "job-remove", "state": "succeeded", "voice_id": "voice-1"},
    )
    server = build_server(_runtime(application))

    async def list_and_call():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            listed = await client.list_tools()
            publication = await client.call_tool(
                "pandrator_publish_voice",
                {
                    "voice_id": "voice-1",
                    "service_id": "service-1",
                    "expected_revision": 4,
                    "idempotency_key": "publish:voice:1",
                    "workspace_id": "workspace-1",
                },
            )
            removal = await client.call_tool(
                "pandrator_unpublish_voice",
                {
                    "voice_id": "voice-1",
                    "service_id": "service-1",
                    "expected_revision": 5,
                    "idempotency_key": "remove:voice:1",
                },
            )
            return listed.tools, publication, removal

    tools, publication, removal = asyncio.run(list_and_call())
    tools_by_name = {tool.name: tool for tool in tools}
    expected = {
        "pandrator_publish_voice": (
            "PublishVoiceInput",
            "publishVoiceToProvider",
            "POST",
            "/api/v1/voices/{voiceId}/providers/{serviceId}",
            "Publish one managed voice to a provider",
        ),
        "pandrator_unpublish_voice": (
            "UnpublishVoiceInput",
            "removeVoiceFromProvider",
            "DELETE",
            "/api/v1/voices/{voiceId}/providers/{serviceId}",
            "Remove one managed provider voice copy",
        ),
    }
    for name, (model_name, operation_id, method, path, title) in expected.items():
        tool = tools_by_name[name]
        action = ACTION_CATALOG.get(name)
        assert tool.title == title
        assert (
            action.input_model,
            action.risk,
            action.required_scope,
            action.downstream_operation_id,
            action.method,
            action.path,
            action.enabled,
            action.requires_idempotency,
        ) == (
            model_name,
            RiskClass.WRITE,
            "app.write",
            operation_id,
            method,
            path,
            True,
            True,
        )
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
        assert "expected_revision" in tool.input_schema["required"]
        assert "idempotency_key" in tool.input_schema["required"]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.idempotent_hint is True

    assert not publication.is_error
    assert not removal.is_error
    assert [path for path, _ in application.calls] == [
        "/api/v1/voices/voice-1/providers/service-1",
        "/api/v1/voices/voice-1/providers/service-1",
    ]
    assert application.calls[0][1]["parameters"] == {"workspace_id": "workspace-1"}
    assert "parameters" not in application.calls[1][1]
