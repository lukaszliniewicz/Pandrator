"""Workspace-selector contracts for sound MCP inputs and request builders."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.application_sound_validation import workspace_parameters
from pandrator_mcp.clients.application_sounds import ApplicationSoundMethods
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.schemas.sounds import (
    GenerateSoundInput,
    ImportAudioReferenceInput,
    SoundLibraryInput,
)
from pandrator_mcp.targets import TargetProfile, TargetRegistry
from pandrator_mcp.tools.sound_audio_import import import_audio_reference
from pandrator_mcp.tools.sounds import generate_sound, sound_library

_SCOPE = 'team "North"/part&100%?'


class _RecordingSoundClient(ApplicationSoundMethods):
    def __init__(self) -> None:
        self.json_calls: list[tuple[str, dict[str, Any]]] = []
        self.binary_calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.json_calls.append((path, kwargs))
        return {"candidate_id": "candidate-1", "status": "queued"}

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.binary_calls.append((path, kwargs))
        return {}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self.body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class _FakeSession(requests.Session):
    """Capture prepared requests before they can reach an adapter or socket."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.responses_received: list[_FakeResponse] = []

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


def _runtime(application: Any) -> Any:
    return SimpleNamespace(require_application=lambda: application)


def _library_input(action: str, *, workspace_id: str | None) -> SoundLibraryInput:
    values: dict[str, Any] = {"action": action}
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    action_values: dict[str, dict[str, Any]] = {
        "list": {},
        "get": {"item_id": "item-1"},
        "candidates": {},
        "candidate": {"candidate_id": "candidate-1"},
        "save": {
            "candidate_id": "candidate-1",
            "name": "Bell",
            "idempotency_key": "sound-save-001",
        },
        "update": {
            "item_id": "item-1",
            "expected_revision": 2,
            "changes": {"archived": True},
            "idempotency_key": "sound-update-001",
        },
        "discard": {
            "candidate_id": "candidate-1",
            "idempotency_key": "sound-discard-001",
        },
    }
    values.update(action_values[action])
    return SoundLibraryInput.model_validate(values)


def _application_client(session: _FakeSession) -> ApplicationClient:
    origin = "http://127.0.0.1:8097"
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace="C:/Pandrator",
    )
    registry = TargetRegistry(
        [profile],
        network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
        local_discovery=lambda _profile: (origin, "manager-id"),
    )
    return ApplicationClient(
        registry.bind("local"),
        CredentialResolver(()),
        session=session,
        local_bootstrap=lambda _target, _session: "csrf-test",
    )


@pytest.mark.parametrize("model", [GenerateSoundInput, SoundLibraryInput])
@pytest.mark.parametrize("invalid", [42, "", "   ", " padded ", "x" * 81])
def test_new_sound_scopes_require_a_strict_normalized_id(model, invalid):
    with pytest.raises(ValidationError):
        if model is GenerateSoundInput:
            model.model_validate(
                {"prompt": "bell", "idempotency_key": "sound-gen-001", "workspace_id": invalid}
            )
        else:
            model.model_validate({"action": "list", "workspace_id": invalid})


def test_scope_is_allowed_for_each_library_action_and_existing_restrictions_remain():
    for action in ("list", "get", "candidates", "candidate", "save", "update", "discard"):
        assert _library_input(action, workspace_id=_SCOPE).workspace_id == _SCOPE

    with pytest.raises(ValidationError, match="not valid"):
        SoundLibraryInput.model_validate(
            {"action": "list", "item_id": "item-1", "workspace_id": _SCOPE}
        )
    action_values: dict[str, dict[str, Any]] = {
        "list": {},
        "get": {"item_id": "item-1"},
        "candidates": {},
        "candidate": {"candidate_id": "candidate-1"},
        "save": {"candidate_id": "candidate-1", "name": "Bell"},
        "update": {
            "item_id": "item-1",
            "expected_revision": 2,
            "changes": {"archived": True},
        },
        "discard": {"candidate_id": "candidate-1"},
    }
    for action in ("list", "get", "candidates", "candidate"):
        values = {"action": action, **action_values[action]}
        assert "idempotency_key" not in SoundLibraryInput.model_validate(values).model_fields_set
    for action in ("save", "update", "discard"):
        values = {"action": action, **action_values[action]}
        with pytest.raises(ValidationError, match="requires"):
            SoundLibraryInput.model_validate(values)


