"""Workspace roots for existing MCP voice creation, metadata, and sample tools."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import (
    GetVoiceSamplesInput,
    PublishVoiceInput,
    VoiceCreateInput,
)
from pandrator_mcp.schemas.voice_metadata import (
    UpdateVoiceMetadataInput,
    VoiceMetadataChanges,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import (
    create_voice,
    get_voice_samples,
    safe_scope_projection,
)
from pandrator_mcp.tools.voice_metadata import update_voice_metadata


class RecordingApplication(ApplicationVoiceMethods):
    """Run the real request builders while recording only their transport boundary."""

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


def _voice_create_input(workspace_id: str | None = None) -> VoiceCreateInput:
    values: dict[str, Any] = {
        "name": "Narrator",
        "voice_category": "unspecified",
        "idempotency_key": "voice:create:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return VoiceCreateInput.model_validate(values)


def _voice_metadata_input(workspace_id: str | None = None) -> UpdateVoiceMetadataInput:
    values: dict[str, Any] = {
        "voice_id": "voice-1",
        "expected_revision": 3,
        "changes": VoiceMetadataChanges(name="Updated narrator"),
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return UpdateVoiceMetadataInput.model_validate(values)


def _voice_payload(
    *,
    workspace_id: Any = None,
    can_edit: Any = None,
    include_scope: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "voice-1",
        "name": "Narrator",
        "description": "A clear narrator.",
        "language": "en",
        "voice_category": "female",
        "revision": 4,
        "profile": {"schema_version": 1, "pitch": "mid", "private_extension": "secret-sentinel"},
        "reference": {"kind": "managed", "voice_id": "voice-1", "provider_token": "secret-sentinel"},
        "provider_secret": "secret-sentinel",
        "path": "/private/voice.json",
        "backend_extension": "secret-sentinel",
    }
    if include_scope:
        payload["workspace_id"] = workspace_id
        payload["can_edit"] = can_edit
    return payload


def test_voice_workspace_inputs_are_optional_strict_trimmed_and_bounded():
    cases = (
        (
            VoiceCreateInput,
            {"name": "Narrator", "idempotency_key": "voice:create:1"},
        ),
        (GetVoiceSamplesInput, {"voice_id": "voice-1"}),
        (
            UpdateVoiceMetadataInput,
            {
                "voice_id": "voice-1",
                "expected_revision": 3,
                "changes": VoiceMetadataChanges(name="Narrator"),
            },
        ),
    )
    for model, base_values in cases:
        assert model.model_validate(base_values).workspace_id is None
        assert model.model_validate(base_values | {"workspace_id": None}).workspace_id is None
        assert (
            model.model_validate(base_values | {"workspace_id": "  workspace-1  "}).workspace_id
            == "workspace-1"
        )
        for invalid in ("", "   ", True, 7, ["workspace"], "w" * 81):
            with pytest.raises(ValidationError):
                model.model_validate(base_values | {"workspace_id": invalid})
        with pytest.raises(ValidationError):
            model.model_validate(base_values | {"session_id": "session-1"})

    assert "session_id" not in PublishVoiceInput.model_fields


def test_real_voice_request_builders_scope_only_the_three_fixed_routes():
    application = RecordingApplication()
    application.create_voice(
        name="Narrator",
        language="en",
        description="A clear narrator.",
        voice_category="female",
        profile={"pitch": "mid"},
        idempotency_key="voice:create:1",
        workspace_id="  workspace-1  ",
    )
    application.get_voice_samples("voice/a", workspace_id="workspace-1")
    application.voice_metadata_request(
        "update",
        {
            "voice_id": "voice/a",
            "expected_revision": 7,
            "changes": {"name": "Updated", "profile": {"schema_version": 1}},
            "workspace_id": " workspace-1 ",
        },
    )

    assert application.calls == [
        (
            "/api/v1/voices",
            {
                "method": "POST",
                "body": {
                    "name": "Narrator",
                    "voice_category": "female",
                    "language": "en",
                    "description": "A clear narrator.",
                    "profile": {"pitch": "mid"},
                },
                "idempotency_key": "voice:create:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/voices/voice%2Fa/samples",
            {"parameters": {"workspace_id": "workspace-1"}},
        ),
        (
            "/api/v1/voices/voice%2Fa",
            {
                "method": "PATCH",
                "body": {"name": "Updated", "profile": {"schema_version": 1}},
                "if_match_revision": 7,
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
    ]
    for _path, kwargs in application.calls:
        assert "workspace_id" not in kwargs.get("body", {})
        assert "session_id" not in kwargs.get("body", {})
    assert "idempotency_key" not in application.calls[2][1]


def test_legacy_voice_request_builder_calls_keep_scope_omitted():
    application = RecordingApplication()
    application.create_voice(
        name="Narrator",
        language=None,
        description=None,
        voice_category="unspecified",
        profile=None,
        idempotency_key="voice:create:1",
    )
    application.get_voice_samples("voice-1")
    application.voice_metadata_request(
        "update",
        {
            "voice_id": "voice-1",
            "expected_revision": 3,
            "changes": {"description": "Updated"},
        },
    )

    assert "parameters" not in application.calls[0][1]
    assert application.calls[1] == ("/api/v1/voices/voice-1/samples", {})
    assert "parameters" not in application.calls[2][1]
    assert application.calls[2][1]["maximum_body_bytes"] == 32 * 1024
    assert application.calls[2][1]["if_match_revision"] == 3


def test_direct_client_methods_reject_invalid_workspace_roots_before_transport():
    invalid_roots: tuple[Any, ...] = ("", "  ", True, 7, "w" * 81)
    for root in invalid_roots:
        application = RecordingApplication()
        with pytest.raises(ValueError, match="workspace_id"):
            application.create_voice(
                name="Narrator",
                language=None,
                description=None,
                voice_category="unspecified",
                profile=None,
                idempotency_key="voice:create:1",
                workspace_id=root,
            )
        with pytest.raises(ValueError, match="workspace_id"):
            application.get_voice_samples("voice-1", workspace_id=root)
        with pytest.raises(ValueError, match="workspace_id"):
            application.voice_metadata_request(
                "update",
                {
                    "voice_id": "voice-1",
                    "expected_revision": 3,
                    "changes": {"name": "Narrator"},
                    "workspace_id": root,
                },
            )
        assert application.calls == []

    application = RecordingApplication()
    with pytest.raises(ValueError, match="workspace_id"):
        application.voice_metadata_request(
            "update",
            {
                "voice_id": "voice-1",
                "expected_revision": 3,
                "changes": {"name": "Narrator"},
                "workspace_id": " ",
                "session_id": "unsupported",
            },
        )
    assert application.calls == []


@pytest.mark.parametrize("operation", ("create", "metadata"))
@pytest.mark.parametrize(
    ("backend_workspace", "requested_workspace", "expected_next_arguments"),
    (
        ("backend-workspace", "requested-workspace", {"workspace_id": "backend-workspace"}),
        (None, "requested-workspace", {"workspace_id": "requested-workspace"}),
        (None, None, {}),
    ),
)
def test_voice_mutation_follow_up_scope_is_authoritative_then_requested_then_omitted(
    operation: str,
    backend_workspace: str | None,
    requested_workspace: str | None,
    expected_next_arguments: dict[str, str],
):
    payload = _voice_payload()
    if backend_workspace is not None:
        payload["workspace_id"] = backend_workspace
        payload["can_edit"] = True
    application = Mock()
    if operation == "create":
        application.create_voice.return_value = payload
        outcome = create_voice(_runtime(application), _voice_create_input(requested_workspace))
        forwarded = application.create_voice.call_args.kwargs
    else:
        application.voice_metadata_request.return_value = payload
        outcome = update_voice_metadata(
            _runtime(application), _voice_metadata_input(requested_workspace)
        )
        forwarded = application.voice_metadata_request.call_args.args[1]

    assert outcome.next_actions[0].tool == "pandrator_get_voice_catalog"
    assert outcome.next_actions[0].arguments == expected_next_arguments
    if backend_workspace is not None:
        assert outcome.result["workspace_id"] == backend_workspace
        assert outcome.result["can_edit"] is True
    else:
        assert "workspace_id" not in outcome.result
    if requested_workspace is None:
        assert "workspace_id" not in forwarded
    elif operation == "create":
        assert forwarded["workspace_id"] == requested_workspace
    else:
        assert forwarded["workspace_id"] == requested_workspace
    assert "secret-sentinel" not in str(outcome.result)


def test_sample_list_keeps_existing_revision_and_items_with_safe_scope():
    application = Mock()
    application.get_voice_samples.return_value = {
        "revision": 8,
        "voice_revision": 4,
        "items": [
            {
                "id": "sample-1",
                "voice_id": "voice/a",
                "artifact_id": "artifact-1",
                "status": "ready",
                "transcript": "Hello.",
                "provider_secret": "secret-sentinel",
            }
        ],
        "workspace_id": "backend-workspace",
        "can_edit": False,
        "provider_secret": "secret-sentinel",
    }
    result = get_voice_samples(
        _runtime(application),
        GetVoiceSamplesInput(voice_id="voice/a", workspace_id="requested-workspace"),
    )

    application.get_voice_samples.assert_called_once_with(
        "voice/a", workspace_id="requested-workspace"
    )
    assert result["revision"] == 8
    assert result["voice_revision"] == 4
    assert result["items"][0]["artifact_id"] == "artifact-1"
    assert result["items"][0]["transcript"] == "Hello."
    assert result["workspace_id"] == "backend-workspace"
    assert result["can_edit"] is False
    assert "secret-sentinel" not in str(result)


def test_legacy_voice_responses_do_not_invent_workspace_scope():
    application = Mock()
    legacy_payload = _voice_payload()
    application.create_voice.return_value = legacy_payload
    application.voice_metadata_request.return_value = legacy_payload
    application.get_voice_samples.return_value = {
        "voice_revision": 4,
        "items": [{"id": "sample-1", "artifact_id": "artifact-1"}],
    }

    created = create_voice(_runtime(application), _voice_create_input())
    metadata = update_voice_metadata(_runtime(application), _voice_metadata_input())
    samples = get_voice_samples(
        _runtime(application), GetVoiceSamplesInput(voice_id="voice-1")
    )

    for result in (created.result, metadata.result, samples):
        assert "workspace_id" not in result
        assert "can_edit" not in result
    assert created.next_actions[0].arguments == {}
    assert metadata.next_actions[0].arguments == {}
    assert samples["voice_revision"] == 4
    assert samples["items"][0]["id"] == "sample-1"
    assert "secret-sentinel" not in str(created.result)
    assert "secret-sentinel" not in str(metadata.result)


def test_scope_projection_omits_malformed_backend_fields():
    assert safe_scope_projection(
        {"workspace_id": 7, "can_edit": "true", "provider_secret": "secret-sentinel"}
    ) == {}
    assert safe_scope_projection({"workspace_id": " ", "can_edit": 1}) == {}


def _registered_tools():
    server = build_server(cast(McpRuntime, SimpleNamespace()))

    async def list_tools():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            return await client.list_tools()

    return asyncio.run(list_tools()).tools


def _string_schema_branch(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "string":
        return schema
    return next(branch for branch in schema["anyOf"] if branch.get("type") == "string")


def test_actual_voice_create_metadata_and_samples_tool_schemas_keep_risk_contracts():
    tools = {tool.name: tool for tool in _registered_tools()}
    expected = {
        "pandrator_create_voice": (RiskClass.WRITE, True, "POST"),
        "pandrator_get_voice_samples": (RiskClass.READ, False, "GET"),
        "pandrator_update_voice_metadata": (RiskClass.WRITE, False, "PATCH"),
    }
    for name, (risk, requires_idempotency, method) in expected.items():
        tool = tools[name]
        properties = tool.input_schema["properties"]
        root_schema = _string_schema_branch(properties["workspace_id"])
        assert root_schema["minLength"] == 1
        assert root_schema["maxLength"] == 80
        assert "workspace_id" not in tool.input_schema.get("required", [])
        assert "session_id" not in properties
        action = ACTION_CATALOG.get(name)
        assert action.risk is risk
        assert action.requires_idempotency is requires_idempotency
        assert action.method == method
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (risk is RiskClass.READ)

    create_annotations = tools["pandrator_create_voice"].annotations
    metadata_annotations = tools["pandrator_update_voice_metadata"].annotations
    assert create_annotations is not None
    assert metadata_annotations is not None
    assert create_annotations.idempotent_hint is True
    assert metadata_annotations.idempotent_hint is False
