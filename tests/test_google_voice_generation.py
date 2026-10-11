"""Offline cast resolution and native requests for persisted Google voices."""

from __future__ import annotations

import base64
import io
import json
import socket
import wave
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import requests

from pandrator.logic import tts_handler
from pandrator.web.database import Database
from pandrator.web.generation_cast_runtime import (
    _binding_key,
    apply_resolved_binding,
    resolve_binding,
)
from pandrator.web.google_voice_design import google_voice_scope
from pandrator.web.models import Voice
from pandrator.web.tts_providers import TtsProviderRegistry
from tests.web_test_support import prepare_web_test_data_root

MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
TEXT = "Keep this exact transcript; do not add a neighbor's words."
VOICE = "voice_custom/CaseSensitive-VoiceID"
LOCAL_VOICE_NAME = "Saved Google narrator"
PROJECT = "fixture-project"
GEMINI_KEY_ENV = "PANDRATOR_CAST_TEST_GEMINI_KEY"
VERTEX_KEY_ENV = "PANDRATOR_CAST_TEST_VERTEX_CREDENTIAL"
VERTEX_CREDENTIAL = json.dumps(
    {
        "type": "service_account",
        "project_id": PROJECT,
        "client_email": "fixture@example.invalid",
        "private_key": "fixture-private-key-material",
    }
)
BEFORE = "Earlier, the witness entered the room."
AFTER = "Later, the narrator describes the quiet hallway."
STYLE = "A clear and steady narrator."


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Google cast generation tests must remain offline")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _credential(provider: str) -> str:
    return "fixture-gemini-key" if provider == "gemini" else VERTEX_CREDENTIAL


def _service(provider: str) -> dict[str, Any]:
    return {
        "id": provider,
        "name": provider,
        "provider": provider,
        "api_base": (
            tts_handler.GEMINI_AUDIO_BASE_URL
            if provider == "gemini"
            else "https://aiplatform.googleapis.com"
        ),
        "api_key": _credential(provider),
        "api_key_env": (
            GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
        ),
        "models": list(MODELS),
        "voices": ["Kore"],
        "default_model": MODELS[0],
        "default_voice": "Kore",
        "vertex_project": PROJECT,
        "vertex_location": "global",
    }


def _settings(provider: str, model: str) -> dict[str, Any]:
    return {
        "service": provider,
        "xtts_model": model,
        "speaker": "Kore",
        "language": "en",
        "generation_prompt": STYLE,
        "tts_context_mode": "both",
        "_semantic_context": {"before": BEFORE, "after": AFTER},
        "provider_configs": [_service(provider)],
    }


def _config_and_credential(
    settings: dict[str, Any], provider: str, config_cache: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    config = tts_handler.get_service_config(settings, provider, config_cache)
    assert config is not None
    credential = tts_handler._resolve_openai_audio_api_key(config)
    if credential == tts_handler.XTTS_OPENAI_PLACEHOLDER_API_KEY:
        credential = ""
    return config, credential


def _save_voice(
    database: Database,
    provider: str,
    scope_fingerprint: str | None,
    *,
    resource_kind: str = "designed_voice",
) -> str:
    registration: dict[str, Any] = {
        "status": "ready",
        "resource_kind": resource_kind,
        "voice_id": VOICE,
        "provider_voice_id": VOICE,
    }
    if scope_fingerprint is not None:
        registration["scope_fingerprint"] = scope_fingerprint
    with database.session() as session:
        voice = Voice(
            name=LOCAL_VOICE_NAME,
            language="en",
            description="A saved provider voice.",
            metadata_json={"providers": {provider: registration}},
        )
        session.add(voice)
        session.flush()
        return voice.id


def _voice_scope(settings: dict[str, Any], provider: str) -> dict[str, str]:
    config, credential = _config_and_credential(settings, provider, {})
    return google_voice_scope(provider, config, credential)


def _database(tmp_path: Path) -> Database:
    paths = prepare_web_test_data_root(tmp_path)
    return Database(paths.database)


def _resolve_stored_voice(
    database: Database,
    voice_id: str,
    settings: dict[str, Any],
    provider: str,
) -> tuple[dict[str, Any], dict[str, Any], TtsProviderRegistry]:
    registry = TtsProviderRegistry()
    binding = {"voice_id": voice_id, "service": provider}
    with database.session() as session:
        resolved = resolve_binding(
            session,
            binding,
            settings,
            _service_config_cache={},
            _registry=registry,
        )
    applied = apply_resolved_binding(
        binding,
        settings,
        {_binding_key(binding): resolved},
    )
    return resolved, applied, registry


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00\x40\x00\xc0\xff\x00\x00")
    return output.getvalue()


def _response(provider: str, url: str, audio: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.headers["Content-Type"] = "application/json"
    encoded = base64.b64encode(audio).decode("ascii")
    if provider == "gemini":
        payload = {
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {"type": "audio", "mime_type": "audio/wav", "data": encoded}
                    ],
                }
            ],
        }
    else:
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/wav",
                                    "data": encoded,
                                }
                            }
                        ]
                    }
                }
            ]
        }
    response._content = json.dumps(payload).encode("utf-8")
    return response


