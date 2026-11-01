"""Workspace roots for the existing voice transcription and review tools."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import (
    AuditionVoiceInput,
    PublishVoiceInput,
    ReviewVoiceTranscriptInput,
    TranscribeVoiceSampleInput,
    _RevisionWriteInput,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import (
    review_voice_transcript,
    transcribe_voice_sample,
)


class RecordingApplication(ApplicationVoiceMethods):
    """Run real request builders and record only their JSON transport calls."""

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


def _transcribe_input(workspace_id: str | None = None) -> TranscribeVoiceSampleInput:
    values: dict[str, Any] = {
        "voice_id": "voice-1",
        "sample_id": "sample-1",
        "settings": {"language": "en"},
        "idempotency_key": "transcribe:voice:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return TranscribeVoiceSampleInput.model_validate(values)


def _review_input(workspace_id: str | None = None) -> ReviewVoiceTranscriptInput:
    values: dict[str, Any] = {
        "voice_id": "voice-1",
        "sample_id": "sample-1",
        "transcript": "Hello from the sample.",
        "language": "en",
        "expected_voice_revision": 4,
        "idempotency_key": "transcript:review:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return ReviewVoiceTranscriptInput.model_validate(values)


def test_transcription_workspace_inputs_are_optional_strict_trimmed_and_bounded():
    cases = (
        (
            TranscribeVoiceSampleInput,
            {
                "voice_id": "voice-1",
                "sample_id": "sample-1",
                "idempotency_key": "transcribe:voice:1",
            },
        ),
        (
            ReviewVoiceTranscriptInput,
            {
                "voice_id": "voice-1",
                "sample_id": "sample-1",
                "transcript": "Hello.",
                "expected_voice_revision": 1,
                "idempotency_key": "transcript:review:1",
            },
        ),
    )
    for model, base_values in cases:
        assert model.model_validate(base_values).workspace_id is None
        assert model.model_validate(base_values | {"workspace_id": None}).workspace_id is None
        assert (
            model.model_validate(base_values | {"workspace_id": "  workspace-1  "})
            .workspace_id
            == "workspace-1"
        )
        for invalid in ("", "   ", True, 7, ["workspace"], "w" * 81):
            with pytest.raises(ValidationError):
                model.model_validate(base_values | {"workspace_id": invalid})
        with pytest.raises(ValidationError):
            model.model_validate(base_values | {"session_id": "session-1"})

    assert "workspace_id" not in _RevisionWriteInput.model_fields
    assert "workspace_id" in AuditionVoiceInput.model_fields
    assert "session_id" not in AuditionVoiceInput.model_fields
    assert "session_id" not in PublishVoiceInput.model_fields


def test_transcription_settings_validation_remains_bounded():
    with pytest.raises(ValidationError):
        TranscribeVoiceSampleInput.model_validate(
            {
                "voice_id": "voice-1",
                "sample_id": "sample-1",
                "settings": "language=en",
                "idempotency_key": "transcribe:voice:1",
            }
        )
    with pytest.raises(ValidationError):
        TranscribeVoiceSampleInput.model_validate(
            {
                "voice_id": "voice-1",
                "sample_id": "sample-1",
                "settings": {"oversized": "x" * (32 * 1024)},
                "idempotency_key": "transcribe:voice:1",
            }
        )


def test_real_transcription_and_review_request_builders_scope_fixed_routes_only():
    application = RecordingApplication()
    application.transcribe_voice_sample(
        "voice/a",
        "sample/a",
        settings={"language": "en", "mode": "draft"},
        idempotency_key="transcribe:voice:1",
        workspace_id=" workspace-1 ",
    )
    application.review_voice_transcript(
        "voice/a",
        "sample/a",
        transcript="Hello.",
        language="en",
        expected_voice_revision=4,
        idempotency_key="transcript:review:1",
        workspace_id="workspace-1",
    )

    assert application.calls == [
        (
            "/api/v1/voices/voice%2Fa/samples/sample%2Fa/transcribe",
            {
                "method": "POST",
                "body": {"language": "en", "mode": "draft"},
                "idempotency_key": "transcribe:voice:1",
                "maximum_body_bytes": 32 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/voices/voice%2Fa/samples/sample%2Fa/transcript",
            {
                "method": "PATCH",
                "body": {
                    "transcript": "Hello.",
                    "expected_voice_revision": 4,
                    "language": "en",
                },
                "idempotency_key": "transcript:review:1",
                "if_match_revision": 4,
                "maximum_body_bytes": 32 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
    ]
    assert len(application.calls) == 2
    for _path, kwargs in application.calls:
        assert "workspace_id" not in kwargs["body"]
        assert "session_id" not in kwargs["body"]


def test_legacy_transcription_request_builders_keep_scope_omitted():
    application = RecordingApplication()
    application.transcribe_voice_sample(
        "voice/a",
        "sample/a",
        settings={"language": "en"},
        idempotency_key="transcribe:voice:1",
    )
    application.review_voice_transcript(
        "voice/a",
        "sample/a",
        transcript="Hello.",
        language=None,
        expected_voice_revision=4,
        idempotency_key="transcript:review:1",
    )

    assert application.calls == [
        (
            "/api/v1/voices/voice%2Fa/samples/sample%2Fa/transcribe",
            {
                "method": "POST",
                "body": {"language": "en"},
                "idempotency_key": "transcribe:voice:1",
                "maximum_body_bytes": 32 * 1024,
            },
        ),
        (
            "/api/v1/voices/voice%2Fa/samples/sample%2Fa/transcript",
            {
                "method": "PATCH",
                "body": {"transcript": "Hello.", "expected_voice_revision": 4},
                "idempotency_key": "transcript:review:1",
                "if_match_revision": 4,
                "maximum_body_bytes": 32 * 1024,
            },
        ),
    ]


def test_direct_client_methods_reject_invalid_workspace_before_transport():
    invalid_roots: tuple[Any, ...] = ("", "  ", True, 7, ["workspace"], "w" * 81)
    for root in invalid_roots:
        application = RecordingApplication()
        with pytest.raises(ValueError, match="workspace_id"):
            application.transcribe_voice_sample(
                "voice-1",
                "sample-1",
                settings={},
                idempotency_key="transcribe:voice:1",
                workspace_id=root,
            )
        with pytest.raises(ValueError, match="workspace_id"):
            application.review_voice_transcript(
                "voice-1",
                "sample-1",
                transcript="Hello.",
                language=None,
                expected_voice_revision=1,
                idempotency_key="transcript:review:1",
                workspace_id=root,
            )
        assert application.calls == []


@pytest.mark.parametrize(
    ("operation", "backend_workspace", "requested_workspace", "expected_workspace"),
    (
        ("transcribe", "backend-workspace", "requested-workspace", "backend-workspace"),
        ("transcribe", None, "requested-workspace", "requested-workspace"),
        ("transcribe", None, None, None),
        ("review", "backend-workspace", "requested-workspace", "backend-workspace"),
        ("review", None, "requested-workspace", "requested-workspace"),
        ("review", None, None, None),
    ),
)
def test_tool_scope_results_and_sample_followups_use_backend_then_request_then_omit(
    operation: str,
    backend_workspace: str | None,
    requested_workspace: str | None,
    expected_workspace: str | None,
):
    payload: dict[str, Any] = {
        "id": "job-1" if operation == "transcribe" else "sample-1",
        "state": "queued" if operation == "transcribe" else "reviewed",
        "voice_id": "voice-1",
        "sample_id": "sample-1",
        "transcript": "Reviewed transcript.",
        "transcript_language": "en",
        "revision": 8,
        "voice_revision": 5,
        "path": "/private/sample.wav",
        "credentials": {"token": "secret-sentinel"},
        "backend_extension": "secret-sentinel",
    }
    if backend_workspace is not None:
        payload["workspace_id"] = backend_workspace
        payload["can_edit"] = True

    application = RecordingApplication(payload)
    if operation == "transcribe":
        outcome = transcribe_voice_sample(
            _runtime(application), _transcribe_input(requested_workspace)
        )
    else:
        outcome = review_voice_transcript(
            _runtime(application), _review_input(requested_workspace)
        )
    assert len(application.calls) == 1
    _, request = application.calls[0]
    if requested_workspace is None:
        assert "parameters" not in request
    else:
        assert request["parameters"] == {"workspace_id": requested_workspace}
    assert "workspace_id" not in request["body"]
    assert "session_id" not in request["body"]

    assert "secret-sentinel" not in str(outcome.result)
    if backend_workspace is None:
        assert "workspace_id" not in outcome.result
        assert "can_edit" not in outcome.result
    else:
        assert outcome.result["workspace_id"] == backend_workspace
        assert outcome.result["can_edit"] is True

    sample_action = next(
        action for action in outcome.next_actions if action.tool == "pandrator_get_voice_samples"
    )
    expected_arguments = {"voice_id": "voice-1"}
    if expected_workspace is not None:
        expected_arguments["workspace_id"] = expected_workspace
    assert sample_action.arguments == expected_arguments

    if operation == "transcribe":
        work_action = outcome.next_actions[0]
        assert work_action.tool == "pandrator_get_work"
        assert work_action.arguments == {"work_id": "job-1", "work_type": "job"}


def test_registered_transcription_tool_schemas_expose_workspace_only_on_selected_tools():
    server = build_server(cast(McpRuntime, SimpleNamespace()))

    async def list_tools():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(list_tools()).tools}
    selected = {
        "pandrator_transcribe_voice_sample",
        "pandrator_review_voice_transcript",
    }
    for name in selected:
        tool = tools[name]
        properties = tool.input_schema["properties"]
        root_schema = properties["workspace_id"]
        if root_schema.get("type") != "string":
            root_schema = next(
                branch for branch in root_schema["anyOf"] if branch.get("type") == "string"
            )
        assert root_schema["minLength"] == 1
        assert root_schema["maxLength"] == 80
        assert "workspace_id" not in tool.input_schema.get("required", [])
        assert "session_id" not in properties
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False

    excluded = {"pandrator_audition_voice"}
    for name in excluded:
        properties = tools[name].input_schema["properties"]
        assert "workspace_id" in properties
        assert "workspace_id" not in tools[name].input_schema.get("required", [])
        assert "session_id" not in properties

    expected_catalog = {
        "pandrator_transcribe_voice_sample": (
            "TranscribeVoiceSampleInput",
            RiskClass.WRITE,
            "app.write",
            "POST",
            "/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe",
            True,
        ),
        "pandrator_review_voice_transcript": (
            "ReviewVoiceTranscriptInput",
            RiskClass.WRITE,
            "app.write",
            "PATCH",
            "/api/v1/voices/{voiceId}/samples/{sampleId}/transcript",
            True,
        ),
    }
    for name, expected in expected_catalog.items():
        action = ACTION_CATALOG.get(name)
        actual = (
            action.input_model,
            action.risk,
            action.required_scope,
            action.method,
            action.path,
            action.requires_idempotency,
        )
        assert actual == expected