@pytest.mark.parametrize("target", ["sound", "voice_reference"])
def test_audio_import_scope_keeps_existing_input_normalization_for_both_targets(target):
    arguments = ImportAudioReferenceInput.model_validate(
        {
            "root_name": "approved",
            "relative_path": "audio/ref.wav",
            "target": target,
            "idempotency_key": "audio-import-001",
            "workspace_id": " workspace-1 ",
        }
    )
    assert arguments.workspace_id == "workspace-1"


def test_workspace_parameter_helper_is_strict_and_does_not_rewrite_values():
    assert workspace_parameters(None) == {}
    assert workspace_parameters(_SCOPE) == {"workspace_id": _SCOPE}
    for invalid in (7, "", " ", " padded ", "x" * 81):
        with pytest.raises(ValueError):
            workspace_parameters(invalid)
    client = _RecordingSoundClient()
    with pytest.raises(ValueError):
        client.list_sounds(workspace_id=" padded ")
    assert client.json_calls == []


def test_client_builders_scope_generation_and_all_library_methods_as_query_only():
    client = _RecordingSoundClient()
    client.generate_sound(
        prompt="bell",
        idempotency_key="sound-gen-001",
        workspace_id=_SCOPE,
    )
    client.list_sounds(workspace_id=_SCOPE)
    client.get_sound("item-1", workspace_id=_SCOPE)
    client.list_sound_candidates(workspace_id=_SCOPE)
    client.get_sound_candidate("candidate-1", workspace_id=_SCOPE)
    client.save_sound(
        candidate_id="candidate-1",
        name="Bell",
        idempotency_key="sound-save-001",
        workspace_id=_SCOPE,
    )
    client.update_sound(
        "item-1",
        expected_revision=2,
        changes={"archived": True},
        idempotency_key="sound-update-001",
        workspace_id=_SCOPE,
    )
    client.discard_sound_candidate(
        "candidate-1",
        idempotency_key="sound-discard-001",
        workspace_id=_SCOPE,
    )

    assert len(client.json_calls) == 8
    for _path, kwargs in client.json_calls:
        assert kwargs["parameters"]["workspace_id"] == _SCOPE
        assert kwargs.get("body", {}).get("workspace_id") is None

    client.import_audio_reference(
        target="sound",
        filename="sound.wav",
        body=b"sound-bytes",
        idempotency_key="audio-sound-001",
        workspace_id=f" {_SCOPE} ",
    )
    client.import_audio_reference(
        target="voice_reference",
        filename="reference.wav",
        body=b"voice-bytes",
        idempotency_key="audio-voice-001",
        workspace_id=_SCOPE,
    )
    assert [path for path, _kwargs in client.binary_calls] == [
        "/api/v1/sounds/import",
        "/api/v1/voices/reference-audio/import",
    ]
    for _path, kwargs in client.binary_calls:
        assert kwargs["parameters"] == {"workspace_id": _SCOPE}
        assert kwargs["body"]


