"""Workspace-scoped voice normalization and reference audio ingress."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
import requests
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.application_sounds import ApplicationSoundMethods
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.schemas.sounds import ImportAudioReferenceInput
from pandrator_mcp.schemas.voice_lifecycle import (
    AuditionVoiceInput,
    ImportVoiceReferenceInput,
    PromoteVoiceDesignInput,
    PublishVoiceInput,
    _RevisionWriteInput,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.targets import TargetProfile, TargetRegistry
from pandrator_mcp.tools.sound_audio_import import import_audio_reference
from pandrator_mcp.tools.voice_lifecycle import (
    import_voice_reference,
    promote_voice_design,
)


class RecordingVoiceApplication(ApplicationVoiceMethods):
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((path, kwargs))
        return self.responses.pop(0) if self.responses else {}

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Unexpected binary request: {path}")


class RecordingSoundApplication(ApplicationSoundMethods):
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.binary_calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Unexpected JSON request: {path}")

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.binary_calls.append((path, kwargs))
        return self.responses.pop(0) if self.responses else {}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession(requests.Session):
    """Intercept prepared requests before adapters or sockets are reached."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.responses_received: list[FakeResponse] = []

    def send(
        self,
        request: requests.PreparedRequest,
        **kwargs: Any,
    ) -> requests.Response:
        self.calls.append(
            {
                "method": request.method,
                "url": request.url,
                "data": request.body,
                "headers": request.headers,
                **kwargs,
            }
        )
        response = self.responses.pop(0)
        self.responses_received.append(response)
        return cast(requests.Response, response)


def _runtime(application: Any) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