def _vertex_url(model: str) -> str:
    return (
        "https://aiplatform.googleapis.com/v1/projects/"
        f"{quote(PROJECT, safe='')}/locations/global/publishers/google/models/"
        f"{model}:generateContent"
    )


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
@pytest.mark.parametrize("model", MODELS)
def test_stored_voice_binding_reaches_native_request_with_provider_id_and_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
) -> None:
    key_env = GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
    monkeypatch.setenv(key_env, _credential(provider))
    settings = _settings(provider, model)
    before = deepcopy(settings)
    database = _database(tmp_path)
    config, credential = _config_and_credential(settings, provider, {})
    scope = google_voice_scope(provider, config, credential)
    local_voice_id = _save_voice(database, provider, scope["fingerprint"])
    calls: list[tuple[str, dict[str, Any]]] = []
    sdk_calls: list[bool] = []
    expected_url = (
        "https://generativelanguage.googleapis.com/v1beta/interactions"
        if provider == "gemini"
        else _vertex_url(model)
    )
    audio = _wav_bytes()

    def post(url: str, **options: Any) -> requests.Response:
        calls.append((url, options))
        return _response(provider, url, audio)

    def no_sdk(*_args: object, **_kwargs: object) -> requests.Response:
        sdk_calls.append(True)
        raise AssertionError("Structured Gemini 3.8 must use the native request")

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler, "_request_litellm_audio", no_sdk)
    token_calls: list[dict[str, object]] = []
    if provider == "vertex_ai":
        def access_token(service: dict[str, object]) -> tuple[str, str]:
            token_calls.append(service)
            assert service["vertex_project"] == PROJECT
            return "fixture-vertex-token", PROJECT

        monkeypatch.setattr(tts_handler, "_vertex_access_token", access_token)

    resolved, applied, registry = _resolve_stored_voice(
        database, local_voice_id, settings, provider
    )
    try:
        assert resolved["voice"] == VOICE
        assert resolved["voice_id"] == local_voice_id
        assert applied["voice"] == VOICE
        assert applied["speaker"] == VOICE
        assert applied["_cast_voice_id"] == local_voice_id

        result = (
            tts_handler._request_openai_compatible_audio(TEXT, applied)
            if provider == "gemini"
            else tts_handler._request_vertex_ai_audio(TEXT, applied)
        )
        assert result.content == audio
        assert result.headers["Content-Type"] == "audio/wav"
        assert result.content.count(b"RIFF") == 1
        assert sdk_calls == []
        assert len(calls) == 1
        url, options = calls[0]
        assert url == expected_url
        assert options["timeout"] == tts_handler.TTS_GENERATION_TIMEOUT_SECONDS
        if provider == "gemini":
            assert options["headers"] == {
                "x-goog-api-key": "fixture-gemini-key",
                "Content-Type": "application/json",
            }
            body = options["json"]
            assert body["model"] == model
            content = body["input"][0]["content"]
            assert content[0]["type"] == "text"
            assert content[0]["text"] == TEXT
            assert BEFORE not in content[0]["text"]
            assert AFTER not in content[0]["text"]
            speech_style = content[0]["annotations"][0]["style"]
            assert VOICE == body["generation_config"]["speech_config"][0]["voice"]
            assert body["response_format"] == {
                "type": "audio",
                "mime_type": "audio/wav",
            }
        else:
            assert options["headers"] == {
                "Authorization": "Bearer fixture-vertex-token",
                "Content-Type": "application/json",
            }
            body = options["json"]
            content = body["contents"][0]["parts"]
            assert content[0]["text"] == TEXT
            assert BEFORE not in content[0]["text"]
            assert AFTER not in content[0]["text"]
            speech_style = content[0]["speechMetadata"]["style"]
            assert (
                body["generationConfig"]["speechConfig"]["voiceConfig"]["voice"]
                == VOICE
            )
            assert len(token_calls) == 1
        assert STYLE in speech_style
        assert BEFORE in speech_style
        assert AFTER in speech_style
        assert settings == before
    finally:
        registry.close()
        database.dispose()


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_changed_google_key_or_project_blocks_saved_voice_before_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env = GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
    monkeypatch.setenv(key_env, _credential(provider))
    settings = _settings(provider, MODELS[0])
    config, credential = _config_and_credential(settings, provider, {})
    scope = google_voice_scope(provider, config, credential)
    database = _database(tmp_path)
    local_voice_id = _save_voice(database, provider, scope["fingerprint"])
    synthesis_calls: list[bool] = []

    def blocked_synthesis(*_args: object, **_kwargs: object) -> None:
        synthesis_calls.append(True)
        raise AssertionError("Scope validation must finish before synthesis")

    monkeypatch.setattr(tts_handler.requests, "post", blocked_synthesis)
    rotated_credential: str | None = None
    if provider == "gemini":
        rotated_credential = "rotated-fixture-gemini-key"
        monkeypatch.setenv(key_env, rotated_credential)
    else:
        settings["provider_configs"][0]["vertex_project"] = "changed-project"

    registry = TtsProviderRegistry()
    try:
        with database.session() as session, pytest.raises(
            ValueError, match="changed since this voice was designed"
        ) as error:
            resolve_binding(
                session,
                {"voice_id": local_voice_id, "service": provider},
                settings,
                _service_config_cache={},
                _registry=registry,
            )
        if rotated_credential is not None:
            assert rotated_credential not in str(error.value)
        assert synthesis_calls == []
    finally:
        registry.close()
        database.dispose()


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_designed_google_voice_rejects_legacy_model_without_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env = GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
    monkeypatch.setenv(key_env, _credential(provider))
    settings = _settings(provider, "gemini-3.1-flash-tts-preview")
    scope = _voice_scope(settings, provider)
    database = _database(tmp_path)
    local_voice_id = _save_voice(database, provider, scope["fingerprint"])
    synthesis_calls: list[bool] = []
    monkeypatch.setattr(
        tts_handler.requests,
        "post",
        lambda *_args, **_kwargs: synthesis_calls.append(True),
    )
    registry = TtsProviderRegistry()
    try:
        with database.session() as session, pytest.raises(
            ValueError, match="require Gemini 3.8 TTS"
        ):
            resolve_binding(
                session,
                {"voice_id": local_voice_id, "service": provider},
                settings,
                _service_config_cache={},
                _registry=registry,
            )
        assert synthesis_calls == []
    finally:
        registry.close()
        database.dispose()


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_designed_google_voice_requires_saved_scope_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env = GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
    monkeypatch.setenv(key_env, _credential(provider))
    settings = _settings(provider, MODELS[0])
    database = _database(tmp_path)
    local_voice_id = _save_voice(database, provider, None)
    registry = TtsProviderRegistry()
    try:
        with database.session() as session, pytest.raises(
            ValueError, match="missing its saved scope"
        ):
            resolve_binding(
                session,
                {"voice_id": local_voice_id, "service": provider},
                settings,
                _service_config_cache={},
                _registry=registry,
            )
    finally:
        registry.close()
        database.dispose()


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_raw_and_non_designed_legacy_bindings_keep_existing_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env = GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
    monkeypatch.setenv(key_env, _credential(provider))
    settings = _settings(provider, "gemini-3.1-flash-tts-preview")
    database = _database(tmp_path)
    local_voice_id = _save_voice(
        database,
        provider,
        None,
        resource_kind="provider_voice",
    )
    registry = TtsProviderRegistry()
    try:
        with database.session() as session:
            managed = resolve_binding(
                session,
                {"voice_id": local_voice_id, "service": provider},
                settings,
                _service_config_cache={},
                _registry=registry,
            )
            direct = resolve_binding(
                session,
                {"voice": "provider/Custom_Voice"},
                settings,
                _service_config_cache={},
                _registry=registry,
            )
        assert managed["voice"] == VOICE
        assert direct["voice"] == "provider/Custom_Voice"
    finally:
        registry.close()
        database.dispose()


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_prefixed_structured_model_is_normalized_before_scope_fencing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env = GEMINI_KEY_ENV if provider == "gemini" else VERTEX_KEY_ENV
    monkeypatch.setenv(key_env, _credential(provider))
    settings = _settings(provider, f"models/{MODELS[1]}")
    scope = _voice_scope(settings, provider)
    database = _database(tmp_path)
    local_voice_id = _save_voice(database, provider, scope["fingerprint"])
    registry = TtsProviderRegistry()
    try:
        with database.session() as session:
            resolved = resolve_binding(
                session,
                {"voice_id": local_voice_id, "service": provider},
                settings,
                _service_config_cache={},
                _registry=registry,
            )
        assert resolved["voice"] == VOICE
    finally:
        registry.close()
        database.dispose()