def test_unscoped_client_builders_keep_legacy_routes_bodies_headers_and_kwargs():
    client = _RecordingSoundClient()
    client.generate_sound(prompt="bell", idempotency_key="sound-gen-001")
    client.list_sounds()
    client.get_sound("item/a")
    client.list_sound_candidates()
    client.get_sound_candidate("candidate/a")
    client.save_sound(
        candidate_id="candidate-1", name="Bell", idempotency_key="sound-save-001"
    )
    client.update_sound(
        "item-1",
        expected_revision=2,
        changes={"archived": True},
        idempotency_key="sound-update-001",
    )
    client.discard_sound_candidate("candidate-1", idempotency_key="sound-discard-001")
    client.import_audio_reference(
        target="sound",
        filename="sound.wav",
        body=b"sound-bytes",
        idempotency_key="audio-sound-001",
    )

    assert client.json_calls == [
        (
            "/api/v1/sounds/generate",
            {
                "method": "POST",
                "body": {
                    "prompt": "bell",
                    "model_id": "stable_audio_3_small_sfx_q8_0",
                },
                "idempotency_key": "sound-gen-001",
                "maximum_body_bytes": 512 * 1024,
            },
        ),
        (
            "/api/v1/sounds",
            {"parameters": {"query": "", "limit": 40, "archived": "false"}},
        ),
        ("/api/v1/sounds/item%2Fa", {"parameters": {"limit": 20}}),
        ("/api/v1/sounds/candidates", {"parameters": {"limit": 20}}),
        ("/api/v1/sounds/candidates/candidate%2Fa", {}),
        (
            "/api/v1/sounds/save",
            {
                "method": "POST",
                "body": {"candidate_id": "candidate-1", "name": "Bell"},
                "idempotency_key": "sound-save-001",
                "maximum_body_bytes": 16 * 1024,
            },
        ),
        (
            "/api/v1/sounds/item-1",
            {
                "method": "PATCH",
                "body": {"expected_revision": 2, "archived": True},
                "idempotency_key": "sound-update-001",
                "maximum_body_bytes": 16 * 1024,
            },
        ),
        (
            "/api/v1/sounds/candidates/candidate-1",
            {"method": "DELETE", "idempotency_key": "sound-discard-001"},
        ),
    ]
    assert client.binary_calls == [
        (
            "/api/v1/sounds/import",
            {
                "method": "PUT",
                "body": b"sound-bytes",
                "content_type": "application/octet-stream",
                "extra_headers": {
                    "X-Audio-Filename": "sound.wav",
                    "Idempotency-Key": "audio-sound-001",
                },
            },
        )
    ]


def test_tool_handlers_forward_scope_only_when_present_for_generation_and_each_action():
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def handler(name: str):
        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, args, kwargs))
            if name == "generate_sound":
                return {
                    "candidate_id": "candidate-1",
                    "status": "queued",
                    "workspace_id": "must-not-project",
                    "secret": "secret-sentinel",
                }
            return {
                "workspace_id": "must-not-project",
                "secret": "secret-sentinel",
                "path": "/private/sound.wav",
            }

        return invoke

    methods = (
        "generate_sound",
        "list_sounds",
        "get_sound",
        "list_sound_candidates",
        "get_sound_candidate",
        "save_sound",
        "update_sound",
        "discard_sound_candidate",
    )
    application = SimpleNamespace(**{name: handler(name) for name in methods})
    runtime = _runtime(application)
    generate_arguments = GenerateSoundInput.model_validate(
        {
            "prompt": "bell",
            "idempotency_key": "sound-gen-001",
            "workspace_id": _SCOPE,
        }
    )
    generated = generate_sound(runtime, generate_arguments)
    assert "workspace_id" not in str(generated.result)
    assert "secret-sentinel" not in str(generated.result)
    assert calls[-1][0] == "generate_sound"
    assert calls[-1][2]["workspace_id"] == _SCOPE

    for action in ("list", "get", "candidates", "candidate", "save", "update", "discard"):
        outcome = sound_library(runtime, _library_input(action, workspace_id=_SCOPE))
        assert calls[-1][0] == {
            "list": "list_sounds",
            "get": "get_sound",
            "candidates": "list_sound_candidates",
            "candidate": "get_sound_candidate",
            "save": "save_sound",
            "update": "update_sound",
            "discard": "discard_sound_candidate",
        }[action]
        assert calls[-1][2]["workspace_id"] == _SCOPE
        assert "secret-sentinel" not in str(outcome)

    generate_sound(
        runtime,
        GenerateSoundInput.model_validate(
            {"prompt": "bell", "idempotency_key": "sound-gen-002"}
        ),
    )
    assert "workspace_id" not in calls[-1][2]
    for action in ("list", "get", "candidates", "candidate", "save", "update", "discard"):
        sound_library(runtime, _library_input(action, workspace_id=None))
        assert "workspace_id" not in calls[-1][2]