def _promote_input(workspace_id: str | None = None) -> PromoteVoiceDesignInput:
    values: dict[str, Any] = {
        "voice_id": "voice-1",
        "artifact_id": "preview-1",
        "transcript": "Hello from the preview.",
        "language": "en",
        "expected_voice_revision": 4,
        "idempotency_key": "voice:promote:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return PromoteVoiceDesignInput.model_validate(values)


def _import_voice_input(
    workspace_id: str | None = None,
    *,
    include_crop: bool = False,
) -> ImportVoiceReferenceInput:
    values: dict[str, Any] = {
        "voice_id": "voice-1",
        "artifact_id": "artifact-1",
        "transcript": "Hello from the reference.",
        "language": "en",
        "transcript_reviewed": True,
        "expected_voice_revision": 4,
        "idempotency_key": "voice:reference:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    if include_crop:
        values["start_seconds"] = 0.0
        values["end_seconds"] = 2.5
    return ImportVoiceReferenceInput.model_validate(values)


def _audio_input(
    target: str,
    workspace_id: str | None = None,
) -> ImportAudioReferenceInput:
    values: dict[str, Any] = {
        "root_name": "approved-audio",
        "relative_path": "sample.wav",
        "target": target,
        "idempotency_key": "audio:import:1",
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return ImportAudioReferenceInput.model_validate(values)


def test_normalization_scope_inputs_are_optional_strict_and_trimmed():
    cases = (
        (
            PromoteVoiceDesignInput,
            {
                "voice_id": "voice-1",
                "artifact_id": "preview-1",
                "transcript": "Hello.",
                "expected_voice_revision": 1,
                "idempotency_key": "voice:promote:1",
            },
        ),
        (
            ImportVoiceReferenceInput,
            {
                "voice_id": "voice-1",
                "artifact_id": "artifact-1",
                "expected_voice_revision": 1,
                "idempotency_key": "voice:reference:1",
            },
        ),
        (
            ImportAudioReferenceInput,
            {
                "root_name": "approved-audio",
                "relative_path": "sample.wav",
                "target": "voice_reference",
                "idempotency_key": "audio:import:1",
            },
        ),
    )
    for model, values in cases:
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

    assert "workspace_id" not in _RevisionWriteInput.model_fields
    for model in (AuditionVoiceInput,):
        assert "workspace_id" in model.model_fields
        assert "session_id" not in model.model_fields
    assert "session_id" not in PublishVoiceInput.model_fields


def test_import_audio_schema_preserves_target_and_requires_no_scope_for_sound():
    sound_input = _audio_input("sound")
    assert sound_input.target == "sound"
    assert sound_input.workspace_id is None
    voice_input = _audio_input("voice_reference", "workspace-1")
    assert voice_input.workspace_id == "workspace-1"


def test_import_voice_crop_presence_and_range_validation_remain_unchanged():
    omitted = _import_voice_input()
    explicit = _import_voice_input(include_crop=True)
    assert "start_seconds" not in omitted.model_fields_set
    assert "end_seconds" not in omitted.model_fields_set
    assert {"start_seconds", "end_seconds"} <= explicit.model_fields_set

    invalid_values = (
        {"start_seconds": True, "end_seconds": 2.0},
        {"start_seconds": 1.0, "end_seconds": 1.0},
        {"start_seconds": 2.0, "end_seconds": 1.0},
    )
    for crop in invalid_values:
        with pytest.raises(ValidationError):
            ImportVoiceReferenceInput.model_validate(
                {
                    "voice_id": "voice-1",
                    "artifact_id": "artifact-1",
                    "expected_voice_revision": 1,
                    "idempotency_key": "voice:reference:1",
                    **crop,
                }
            )


def test_promote_and_import_voice_builders_add_fixed_query_without_body_changes():
    application = RecordingVoiceApplication()
    application.promote_voice_design(
        "voice/a",
        artifact_id="preview-1",
        transcript="Hello.",
        language="en",
        expected_voice_revision=4,
        idempotency_key="voice:promote:1",
        recipe_signature="recipe-abc",
        workspace_id=" workspace-1 ",
    )
    application.import_voice_reference(
        "voice/a",
        artifact_id="artifact-1",
        transcript="Reference transcript.",
        language="en",
        transcript_reviewed=True,
        expected_voice_revision=5,
        idempotency_key="voice:reference:1",
        start_seconds=0.0,
        end_seconds=2.5,
        workspace_id="workspace-1",
    )
    assert application.calls == [
        (
            "/api/v1/voices/voice%2Fa/samples/from-preview",
            {
                "method": "POST",
                "body": {
                    "artifact_id": "preview-1",
                    "transcript": "Hello.",
                    "expected_voice_revision": 4,
                    "language": "en",
                    "recipe_signature": "recipe-abc",
                },
                "idempotency_key": "voice:promote:1",
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/voices/voice%2Fa/samples/from-artifact",
            {
                "method": "POST",
                "body": {
                    "artifact_id": "artifact-1",
                    "transcript_reviewed": True,
                    "expected_voice_revision": 5,
                    "transcript": "Reference transcript.",
                    "language": "en",
                    "start_seconds": 0.0,
                    "end_seconds": 2.5,
                },
                "idempotency_key": "voice:reference:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
    ]
    for _path, kwargs in application.calls:
        assert "workspace_id" not in kwargs["body"]
        assert "session_id" not in kwargs["body"]


def test_legacy_voice_builders_keep_scope_omitted_and_crop_presence():
    application = RecordingVoiceApplication()
    application.promote_voice_design(
        "voice-1",
        artifact_id="preview-1",
        transcript="Hello.",
        language=None,
        expected_voice_revision=2,
        idempotency_key="voice:promote:2",
    )
    application.import_voice_reference(
        "voice-1",
        artifact_id="artifact-1",
        transcript=None,
        language=None,
        transcript_reviewed=False,
        expected_voice_revision=2,
        idempotency_key="voice:reference:2",
    )
    assert application.calls == [
        (
            "/api/v1/voices/voice-1/samples/from-preview",
            {
                "method": "POST",
                "body": {
                    "artifact_id": "preview-1",
                    "transcript": "Hello.",
                    "expected_voice_revision": 2,
                },
                "idempotency_key": "voice:promote:2",
            },
        ),
        (
            "/api/v1/voices/voice-1/samples/from-artifact",
            {
                "method": "POST",
                "body": {
                    "artifact_id": "artifact-1",
                    "transcript_reviewed": False,
                    "expected_voice_revision": 2,
                },
                "idempotency_key": "voice:reference:2",
                "maximum_body_bytes": 64 * 1024,
            },
        ),
    ]


def test_voice_builders_reject_invalid_scope_and_crop_before_transport():
    invalid_roots: tuple[Any, ...] = ("", "  ", True, 12, "w" * 81)
    for root in invalid_roots:
        application = RecordingVoiceApplication()
        with pytest.raises(ValueError, match="workspace_id"):
            application.promote_voice_design(
                "voice-1",
                artifact_id="preview-1",
                transcript="Hello.",
                language=None,
                expected_voice_revision=1,
                idempotency_key="voice:promote:1",
                workspace_id=root,
            )
        with pytest.raises(ValueError, match="workspace_id"):
            application.import_voice_reference(
                "voice-1",
                artifact_id="artifact-1",
                transcript=None,
                language=None,
                transcript_reviewed=False,
                expected_voice_revision=1,
                idempotency_key="voice:reference:1",
                workspace_id=root,
            )
        assert application.calls == []

    application = RecordingVoiceApplication()
    with pytest.raises(ValueError, match="start_seconds"):
        application.import_voice_reference(
            "voice-1",
            artifact_id="artifact-1",
            transcript=None,
            language=None,
            transcript_reviewed=False,
            expected_voice_revision=1,
            idempotency_key="voice:reference:1",
            start_seconds=float("nan"),
        )
    assert application.calls == []


def test_promoted_ready_reference_projects_backend_scope_and_reused_shape():
    application = Mock()
    application.promote_voice_design.return_value = {
        "status": "ready",
        "sample_id": "sample-1",
        "artifact_id": "artifact-1",
        "sample_sha256": "a" * 64,
        "voice_revision": 5,
        "reused_reference": True,
        "workspace_id": "backend-workspace",
        "can_edit": True,
        "credential": "secret-sentinel",
        "path": "/private/reference.wav",
        "backend_extension": "secret-sentinel",
    }
    outcome = promote_voice_design(_runtime(application), _promote_input("requested-workspace"))

    application.promote_voice_design.assert_called_once_with(
        "voice-1",
        artifact_id="preview-1",
        transcript="Hello from the preview.",
        language="en",
        expected_voice_revision=4,
        idempotency_key="voice:promote:1",
        workspace_id="requested-workspace",
    )
    assert outcome.result == {
        "status": "ready",
        "sample_id": "sample-1",
        "artifact_id": "artifact-1",
        "sample_sha256": "a" * 64,
        "voice_revision": 5,
        "reused_reference": True,
        "workspace_id": "backend-workspace",
        "can_edit": True,
    }
    assert outcome.work is None
    assert outcome.next_actions[0].tool == "pandrator_get_voice_samples"
    assert outcome.next_actions[0].arguments == {
        "voice_id": "voice-1",
        "workspace_id": "backend-workspace",
    }
    assert "secret-sentinel" not in str(outcome.result)


@pytest.mark.parametrize("operation", ("promote", "import"))
@pytest.mark.parametrize(
    ("backend_workspace", "requested_workspace", "expected_workspace"),
    (
        ("backend-workspace", "requested-workspace", "backend-workspace"),
        (None, "requested-workspace", "requested-workspace"),
        (None, None, None),
    ),
)
def test_queued_voice_normalization_jobs_keep_work_id_and_scope_fallback(
    operation: str,
    backend_workspace: str | None,
    requested_workspace: str | None,
    expected_workspace: str | None,
):
    payload: dict[str, Any] = {
        "id": "job-1",
        "state": "queued",
        "sample_id": "sample-1",
        "artifact_id": "artifact-1",
        "path": "/private/sample.wav",
        "credentials": {"token": "secret-sentinel"},
        "backend_extension": "secret-sentinel",
    }
    if backend_workspace is not None:
        payload.update({"workspace_id": backend_workspace, "can_edit": True})
    application = Mock()
    if operation == "promote":
        application.promote_voice_design.return_value = payload
        outcome = promote_voice_design(_runtime(application), _promote_input(requested_workspace))
        call = application.promote_voice_design.call_args.kwargs
    else:
        application.import_voice_reference.return_value = payload
        outcome = import_voice_reference(
            _runtime(application), _import_voice_input(requested_workspace, include_crop=True)
        )
        call = application.import_voice_reference.call_args.kwargs
        assert call["start_seconds"] == 0.0
        assert call["end_seconds"] == 2.5

    method = (
        application.promote_voice_design
        if operation == "promote"
        else application.import_voice_reference
    )
    method.assert_called_once()
    if requested_workspace is None:
        assert "workspace_id" not in call
    else:
        assert call["workspace_id"] == requested_workspace

    assert outcome.work is not None
    assert outcome.work.id == "job-1"
    assert outcome.next_actions[0].tool == "pandrator_get_work"
    assert outcome.next_actions[0].arguments == {"work_id": "job-1", "work_type": "job"}
    sample_action = outcome.next_actions[1]
    assert sample_action.tool == "pandrator_get_voice_samples"
    expected_arguments = {"voice_id": "voice-1"}
    if expected_workspace is not None:
        expected_arguments["workspace_id"] = expected_workspace
    assert sample_action.arguments == expected_arguments
    if backend_workspace is None:
        assert "workspace_id" not in outcome.result
        assert "can_edit" not in outcome.result
    else:
        assert outcome.result["workspace_id"] == backend_workspace
        assert outcome.result["can_edit"] is True
    assert "secret-sentinel" not in str(outcome.result)


def test_raw_binary_voice_reference_query_and_sound_scope_rejection():
    application = RecordingSoundApplication(
        {"artifact_id": "artifact-1", "sha256": "b" * 64, "size_bytes": 11}
    )
    application.import_audio_reference(
        target="voice_reference",
        filename="reference.wav",
        body=b"raw-audio",
        idempotency_key="audio:voice:1",
        workspace_id=" workspace-1 ",
    )
    assert application.binary_calls == [
        (
            "/api/v1/voices/reference-audio/import",
            {
                "method": "PUT",
                "body": b"raw-audio",
                "content_type": "application/octet-stream",
                "extra_headers": {
                    "X-Audio-Filename": "reference.wav",
                    "Idempotency-Key": "audio:voice:1",
                },
                "parameters": {"workspace_id": "workspace-1"},
            },
        )
    ]

    with pytest.raises(ValueError, match="only supported for voice_reference"):
        application.import_audio_reference(
            target="sound",
            filename="sound.wav",
            body=b"raw-audio",
            idempotency_key="audio:sound:1",
            workspace_id="workspace-1",
        )
    assert len(application.binary_calls) == 1

    invalid_roots: tuple[Any, ...] = ("", "   ", True, 7, "w" * 81)
    for invalid in invalid_roots:
        invalid_application = RecordingSoundApplication()
        with pytest.raises(ValueError, match="workspace_id"):
            invalid_application.import_audio_reference(
                target="voice_reference",
                filename="reference.wav",
                body=b"raw-audio",
                idempotency_key="audio:voice:2",
                workspace_id=invalid,
            )
        assert invalid_application.binary_calls == []


def test_direct_raw_audio_client_keeps_sound_route_unqualified():
    application = RecordingSoundApplication({"id": "candidate-1", "status": "queued"})
    application.import_audio_reference(
        target="sound",
        filename="sound.wav",
        body=b"raw-audio",
        idempotency_key="audio:sound:1",
    )
    assert application.binary_calls == [
        (
            "/api/v1/sounds/import",
            {
                "method": "PUT",
                "body": b"raw-audio",
                "content_type": "application/octet-stream",
                "extra_headers": {
                    "X-Audio-Filename": "sound.wav",
                    "Idempotency-Key": "audio:sound:1",
                },
            },
        )
    ]


def test_sound_target_workspace_is_rejected_before_approved_file_read(monkeypatch):
    import pandrator_mcp.tools.sound_audio_import as tool_module

    def reject_read(*_args: Any, **_kwargs: Any) -> tuple[bytes, str]:
        raise AssertionError("approved audio read must not occur for a scoped sound target")

    monkeypatch.setattr(tool_module, "_read_approved_audio", reject_read)
    application = RecordingSoundApplication()
    with pytest.raises(PandratorMcpError) as caught:
        import_audio_reference(_runtime(application), _audio_input("sound", "workspace-1"))
    assert caught.value.code == "validation_error"
    assert application.binary_calls == []


def test_raw_voice_reference_result_uses_only_backend_scope(monkeypatch):
    import pandrator_mcp.tools.sound_audio_import as tool_module

    monkeypatch.setattr(
        tool_module,
        "_read_approved_audio",
        lambda *_args, **_kwargs: (b"raw-audio", "reference.wav"),
    )
    application = RecordingSoundApplication(
        {
            "artifact_id": "artifact-1",
            "sha256": "a" * 64,
            "size_bytes": 9,
            "workspace_id": "backend-workspace",
            "can_edit": True,
            "path": "/private/reference.wav",
            "credential": "secret-sentinel",
            "backend_extension": "secret-sentinel",
        }
    )
    outcome = import_audio_reference(
        _runtime(application), _audio_input("voice_reference", "requested-workspace")
    )
    assert application.binary_calls[0][0] == "/api/v1/voices/reference-audio/import"
    assert application.binary_calls[0][1]["parameters"] == {
        "workspace_id": "requested-workspace"
    }
    assert outcome.result == {
        "artifact_id": "artifact-1",
        "sha256": "a" * 64,
        "size_bytes": 9,
        "workspace_id": "backend-workspace",
    }
    assert "requested-workspace" not in str(outcome.result)
    assert "secret-sentinel" not in str(outcome.result)

    legacy_application = RecordingSoundApplication(
        {"artifact_id": "artifact-old", "sha256": "c" * 64, "size_bytes": 5}
    )
    legacy_outcome = import_audio_reference(
        _runtime(legacy_application), _audio_input("voice_reference", "requested-workspace")
    )
    assert legacy_outcome.result == {
        "artifact_id": "artifact-old",
        "sha256": "c" * 64,
        "size_bytes": 5,
    }


def test_sound_candidate_projection_remains_unqualified(monkeypatch):
    import pandrator_mcp.tools.sound_audio_import as tool_module

    monkeypatch.setattr(
        tool_module,
        "_read_approved_audio",
        lambda *_args, **_kwargs: (b"raw-audio", "sound.wav"),
    )
    application = RecordingSoundApplication(
        {
            "id": "candidate-1",
            "status": "queued",
            "artifact_id": "artifact-1",
            "workspace_id": "should-not-project",
            "path": "/private/sound.wav",
            "secret": "secret-sentinel",
        }
    )
    outcome = import_audio_reference(_runtime(application), _audio_input("sound"))
    assert outcome.result == {
        "candidate": {
            "id": "candidate-1",
            "status": "queued",
            "artifact_id": "artifact-1",
        }
    }
    assert "parameters" not in application.binary_calls[0][1]
    assert "workspace_id" not in str(outcome.result)
    assert "secret-sentinel" not in str(outcome.result)


def _local_registry(origin: str) -> TargetRegistry:
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace="C:/Pandrator",
    )
    return TargetRegistry(
        [profile],
        network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
        local_discovery=lambda _profile: (origin, "manager-id"),
    )


def test_binary_http_transport_preserves_query_through_single_local_401_retry():
    origin = "http://127.0.0.1:8097"
    responses = [FakeResponse(401, {"error": "expired"}), FakeResponse(201, {"artifact_id": "a"})]
    session = FakeSession(responses)
    bootstrap_origins: list[str] = []

    def bootstrap(target: Any, _session: requests.Session) -> str:
        bootstrap_origins.append(target.application.origin)
        return f"csrf-{len(bootstrap_origins)}"

    client = ApplicationClient(
        _local_registry(origin).bind("local"),
        CredentialResolver(()),
        session=session,
        local_bootstrap=bootstrap,
    )
    body = b"voice-reference-bytes"
    response = client.import_audio_reference(
        target="voice_reference",
        filename="Reference sample.WAV",
        body=body,
        idempotency_key="audio:voice:retry1",
        workspace_id=" workspace-1 ",
    )

    assert response == {"artifact_id": "a"}
    assert len(session.calls) == 2
    assert bootstrap_origins == [origin, origin]
    for index, call in enumerate(session.calls, start=1):
        assert call["method"] == "PUT"
        assert call["url"].split("?", 1)[0] == (
            f"{origin}/api/v1/voices/reference-audio/import"
        )
        assert call["url"].endswith("?workspace_id=workspace-1")
        assert call["data"] == body
        assert call["headers"]["X-Audio-Filename"] == "Reference%20sample.WAV"
        assert call["headers"]["Idempotency-Key"] == "audio:voice:retry1"
        assert call["headers"]["X-CSRF-Token"] == f"csrf-{index}"
        assert call["allow_redirects"] is False
        assert call["stream"] is True
    assert len(session.responses_received) == 2
    assert all(response.closed for response in session.responses_received)


def test_binary_http_legacy_call_omits_params_argument():
    origin = "http://127.0.0.1:8097"
    session = FakeSession([FakeResponse(201, {"id": "candidate-1"})])
    client = ApplicationClient(
        _local_registry(origin).bind("local"),
        CredentialResolver(()),
        session=session,
        local_bootstrap=lambda _target, _session: "csrf-value",
    )
    client.import_audio_reference(
        target="sound",
        filename="bell.wav",
        body=b"sound-bytes",
        idempotency_key="audio:sound:legacy1",
    )
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == f"{origin}/api/v1/sounds/import"
    assert "?" not in session.calls[0]["url"]


def _registered_tools():
    server = build_server(cast(McpRuntime, SimpleNamespace()))

    async def list_tools():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            return await client.list_tools()

    return {tool.name: tool for tool in asyncio.run(list_tools()).tools}


def _string_schema_branch(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "string":
        return schema
    return next(branch for branch in schema["anyOf"] if branch.get("type") == "string")


def test_registered_normalization_and_raw_audio_tool_schemas_keep_contracts():
    tools = _registered_tools()
    expected_fields = {
        "pandrator_promote_voice_design": "PromoteVoiceDesignInput",
        "pandrator_import_voice_reference": "ImportVoiceReferenceInput",
        "pandrator_import_audio_reference": "ImportAudioReferenceInput",
    }
    for name, model_name in expected_fields.items():
        tool = tools[name]
        properties = tool.input_schema["properties"]
        root_schema = _string_schema_branch(properties["workspace_id"])
        assert root_schema["minLength"] == 1
        assert root_schema["maxLength"] == 80
        assert "workspace_id" not in tool.input_schema.get("required", [])
        assert "session_id" not in properties
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        if name != "pandrator_import_audio_reference":
            action = ACTION_CATALOG.get(name)
            assert action.input_model == model_name
            assert action.risk is RiskClass.WRITE
            assert action.required_scope == "app.write"
            assert action.method == "POST"
            assert action.requires_idempotency is True
    promote = ACTION_CATALOG.get("pandrator_promote_voice_design")
    assert promote.path == "/api/v1/voices/{voiceId}/samples/from-preview"
    imported = ACTION_CATALOG.get("pandrator_import_voice_reference")
    assert imported.path == "/api/v1/voices/{voiceId}/samples/from-artifact"
    audio = ACTION_CATALOG.get("pandrator_import_audio_reference")
    assert audio.input_model == "ImportAudioReferenceInput"
    assert audio.risk is RiskClass.WRITE
    assert audio.required_scope == "app.write"
    assert audio.method == "PUT"
    assert audio.requires_idempotency is True
