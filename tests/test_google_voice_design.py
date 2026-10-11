"""Offline contracts for persistent native Google prompted voices."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import threading
import wave
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import google.auth
import pytest
from google.auth.exceptions import DefaultCredentialsError
from pydantic import ValidationError
from sqlalchemy import select

from pandrator.logic import tts_handler
from pandrator.runtime import DataPaths
from pandrator.web import google_voice_design
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import (
    TTS_SERVICE_ENVS,
    database_reference,
    tts_service_credential_key,
)
from pandrator.web.database import Database
from pandrator.web.google_voice_design import (
    configured_google_design_service,
    design_google_voice,
    google_voice_scope,
)
from pandrator.web.google_voice_design_schemas import GoogleVoiceDesignCreate
from pandrator.web.models import AppSetting, Artifact, Job, Voice, VoiceSample
from pandrator.web.tts_providers import TtsProviderRegistry
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflow_voice import VoiceWorkflowContext
from tests.web_test_support import prepare_web_test_data_root

MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
VERTEX_PROJECT = "project/tenant one"
VERTEX_TOKEN = "fixture-vertex-token"
REMOTE_VOICE_ID = "voice_fixture_123"
PRIVATE_CREDENTIAL = "fixture-private-google-key"
PREVIEW_WARNING = (
    "Google created the voice, but its preview audio could not be saved."
)
UNCERTAIN_WARNING = (
    "Google voice creation could not be confirmed. Refresh Google voices before creating another voice."
)
QUOTA_WARNING = (
    "Google rejected voice creation because a quota or rate limit was reached (HTTP 429). "
    "Check the project's voice-creation quota before creating another voice."
)


def _service_config(service_id: str, key_env: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "id": service_id,
        "provider": service_id,
        "adapter": service_id,
        "name": service_id,
        "kind": "commercial",
        "api_base": (
            tts_handler.GEMINI_AUDIO_BASE_URL
            if service_id == "gemini"
            else "https://aiplatform.googleapis.com"
        ),
        "api_key_env": key_env,
        "models": list(MODELS),
        "default_model": MODELS[0],
        "default_voice": "Kore",
        "voices": ["Kore"],
    }
    if service_id == "vertex_ai":
        config["vertex_project"] = VERTEX_PROJECT
        config["vertex_location"] = "us-central1"
    return config


def _write_setting(
    database: Database,
    key: str,
    value: dict[str, Any],
) -> None:
    with database.session() as session:
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value_json=deepcopy(value), revision=1))
        else:
            row.value_json = deepcopy(value)
            row.revision += 1


def _configure_service(
    database: Database,
    service_id: str,
    *,
    credential: str | None = None,
    key_env: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    variable = key_env or f"PANDRATOR_TEST_{service_id.upper()}_CREDENTIAL"
    secret = credential or (
        PRIVATE_CREDENTIAL
        if service_id == "gemini"
        else json.dumps(
            {
                "project_id": "json-project",
                "client_email": "fixture@example.invalid",
                "private_key": "fixture-private-key-material",
            }
        )
    )
    config = _service_config(service_id, variable)
    value = {"provider_configs": [config]}
    _write_setting(database, "defaults.tts", value)
    _write_setting(database, "services.tts", value)
    return config, variable, secret


def _wav_bytes(sample_rate: int = 16_000, samples: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples if samples is not None else b"\x01\x00" * 160)
    return output.getvalue()


def _sample_audio(data: bytes, mime_type: str) -> dict[str, str]:
    return {"data": base64.b64encode(data).decode("ascii"), "mime_type": mime_type}


def _new_queued_voice(
    database: Database,
    *,
    service_id: str,
    model: str,
    job_id: str,
    scope: dict[str, str],
    name: str = "Designed voice",
    prompt: str = "A calm, clear narrator with measured pacing.",
) -> tuple[str, dict[str, Any]]:
    voice = Voice(
        name=name,
        language="en-GB",
        description=prompt,
        metadata_json={
            "voice_category": "androgynous",
            "custom_keep": {"source": "fixture"},
            "google_voice_design": {
                "state": "queued",
                "job_id": job_id,
                "service_id": service_id,
                "model": model,
                "prompt": prompt,
                "scope_fingerprint": scope["fingerprint"],
                "project": scope["project"],
            },
        },
    )
    with database.session() as session:
        session.add(voice)
        session.flush()
        voice_id = voice.id
    payload = {
        "voice_id": voice_id,
        "service_id": service_id,
        "model": model,
        "name": name,
        "prompt": prompt,
        "language": "en-GB",
        "scope_fingerprint": scope["fingerprint"],
        "project": scope["project"],
        "job_id": job_id,
    }
    return voice_id, payload


@dataclass
class ApiFixture:
    client: Any
    csrf: str
    extension: dict[str, Any]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Google voice design tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", blocked_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_socket)


@pytest.fixture
def api_fixture(tmp_path: Path) -> Iterator[ApiFixture]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
    )
    client = app.test_client()
    bootstrap_response = client.post(
        "/api/v1/auth/bootstrap",
        json={"token": token},
    )
    assert bootstrap_response.status_code == 200
    extension = app.extensions["pandrator"]
    try:
        yield ApiFixture(
            client=client,
            csrf=bootstrap_response.get_json()["csrf_token"],
            extension=extension,
        )
    finally:
        extension["database"].dispose()


def _api_headers(fixture: ApiFixture, key: str | None = None) -> dict[str, str]:
    headers = {"X-CSRF-Token": fixture.csrf}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _request_body(
    service_id: str = "gemini",
    model: str = MODELS[0],
    *,
    name: str = "New Google voice",
) -> dict[str, str]:
    return {
        "service_id": service_id,
        "model": model,
        "name": name,
        "prompt": "A calm, clear narrator with measured pacing.",
        "language": "en-GB",
        "voice_category": "androgynous",
    }


def test_google_voice_design_schema_is_strict_and_normalizes_text() -> None:
    parsed = GoogleVoiceDesignCreate.model_validate(
        {
            **_request_body(),
            "name": "  New Google voice  ",
            "prompt": "  A measured, warm voice.  ",
        }
    )
    assert parsed.name == "New Google voice"
    assert parsed.prompt == "A measured, warm voice."
    assert parsed.language == "en-GB"
    assert parsed.voice_category == "androgynous"

    for extra in (
        {"seed": 17},
        {"sample_text": "repeat this"},
        {"api_key": "do-not-persist-this"},
        {"target_voice_id": "voice_existing"},
    ):
        with pytest.raises(ValidationError):
            GoogleVoiceDesignCreate.model_validate({**_request_body(), **extra})
    with pytest.raises(ValidationError):
        GoogleVoiceDesignCreate.model_validate({**_request_body(), "name": "  "})
    with pytest.raises(ValidationError):
        GoogleVoiceDesignCreate.model_validate({**_request_body(), "language": "en--GB"})


def test_gemini_voice_scope_hashes_api_key_without_disclosing_it() -> None:
    service = _service_config("gemini", "GEMINI_TEST_KEY")
    key = "fixture-gemini-key"
    scope = google_voice_scope("gemini", service, key)
    identity = hashlib.sha256(key.encode("utf-8")).hexdigest()
    expected = hashlib.sha256(
        json.dumps(
            ["gemini", service["api_base"], identity],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert scope == {"fingerprint": expected, "project": ""}
    assert key not in repr(scope)
    assert scope["fingerprint"] != google_voice_scope(
        "gemini", service, "rotated-gemini-key"
    )["fingerprint"]


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_native_scope_ignores_endpoint_trailing_slash(provider: str) -> None:
    service = _service_config(provider, "PANDRATOR_SCOPE_FIXTURE")
    credential = "fixture-key" if provider == "gemini" else '{"project_id":"fixture-project"}'
    original = google_voice_scope(provider, service, credential)
    assert google_voice_scope(provider, {**service, "api_base": service["api_base"] + "/"}, credential) == original


def test_vertex_scope_project_precedence_and_adc_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_config("vertex_ai", "VERTEX_TEST_KEY")
    json_credential = json.dumps({"project_id": "json-project", "private_key": "private"})

    def unexpected_default(**_options: object) -> tuple[object, str]:
        raise AssertionError("configured projects must avoid ADC discovery")

    monkeypatch.setattr(google.auth, "default", unexpected_default)
    assert google_voice_scope(
        "vertex_ai",
        {**service, "vertex_project": "override-project", "_google_project": "adc-project"},
        json_credential,
    )["project"] == "override-project"
    assert google_voice_scope(
        "vertex_ai",
        {**service, "vertex_project": "", "_google_project": "adc-project"},
        json_credential,
    )["project"] == "json-project"
    assert google_voice_scope(
        "vertex_ai",
        {**service, "vertex_project": "", "_google_project": "adc-project"},
        "",
    )["project"] == "adc-project"

    default_calls: list[dict[str, object]] = []

    class NoRefreshCredentials:
        def refresh(self, _request: object) -> None:
            raise AssertionError("scope calculation must not refresh credentials")

    def adc_default(**options: object) -> tuple[object, str]:
        default_calls.append(dict(options))
        return NoRefreshCredentials(), "detected-project"

    monkeypatch.setattr(google.auth, "default", adc_default)
    detected = google_voice_scope(
        "vertex_ai",
        {**service, "vertex_project": "", "_google_project": ""},
        "",
    )
    assert detected["project"] == "detected-project"
    assert default_calls == [
        {"scopes": ["https://www.googleapis.com/auth/cloud-platform"]}
    ]
    assert PRIVATE_CREDENTIAL not in repr(detected)
    assert VERTEX_TOKEN not in repr(detected)


def test_google_voice_scope_rejects_non_native_and_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gemini = _service_config("gemini", "GEMINI_TEST_KEY")
    with pytest.raises(ValueError, match="native Google endpoint"):
        google_voice_scope(
            "gemini", {**gemini, "api_base": f"{gemini['api_base']}/proxy"}, "fixture-key"
        )
    with pytest.raises(ValueError, match="API key") as missing_key:
        google_voice_scope("gemini", gemini, "")
    assert PRIVATE_CREDENTIAL not in str(missing_key.value)

    vertex = _service_config("vertex_ai", "VERTEX_TEST_KEY")

    def missing_adc(**_options: object) -> tuple[object, None]:
        raise DefaultCredentialsError("fixture-secret-private-key")

    monkeypatch.setattr(google.auth, "default", missing_adc)
    with pytest.raises(ValueError, match="credentials are not configured") as missing_vertex:
        google_voice_scope(
            "vertex_ai",
            {**vertex, "vertex_project": "", "_google_project": ""},
            "",
        )
    assert "fixture-secret-private-key" not in str(missing_vertex.value)

    with pytest.raises(ValueError, match="project is required"):
        google_voice_scope(
            "vertex_ai",
            {**vertex, "vertex_project": "", "_google_project": ""},
            json.dumps({"client_email": "fixture@example.invalid"}),
        )


def test_configured_google_service_reads_settings_and_resolves_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        config, key_env, secret = _configure_service(database, "gemini")
        monkeypatch.setenv(key_env, secret)
        service, credential = configured_google_design_service(
            database, paths, "gemini"
        )
        assert service["api_base"] == config["api_base"]
        assert set(MODELS).issubset(service["models"])
        assert credential == secret
        with database.session() as session:
            for key in ("defaults.tts", "services.tts"):
                row = session.get(AppSetting, key)
                assert row is not None
                assert secret not in repr(row.value_json)
                assert row.revision == 1
            assert session.get(
                AppSetting,
                "credentials.auxiliary_refs",
            ) is None
        assert database_reference(tts_service_credential_key("gemini"))
        assert TTS_SERVICE_ENVS["gemini"] == "GEMINI_API_KEY"
    finally:
        database.dispose()


@pytest.mark.parametrize("service_id", ("gemini", "vertex_ai"))
@pytest.mark.parametrize("model", MODELS)
def test_route_queues_one_voice_and_replays_before_mutable_settings(
    api_fixture: ApiFixture,
    monkeypatch: pytest.MonkeyPatch,
    service_id: str,
    model: str,
) -> None:
    database = api_fixture.extension["database"]
    _config, key_env, secret = _configure_service(database, service_id)
    monkeypatch.setenv(key_env, secret)

    def no_provider_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the route must enqueue without creating a remote voice")

    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        no_provider_call,
    )
    body = _request_body(service_id, model)
    headers = _api_headers(api_fixture, f"design-{service_id}-{model[-4:]}")
    created = api_fixture.client.post(
        "/api/v1/voices/design/google",
        json=body,
        headers=headers,
    )
    assert created.status_code == 202, created.get_json()
    receipt = created.get_json()
    assert receipt["kind"] == "voice.google_design"
    assert receipt["status"] == "queued"
    assert receipt["voice_id"]

    with database.session() as session:
        job = session.get(Job, receipt["id"])
        voice = session.get(Voice, receipt["voice_id"])
        assert job is not None
        assert voice is not None
        assert job.max_attempts == 1
        assert set(job.resource_keys_json) == {
            f"voice:{voice.id}",
            f"service:tts:{service_id}",
        }
        assert job.payload_json["job_id"] == job.id
        assert job.payload_json["model"] == model
        assert job.payload_json["project"] == (
            VERTEX_PROJECT if service_id == "vertex_ai" else ""
        )
        assert voice.description == body["prompt"]
        assert voice.metadata_json["voice_category"] == "androgynous"
        assert voice.metadata_json["google_voice_design"]["job_id"] == job.id
        assert voice.metadata_json["google_voice_design"]["state"] == "queued"
        assert secret not in repr(job.payload_json)
        assert secret not in repr(voice.metadata_json)

        voice.description = "Locally edited description"
        row = session.get(AppSetting, "services.tts")
        assert row is not None
        row.value_json = {"provider_configs": []}

    replay = api_fixture.client.post(
        "/api/v1/voices/design/google",
        json=body,
        headers=headers,
    )
    assert replay.status_code == 202, replay.get_json()
    assert replay.get_json() == receipt
    assert replay.headers["Idempotency-Replayed"] == "true"
    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "voice.google_design")) == receipt["id"]
        assert len(list(session.scalars(select(Voice).where(Voice.name == body["name"])))) == 1
        assert session.get(Voice, receipt["voice_id"]).description == "Locally edited description"


def test_route_requires_idempotency_key_and_sanitizes_invalid_request(
    api_fixture: ApiFixture,
) -> None:
    missing_key = api_fixture.client.post(
        "/api/v1/voices/design/google",
        json=_request_body(),
        headers=_api_headers(api_fixture),
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json()["error"]["code"] == "idempotency_key_required"

    secret = "fixture-inline-secret-value"
    invalid = api_fixture.client.post(
        "/api/v1/voices/design/google",
        json={**_request_body(), "api_key": secret},
        headers=_api_headers(api_fixture, "invalid-design-key"),
    )
    assert invalid.status_code == 422
    assert invalid.get_json()["error"]["code"] == "validation_error"
    assert secret not in invalid.get_data(as_text=True)


def test_route_rejects_duplicate_local_voice_name_with_conflict(
    api_fixture: ApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = api_fixture.extension["database"]
    _config, key_env, secret = _configure_service(database, "gemini")
    monkeypatch.setenv(key_env, secret)
    with database.session() as session:
        session.add(Voice(name="Existing name", language="en"))
    response = api_fixture.client.post(
        "/api/v1/voices/design/google",
        json=_request_body(name="Existing name"),
        headers=_api_headers(api_fixture, "duplicate-google-voice"),
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "already_exists"
    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "voice.google_design")) is None


def test_openapi_registers_google_voice_design_schema_and_required_header() -> None:
    from pandrator.web.openapi import build_openapi_document

    document = build_openapi_document()
    operation = document["paths"]["/api/v1/voices/design/google"]["post"]
    assert operation["operationId"] == "designGoogleVoice"
    assert "202" in operation["responses"]
    assert operation["parameters"][0]["name"] == "Idempotency-Key"
    assert operation["parameters"][0]["in"] == "header"
    assert operation["parameters"][0]["required"] is True
    assert "every caller" in operation["parameters"][0]["description"]
    assert operation["parameters"][0]["schema"] == {
        "type": "string",
        "minLength": 8,
        "maxLength": 200,
    }
    schema = document["components"]["schemas"]["GoogleVoiceDesignCreate"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["model"]["enum"] == list(MODELS)


def _worker_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_id: str,
) -> tuple[Database, DataPaths, VoiceWorkflowContext, dict[str, Any], str, str]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    config, key_env, credential = _configure_service(database, service_id)
    monkeypatch.setenv(key_env, credential)
    registry = TtsProviderRegistry()
    context = VoiceWorkflowContext(
        database=database,
        paths=paths,
        artifacts=ArtifactService(database, paths),
        tts_providers=registry,
        manager_bridge=None,
        _resolve_input=_unexpected_input,
        _session_dir=lambda key: paths.sessions / key,
        _scaled_progress_callback=lambda callback, _start, _end: callback,
    )
    return database, paths, context, config, key_env, credential


def _unexpected_input(_artifact_id: str) -> tuple[Artifact, Path]:
    raise AssertionError("Google voice design does not resolve input artifacts")


@pytest.mark.parametrize("service_id", ("gemini", "vertex_ai"))
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("sample_format", ("wav", "l16"))
def test_worker_creates_and_persists_google_voice_once_with_safe_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_id: str,
    model: str,
    sample_format: str,
) -> None:
    database, paths, context, config, key_env, credential = _worker_stack(
        tmp_path, monkeypatch, service_id
    )
    job_id = "job-fixture-0001"
    service, resolved = configured_google_design_service(database, paths, service_id)
    scope = google_voice_scope(service_id, service, resolved)
    voice_id, payload = _new_queued_voice(
        database,
        service_id=service_id,
        model=model,
        job_id=job_id,
        scope=scope,
    )
    pcm = b"\x01\x00" * 160
    if sample_format == "wav":
        sample = _sample_audio(_wav_bytes(16_000, pcm), "audio/wav")
    else:
        sample = _sample_audio(pcm, "audio/l16;rate=24000;channels=1")
    calls: list[tuple[str, dict[str, Any]]] = []

    def create_voice(endpoint: str, **options: Any) -> dict[str, Any]:
        calls.append((endpoint, options))
        assert options["vertex"] is (service_id == "vertex_ai")
        assert options["model"] == model
        assert options["display_name"] == payload["name"]
        assert options["prompt"] == payload["prompt"]
        assert options["language_code"] == payload["language"]
        if service_id == "gemini":
            assert endpoint == "https://generativelanguage.googleapis.com/v1beta/voices"
            assert options["headers"] == {"x-goog-api-key": credential}
        else:
            assert endpoint == (
                "https://aiplatform.googleapis.com/v1beta1/projects/"
                f"{quote(VERTEX_PROJECT, safe='')}/locations/global/voices"
            )
            assert options["headers"] == {
                "Authorization": f"Bearer {VERTEX_TOKEN}"
            }
        return {
            "id": REMOTE_VOICE_ID,
            "display_name": "Generated narrator",
            "expire_time": "2099-01-01T00:00:00Z",
            "usage": {
                "total_input_tokens": 91,
                "total_output_tokens": 57,
                "api_key": "must-not-be-retained",
            },
            "sample_audio": sample,
        }

    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        create_voice,
    )
    token_calls: list[dict[str, Any]] = []
    if service_id == "vertex_ai":

        def access_token(current: dict[str, Any]) -> tuple[str, str]:
            token_calls.append(current)
            return VERTEX_TOKEN, VERTEX_PROJECT

        monkeypatch.setattr(tts_handler, "_vertex_access_token", access_token)

    progress_calls: list[tuple[float, str | None]] = []
    registered_handler = WorkflowHandlers(
        database, paths, tts_providers=context.tts_providers
    ).handlers()["voice.google_design"]
    result = registered_handler(
        payload,
        lambda value, detail: progress_calls.append((value, detail)),
        threading.Event(),
    )
    assert result is not None
    assert len(calls) == 1
    assert result["voice_id"] == voice_id
    assert result["provider_voice_id"] == REMOTE_VOICE_ID
    assert result["service_id"] == service_id
    assert result["model"] == model
    assert result["usage"] == {
        "total_input_tokens": 91,
        "total_output_tokens": 57,
    }
    assert result["artifact_id"]
    assert "warning" not in result
    if service_id == "vertex_ai":
        assert len(token_calls) == 1
        assert token_calls[0]["api_key"] == credential

    with database.session() as session:
        voice = session.get(Voice, voice_id)
        artifact = session.get(Artifact, result["artifact_id"])
        assert voice is not None
        assert artifact is not None
        design = voice.metadata_json["google_voice_design"]
        provider = voice.metadata_json["providers"][service_id]
        assert voice.description == payload["prompt"]
        assert voice.language == "en-GB"
        assert voice.metadata_json["voice_category"] == "androgynous"
        assert voice.metadata_json["custom_keep"] == {"source": "fixture"}
        assert design["state"] == "created"
        assert design["provider_voice_id"] == REMOTE_VOICE_ID
        assert design["preview_artifact_id"] == artifact.id
        assert provider["status"] == "ready"
        assert provider["voice_id"] == REMOTE_VOICE_ID
        assert provider["voice_id"] != voice_id
        assert provider["provider_voice_id"] == REMOTE_VOICE_ID
        assert provider["managed_by"] == "pandrator"
        assert provider["protocol"] == "google_voices"
        assert provider["resource_kind"] == "designed_voice"
        assert provider["scope_fingerprint"] == scope["fingerprint"]
        assert provider["project"] == scope["project"]
        assert provider["models"] == list(MODELS)
        assert provider["model"] == model
        assert provider["endpoint_fingerprint"]
        assert provider["design_job_id"] == job_id
        assert provider["expire_time"] == "2099-01-01T00:00:00Z"
        assert artifact.role == "tts_voice_preview"
        assert artifact.kind == "audio"
        assert artifact.metadata_json == {
            "service_id": service_id,
            "service_adapter": config["adapter"],
            "model": model,
            "voice": REMOTE_VOICE_ID,
            "language": "en-GB",
            "preview_text": "",
            "sample_kind": "provider_generated",
            "voice_id": voice_id,
            "design_job_id": job_id,
        }
        assert session.scalar(select(VoiceSample.id).where(VoiceSample.voice_id == voice_id)) is None
        persisted_values = repr((voice.metadata_json, artifact.metadata_json))
        assert credential not in persisted_values
        assert VERTEX_TOKEN not in persisted_values
        assert "must-not-be-retained" not in persisted_values

    artifact_path = paths.root / artifact.relative_path
    expected_wav = _wav_bytes(16_000, pcm) if sample_format == "wav" else _wav_bytes(24_000, pcm)
    assert artifact_path.read_bytes() == expected_wav
    with wave.open(io.BytesIO(artifact_path.read_bytes()), "rb") as saved_audio:
        assert saved_audio.getnchannels() == 1
        assert saved_audio.getsampwidth() == 2
        assert saved_audio.getframerate() == (16_000 if sample_format == "wav" else 24_000)

    reopened = Database(paths.database)
    try:
        with reopened.session() as session:
            voice_after_reopen = session.get(Voice, voice_id)
            artifact_after_reopen = session.get(Artifact, result["artifact_id"])
            assert voice_after_reopen is not None
            assert artifact_after_reopen is not None
            assert voice_after_reopen.description == payload["prompt"]
            assert voice_after_reopen.metadata_json["voice_category"] == "androgynous"
            reopened_provider = voice_after_reopen.metadata_json["providers"][service_id]
            assert reopened_provider["voice_id"] == REMOTE_VOICE_ID
            assert reopened_provider["voice_id"] != voice_id
            assert reopened_provider["provider_voice_id"] == REMOTE_VOICE_ID
            assert artifact_after_reopen.metadata_json["voice"] == REMOTE_VOICE_ID
            assert artifact_after_reopen.metadata_json["voice_id"] == voice_id
            assert (
                artifact_after_reopen.metadata_json["voice"]
                != artifact_after_reopen.metadata_json["voice_id"]
            )
    finally:
        reopened.dispose()

    replay_payload = dict(payload)
    replay_payload.pop("job_id")
    replay_payload["_job_id"] = job_id
    replay = design_google_voice(
        context, replay_payload, lambda *_args: None, threading.Event()
    )
    assert replay == result
    assert len(calls) == 1
    if service_id == "vertex_ai":
        assert len(token_calls) == 1
    context.tts_providers.close()
    database.dispose()


def test_worker_rejects_changed_gemini_scope_before_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, paths, context, _config, key_env, _credential = _worker_stack(
        tmp_path, monkeypatch, "gemini"
    )
    service, original_credential = configured_google_design_service(
        database, paths, "gemini"
    )
    scope = google_voice_scope("gemini", service, original_credential)
    voice_id, payload = _new_queued_voice(
        database,
        service_id="gemini",
        model=MODELS[0],
        job_id="job-scope-change",
        scope=scope,
    )
    monkeypatch.setenv(key_env, "rotated-gemini-key")
    calls: list[bool] = []
    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        lambda *_args, **_kwargs: calls.append(True),
    )
    with pytest.raises(ValueError, match="changed after this design was queued"):
        design_google_voice(context, payload, lambda *_args: None, threading.Event())
    assert calls == []
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        assert voice.metadata_json["google_voice_design"]["state"] == "queued"
    context.tts_providers.close()
    database.dispose()


@pytest.mark.parametrize("failure", ("invalid_sample", "artifact_save"))
def test_worker_retains_created_id_when_preview_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    database, paths, context, _config, _key_env, _credential = _worker_stack(
        tmp_path, monkeypatch, "gemini"
    )
    service, credential = configured_google_design_service(database, paths, "gemini")
    scope = google_voice_scope("gemini", service, credential)
    voice_id, payload = _new_queued_voice(
        database,
        service_id="gemini",
        model=MODELS[1],
        job_id=f"job-preview-{failure}",
        scope=scope,
    )
    calls: list[bool] = []
    sample = (
        {"data": "not-valid-base64", "mime_type": "audio/wav"}
        if failure == "invalid_sample"
        else _sample_audio(_wav_bytes(), "audio/wav")
    )

    def create_voice(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls.append(True)
        return {"id": REMOTE_VOICE_ID, "sample_audio": sample}

    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        create_voice,
    )
    if failure == "artifact_save":
        monkeypatch.setattr(
            context.artifacts,
            "register",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture disk failure")),
        )
    result = design_google_voice(context, payload, lambda *_args: None, threading.Event())
    assert result["provider_voice_id"] == REMOTE_VOICE_ID
    assert "artifact_id" not in result
    assert result["warning"] == PREVIEW_WARNING
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        assert voice.metadata_json["google_voice_design"]["state"] == "created"
        assert voice.metadata_json["google_voice_design"]["provider_voice_id"] == REMOTE_VOICE_ID
        assert voice.metadata_json["providers"]["gemini"]["status"] == "ready"
        assert voice.metadata_json["google_voice_design"]["warning"] == PREVIEW_WARNING
        assert voice.metadata_json["providers"]["gemini"].get("preview_artifact_id") is None

    replay = design_google_voice(context, payload, lambda *_args: None, threading.Event())
    assert replay["provider_voice_id"] == REMOTE_VOICE_ID
    assert replay["warning"] == PREVIEW_WARNING
    assert len(calls) == 1
    context.tts_providers.close()
    database.dispose()


@pytest.mark.parametrize(
    "failure",
    ("timeout", "http_408", "http_503", "http_unknown", "malformed_2xx"),
)
def test_worker_marks_unknown_post_outcome_uncertain_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    database, paths, context, _config, _key_env, _credential = _worker_stack(
        tmp_path, monkeypatch, "gemini"
    )
    service, credential = configured_google_design_service(database, paths, "gemini")
    scope = google_voice_scope("gemini", service, credential)
    voice_id, payload = _new_queued_voice(
        database,
        service_id="gemini",
        model=MODELS[0],
        job_id="job-uncertain",
        scope=scope,
    )
    calls: list[bool] = []

    def failed_post(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls.append(True)
        status_by_failure = {
            "http_408": 408,
            "http_503": 503,
            "http_unknown": None,
            "malformed_2xx": 200,
        }
        if failure in status_by_failure:
            error = google_voice_design.google_tts_catalogue_http.GoogleVoiceCreationStatusError(
                status_by_failure[failure]
            )
            error.args = (
                "fixture-provider-response-body "
                "fixture-private-google-key and api_key=fixture-secret-token",
            )
            raise error
        raise google_voice_design.google_tts_catalogue_http.requests.Timeout(
            "fixture-api-key=must-not-leak"
        )

    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        failed_post,
    )
    with pytest.raises(RuntimeError, match=UNCERTAIN_WARNING) as raised:
        design_google_voice(context, payload, lambda *_args: None, threading.Event())
    assert "fixture-provider-response-body" not in str(raised.value)
    assert "fixture-private-google-key" not in str(raised.value)
    assert "fixture-secret-token" not in str(raised.value)
    assert "fixture-api-key=must-not-leak" not in str(raised.value)
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        assert voice.metadata_json["google_voice_design"]["state"] == "uncertain"
        assert voice.metadata_json["google_voice_design"]["warning"] == UNCERTAIN_WARNING
        assert "error_code" not in voice.metadata_json["google_voice_design"]
        assert "http_status" not in voice.metadata_json["google_voice_design"]
    with pytest.raises(ValueError, match="unresolved outcome"):
        design_google_voice(context, payload, lambda *_args: None, threading.Event())
    assert len(calls) == 1
    assert PRIVATE_CREDENTIAL not in UNCERTAIN_WARNING
    context.tts_providers.close()
    database.dispose()


def test_worker_persists_known_http_rejections_without_provider_details_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, paths, context, _config, _key_env, _credential = _worker_stack(
        tmp_path, monkeypatch, "gemini"
    )
    service, credential = configured_google_design_service(database, paths, "gemini")
    scope = google_voice_scope("gemini", service, credential)
    cases = (
        (
            400,
            "invalid_request",
            "Google rejected voice creation because the request is invalid (HTTP 400). "
            "Check the voice prompt, model, and language settings before retrying.",
        ),
        (
            401,
            "authentication_failed",
            "Google rejected voice creation because authentication failed (HTTP 401). "
            "Check the selected Google credential before retrying.",
        ),
        (
            403,
            "permission_denied",
            "Google rejected voice creation because the credential lacks permission "
            "(HTTP 403). Check the selected project's voice-creation permissions "
            "before retrying.",
        ),
        (
            404,
            "model_or_endpoint_unavailable",
            "Google rejected voice creation because the selected model or endpoint "
            "is unavailable (HTTP 404). Check the provider, region, and model "
            "configuration before retrying.",
        ),
        (
            422,
            "invalid_request",
            "Google rejected voice creation because the request is invalid (HTTP 422). "
            "Check the voice prompt, model, and language settings before retrying.",
        ),
        (
            499,
            "request_rejected",
            "Google rejected voice creation (HTTP 499). "
            "Check the selected provider configuration before retrying.",
        ),
    )
    current_status: list[int] = []
    calls: list[int] = []

    def rejected_post(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls.append(current_status[0])
        error = google_voice_design.google_tts_catalogue_http.GoogleVoiceCreationStatusError(
            current_status[0]
        )
        error.args = (
            "fixture-provider-response-body fixture-private-google-key "
            "api_key=fixture-secret-token",
        )
        raise error

    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        rejected_post,
    )

    for index, (status, error_code, warning) in enumerate(cases):
        voice_id, payload = _new_queued_voice(
            database,
            service_id="gemini",
            model=MODELS[0],
            job_id=f"job-rejected-{status}-{index}",
            scope=scope,
            name=f"Rejected voice {status}",
        )
        current_status[:] = [status]
        with pytest.raises(RuntimeError) as raised:
            design_google_voice(context, payload, lambda *_args: None, threading.Event())

        assert str(raised.value) == warning
        assert "fixture-provider-response-body" not in str(raised.value)
        assert "fixture-private-google-key" not in str(raised.value)
        assert "fixture-secret-token" not in str(raised.value)
        with database.session() as session:
            voice = session.get(Voice, voice_id)
            assert voice is not None
            design = voice.metadata_json["google_voice_design"]
            assert design["state"] == "rejected"
            assert design["warning"] == warning
            assert design["error_code"] == error_code
            assert design["http_status"] == status
            assert "fixture-provider-response-body" not in str(design)
            assert "fixture-private-google-key" not in str(design)
            assert "fixture-secret-token" not in str(design)

        with pytest.raises(ValueError, match="not queued for creation"):
            design_google_voice(context, payload, lambda *_args: None, threading.Event())
        assert len(calls) == index + 1

    assert calls == [status for status, _error_code, _warning in cases]
    context.tts_providers.close()
    database.dispose()


def test_worker_persists_http_429_rejection_without_identity_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, paths, context, _config, _key_env, credential = _worker_stack(
        tmp_path, monkeypatch, "gemini"
    )
    service, resolved = configured_google_design_service(database, paths, "gemini")
    scope = google_voice_scope("gemini", service, resolved)
    voice_id, payload = _new_queued_voice(
        database,
        service_id="gemini",
        model=MODELS[0],
        job_id="job-quota-rejected",
        scope=scope,
    )

    class Response:
        status_code = 429

        def json(self) -> object:
            raise AssertionError("HTTP 429 response bodies must not be parsed")

    class Session:
        def __init__(self) -> None:
            self.post_calls: list[tuple[str, dict[str, Any]]] = []
            self.close_calls = 0

        def post(self, endpoint: str, **options: Any) -> Response:
            assert endpoint == (
                "https://generativelanguage.googleapis.com/v1beta/voices"
            )
            assert options == {
                "headers": {"x-goog-api-key": credential},
                "json": {
                    "store": True,
                    "voice": {
                        "type": "prompted",
                        "model": MODELS[0],
                        "display_name": payload["name"],
                        "prompted": {"input": payload["prompt"]},
                        "language_code": payload["language"],
                    },
                },
                "timeout": 60,
                "allow_redirects": False,
            }
            self.post_calls.append((endpoint, dict(options)))
            return Response()

        def close(self) -> None:
            self.close_calls += 1

    request_session = Session()
    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http.requests,
        "Session",
        lambda: request_session,
    )

    with pytest.raises(RuntimeError) as raised:
        design_google_voice(context, payload, lambda *_args: None, threading.Event())

    assert str(raised.value) == QUOTA_WARNING
    assert credential not in str(raised.value)
    assert "generativelanguage.googleapis.com" not in str(raised.value)
    assert request_session.post_calls and len(request_session.post_calls) == 1
    assert request_session.close_calls == 1
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        design = voice.metadata_json["google_voice_design"]
        assert design["state"] == "rejected"
        assert design["warning"] == QUOTA_WARNING
        assert design["error_code"] == "quota_or_rate_limited"
        assert design["http_status"] == 429
        assert "provider_voice_id" not in design
        assert "preview_artifact_id" not in design
        assert voice.metadata_json.get("providers", {}) == {}
        assert session.scalar(
            select(Artifact.id).where(Artifact.role == "tts_voice_preview")
        ) is None
        assert session.scalar(
            select(VoiceSample.id).where(VoiceSample.voice_id == voice_id)
        ) is None

    with pytest.raises(ValueError, match="not queued for creation"):
        design_google_voice(context, payload, lambda *_args: None, threading.Event())
    assert len(request_session.post_calls) == 1
    context.tts_providers.close()
    database.dispose()


def test_worker_persists_remote_identity_when_cancelled_during_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, paths, context, _config, _key_env, _credential = _worker_stack(
        tmp_path, monkeypatch, "gemini"
    )
    service, credential = configured_google_design_service(database, paths, "gemini")
    scope = google_voice_scope("gemini", service, credential)
    voice_id, payload = _new_queued_voice(
        database,
        service_id="gemini",
        model=MODELS[0],
        job_id="job-cancelled-after-post",
        scope=scope,
    )
    cancel_event = threading.Event()

    def create_during_cancel(*_args: object, **_kwargs: object) -> dict[str, Any]:
        cancel_event.set()
        return {"id": REMOTE_VOICE_ID}

    monkeypatch.setattr(
        google_voice_design.google_tts_catalogue_http,
        "create_google_voice",
        create_during_cancel,
    )
    result = design_google_voice(
        context, payload, lambda *_args: None, cancel_event
    )
    assert result["provider_voice_id"] == REMOTE_VOICE_ID
    assert result["warning"] == PREVIEW_WARNING
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        assert voice.metadata_json["google_voice_design"]["state"] == "created"
        assert voice.metadata_json["providers"]["gemini"]["provider_voice_id"] == REMOTE_VOICE_ID
    context.tts_providers.close()
    database.dispose()