@pytest.mark.parametrize("target", ["sound", "voice_reference"])
def test_binary_tool_forwards_scope_and_keeps_target_projection(monkeypatch, target):
    from pandrator_mcp.tools import sound_audio_import as tool_module

    source_reads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tool_module,
        "_read_approved_audio",
        lambda _runtime, *, root_name, relative_path: (
            source_reads.append((root_name, relative_path)) or (b"audio", "clip.wav")
        ),
    )
    calls: list[dict[str, Any]] = []

    def import_method(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if target == "sound":
            return {
                "id": "candidate-1",
                "status": "queued",
                "workspace_id": "must-not-project",
                "path": "/private/clip.wav",
                "secret": "secret-sentinel",
            }
        return {
            "artifact_id": "artifact-1",
            "sha256": "a" * 64,
            "size_bytes": 5,
            "workspace_id": "backend-workspace",
            "can_edit": True,
            "path": "/private/clip.wav",
            "secret": "secret-sentinel",
        }

    runtime = _runtime(SimpleNamespace(import_audio_reference=import_method))
    arguments = ImportAudioReferenceInput.model_validate(
        {
            "root_name": "approved",
            "relative_path": "clip.wav",
            "target": target,
            "idempotency_key": "audio-import-001",
            "workspace_id": " workspace-1 ",
        }
    )
    outcome = import_audio_reference(runtime, arguments)
    assert calls[0]["workspace_id"] == "workspace-1"
    assert calls[0]["body"] == b"audio"
    assert source_reads == [("approved", "clip.wav")]
    if target == "sound":
        assert outcome.result == {
            "candidate": {"id": "candidate-1", "status": "queued"}
        }
    else:
        assert outcome.result == {
            "artifact_id": "artifact-1",
            "sha256": "a" * 64,
            "size_bytes": 5,
            "workspace_id": "backend-workspace",
        }
    assert "secret-sentinel" not in str(outcome.result)

    unscoped_arguments = ImportAudioReferenceInput.model_validate(
        {
            "root_name": "approved",
            "relative_path": "clip.wav",
            "target": target,
            "idempotency_key": "audio-import-002",
        }
    )
    import_audio_reference(runtime, unscoped_arguments)
    assert "workspace_id" not in calls[1]


def test_invalid_binary_scope_fails_schema_before_approved_source_read(monkeypatch):
    from pandrator_mcp.tools import sound_audio_import as tool_module

    reads: list[bool] = []
    monkeypatch.setattr(
        tool_module,
        "_read_approved_audio",
        lambda *_args, **_kwargs: reads.append(True),
    )
    runtime = _runtime(SimpleNamespace(import_audio_reference=lambda **_kwargs: {}))
    with pytest.raises(ValidationError):
        arguments = ImportAudioReferenceInput.model_validate(
            {
                "root_name": "approved",
                "relative_path": "clip.wav",
                "target": "sound",
                "idempotency_key": "audio-import-001",
                "workspace_id": "   ",
            }
        )
        import_audio_reference(runtime, arguments)
    assert reads == []


def test_configured_transport_encodes_scoped_json_and_both_binary_routes():
    responses = [_FakeResponse({"ok": True}) for _ in range(3)]
    session = _FakeSession(responses)
    client = _application_client(session)

    client.generate_sound(
        prompt="bell",
        idempotency_key="sound-gen-001",
        workspace_id=_SCOPE,
    )
    client.import_audio_reference(
        target="sound",
        filename="sound.wav",
        body=b"sound-bytes",
        idempotency_key="audio-sound-001",
        workspace_id=_SCOPE,
    )
    client.import_audio_reference(
        target="voice_reference",
        filename="reference.wav",
        body=b"voice-bytes",
        idempotency_key="audio-voice-001",
        workspace_id=_SCOPE,
    )

    assert [call["method"] for call in session.calls] == ["POST", "PUT", "PUT"]
    assert [urlsplit(call["url"]).path for call in session.calls] == [
        "/api/v1/sounds/generate",
        "/api/v1/sounds/import",
        "/api/v1/voices/reference-audio/import",
    ]
    for call in session.calls:
        query = urlsplit(call["url"]).query
        assert _SCOPE not in query
        assert parse_qs(query) == {"workspace_id": [_SCOPE]}
        assert call["headers"]["X-CSRF-Token"] == "csrf-test"
    assert json.loads(session.calls[0]["data"]) == {
        "prompt": "bell",
        "model_id": "stable_audio_3_small_sfx_q8_0",
    }
    for call, body, filename, idempotency_key in zip(
        session.calls[1:],
        (b"sound-bytes", b"voice-bytes"),
        ("sound.wav", "reference.wav"),
        ("audio-sound-001", "audio-voice-001"),
        strict=True,
    ):
        assert call["data"] == body
        assert call["headers"]["X-Audio-Filename"] == filename
        assert call["headers"]["Idempotency-Key"] == idempotency_key
    assert all(response.closed for response in session.responses_received)
