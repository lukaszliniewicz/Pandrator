"""Request-boundary fencing for provider voice upload and deletion."""

from __future__ import annotations

import json

import pytest
import requests

from pandrator.logic import tts_handler
from pandrator.web.tts_providers import LegacyTtsAdapter, TtsProviderRegistry

BASE_URL = "http://provider.invalid/v1"
HOST_KEY_SENTINEL = "host-environment-sentinel"
WORKSPACE_KEY = "workspace-credential-fixture"
ENVIRONMENT_CREDENTIAL_CASES = [
    ("voxcpm", "VoxCPM", "VOXCPM_API_KEY", "_resolve_voxcpm_api_key"),
    ("fishs2", "FishS2", "FISHS2_API_KEY", "_resolve_fishs2_api_key"),
    (
        "kobold_qwen",
        "Qwen3 TTS",
        "KOBOLD_QWEN_API_KEY",
        "_resolve_kobold_qwen_api_key",
    ),
]


def _response(status: int, payload: object = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode("utf-8")
    response.encoding = "utf-8"
    return response


def _wav(tmp_path, name: str = "sample.wav") -> str:
    path = tmp_path / name
    path.write_bytes(b"disposable WAV fixture")
    return str(path)


def _set_provider_key_environment(
    monkeypatch: pytest.MonkeyPatch, service_id: str, default_env: str, value: str
) -> None:
    monkeypatch.setenv(default_env, value)
    service = tts_handler.get_service_config({}, service_id) or {}
    configured_env = str(service.get("api_key_env") or "").strip()
    if configured_env:
        monkeypatch.setenv(configured_env, value)


def _registered_voice_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    service_id: str,
    service: str,
    api_key: str,
    allow_environment_credentials: bool | None,
) -> tuple[list[str], list[str]]:
    registry = TtsProviderRegistry()
    wav = _wav(tmp_path)
    upload_authorization: list[str] = []
    delete_authorization: list[str] = []

    def post(_url: str, *, headers: dict[str, str], **_options: object) -> requests.Response:
        upload_authorization.append(headers["Authorization"])
        return _response(200, {"voice_id": "credential-voice"})

    def delete(_url: str, *, headers: dict[str, str], **_options: object) -> requests.Response:
        delete_authorization.append(headers["Authorization"])
        return _response(204)

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler.requests, "delete", delete)
    options: dict[str, object] = {
        "base_url": BASE_URL,
        "service": service,
        "api_key": api_key,
    }
    if allow_environment_credentials is not None:
        options["allow_environment_credentials"] = allow_environment_credentials
    try:
        uploaded = registry.upload_voice(service_id, wav, **options)
        registry.delete_voice(service_id, uploaded, **options)
    finally:
        registry.close()

    return upload_authorization, delete_authorization


@pytest.mark.parametrize("error", [requests.ConnectionError("denied"), OSError("denied")])
def test_upload_denial_before_first_request_preserves_callback_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path, error: Exception
) -> None:
    wav = _wav(tmp_path)
    callbacks: list[str] = []
    requests_seen: list[str] = []

    def before_io() -> None:
        callbacks.append("before")
        raise error

    def post(url: str, **_options: object) -> requests.Response:
        requests_seen.append(url)
        return _response(200, {"voice_id": "unused"})

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(type(error)) as caught:
        tts_handler.upload_speaker_voice(
            wav,
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert caught.value is error
    assert callbacks == ["before"]
    assert requests_seen == []


def test_upload_validation_precedes_callback(tmp_path) -> None:
    callbacks: list[str] = []

    def before_io() -> None:
        callbacks.append("before")

    with pytest.raises(ValueError, match="WAV file not found"):
        tts_handler.upload_speaker_voice(
            str(tmp_path / "missing.wav"),
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert callbacks == []


def test_upload_revocation_before_legacy_fallback_blocks_second_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    wav = _wav(tmp_path)
    callbacks = 0
    requests_seen: list[str] = []
    denial = RuntimeError("upload permission revoked")

    def before_io() -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks == 2:
            raise denial

    def post(url: str, **_options: object) -> requests.Response:
        requests_seen.append(url)
        return _response(404, {"error": "unsupported endpoint"})

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(RuntimeError) as caught:
        tts_handler.upload_speaker_voice(
            wav,
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert caught.value is denial
    assert callbacks == 2
    assert len(requests_seen) == 1
    assert requests_seen[0].endswith("/audio/voices")


def test_legacy_upload_checks_callback_for_each_file_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    wav_paths = [_wav(tmp_path, "first.wav"), _wav(tmp_path, "second.wav")]
    callbacks = 0
    requests_seen: list[str] = []
    denial = RuntimeError("second file permission revoked")

    def before_io() -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks == 3:
            raise denial

    def post(url: str, **_options: object) -> requests.Response:
        requests_seen.append(url)
        if url.endswith("/audio/voices"):
            return _response(404, {"error": "unsupported endpoint"})
        return _response(200, {"id": "first-file-id"})

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(RuntimeError) as caught:
        tts_handler.upload_speaker_voice(
            wav_paths,
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert caught.value is denial
    assert callbacks == 3
    assert len(requests_seen) == 2
    assert requests_seen[0].endswith("/audio/voices")
    assert requests_seen[1].endswith("/files")


def test_delete_denial_before_first_request_preserves_callback_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks: list[str] = []
    requests_seen: list[str] = []
    denial = requests.ConnectionError("delete permission revoked")

    def before_io() -> None:
        callbacks.append("before")
        raise denial

    def delete(url: str, **_options: object) -> requests.Response:
        requests_seen.append(url)
        return _response(204)

    monkeypatch.setattr(tts_handler.requests, "delete", delete)
    with pytest.raises(requests.ConnectionError) as caught:
        tts_handler.delete_speaker_voice(
            "voice-id",
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert caught.value is denial
    assert callbacks == ["before"]
    assert requests_seen == []


def test_delete_denial_before_verification_get_blocks_catalogue_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = 0
    deletes: list[str] = []
    gets: list[str] = []
    denial = RuntimeError("verification permission revoked")

    def before_io() -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks == 3:
            raise denial

    def delete(url: str, **_options: object) -> requests.Response:
        deletes.append(url)
        return _response(404)

    def get(url: str, **_options: object) -> requests.Response:
        gets.append(url)
        return _response(200, {"data": []})

    monkeypatch.setattr(tts_handler.requests, "delete", delete)
    monkeypatch.setattr(tts_handler.requests, "get", get)
    with pytest.raises(RuntimeError) as caught:
        tts_handler.delete_speaker_voice(
            "voice-id",
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert caught.value is denial
    assert callbacks == 3
    assert len(deletes) == 2
    assert gets == []


def test_delete_rechecks_callback_before_each_verification_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = 0
    deletes: list[str] = []
    gets: list[str] = []
    denial = RuntimeError("second catalogue read revoked")

    def before_io() -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks == 4:
            raise denial

    def delete(url: str, **_options: object) -> requests.Response:
        deletes.append(url)
        return _response(404)

    def get(url: str, **_options: object) -> requests.Response:
        gets.append(url)
        return _response(200, {"error": "catalogue unavailable"})

    monkeypatch.setattr(tts_handler.requests, "delete", delete)
    monkeypatch.setattr(tts_handler.requests, "get", get)
    with pytest.raises(RuntimeError) as caught:
        tts_handler.delete_speaker_voice(
            "voice-id",
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_io,
        )

    assert caught.value is denial
    assert callbacks == 4
    assert len(deletes) == 2
    assert len(gets) == 1


def test_registered_legacy_adapter_threads_callback_to_both_transports(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    registry = TtsProviderRegistry()
    adapter = registry.get("chatterbox")
    assert isinstance(adapter, LegacyTtsAdapter)
    wav = _wav(tmp_path)
    events: list[str] = []

    def before_upload() -> None:
        events.append("before-upload")

    def before_delete() -> None:
        events.append("before-delete")

    def post(_url: str, **_options: object) -> requests.Response:
        events.append("post")
        return _response(200, {"voice_id": "registered-voice"})

    def delete(_url: str, **_options: object) -> requests.Response:
        events.append("delete")
        return _response(204)

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler.requests, "delete", delete)
    try:
        uploaded = registry.upload_voice(
            "chatterbox",
            wav,
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_upload,
        )
        deleted = registry.delete_voice(
            "chatterbox",
            uploaded,
            base_url=BASE_URL,
            service="Chatterbox",
            before_io=before_delete,
        )
    finally:
        registry.close()

    assert uploaded == "registered-voice"
    assert deleted is True
    assert events == ["before-upload", "post", "before-delete", "delete"]


@pytest.mark.parametrize(
    ("service_id", "service", "default_env", "resolver_name"),
    ENVIRONMENT_CREDENTIAL_CASES,
)
def test_environment_credentials_disabled_never_sends_host_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    service_id: str,
    service: str,
    default_env: str,
    resolver_name: str,
) -> None:
    _set_provider_key_environment(monkeypatch, service_id, default_env, HOST_KEY_SENTINEL)

    def fail_resolution(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("host environment credential resolver must not run")

    monkeypatch.setattr(tts_handler, resolver_name, fail_resolution)
    upload_authorization, delete_authorization = _registered_voice_roundtrip(
        monkeypatch,
        tmp_path,
        service_id=service_id,
        service=service,
        api_key="",
        allow_environment_credentials=False,
    )

    expected = f"Bearer {tts_handler.XTTS_OPENAI_PLACEHOLDER_API_KEY}"
    assert upload_authorization == [expected]
    assert delete_authorization == [expected]
    assert HOST_KEY_SENTINEL not in upload_authorization[0]
    assert HOST_KEY_SENTINEL not in delete_authorization[0]


@pytest.mark.parametrize(
    ("service_id", "service", "default_env", "resolver_name"),
    ENVIRONMENT_CREDENTIAL_CASES,
)
def test_environment_credentials_disabled_keeps_explicit_workspace_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    service_id: str,
    service: str,
    default_env: str,
    resolver_name: str,
) -> None:
    _set_provider_key_environment(monkeypatch, service_id, default_env, HOST_KEY_SENTINEL)

    def fail_resolution(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("host environment credential resolver must not run")

    monkeypatch.setattr(tts_handler, resolver_name, fail_resolution)
    upload_authorization, delete_authorization = _registered_voice_roundtrip(
        monkeypatch,
        tmp_path,
        service_id=service_id,
        service=service,
        api_key=WORKSPACE_KEY,
        allow_environment_credentials=False,
    )

    expected = f"Bearer {WORKSPACE_KEY}"
    assert upload_authorization == [expected]
    assert delete_authorization == [expected]
    assert HOST_KEY_SENTINEL not in upload_authorization[0]
    assert HOST_KEY_SENTINEL not in delete_authorization[0]


@pytest.mark.parametrize(
    ("service_id", "service", "default_env", "resolver_name"),
    ENVIRONMENT_CREDENTIAL_CASES,
)
def test_legacy_environment_credential_default_still_resolves_host_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    service_id: str,
    service: str,
    default_env: str,
    resolver_name: str,
) -> None:
    _set_provider_key_environment(monkeypatch, service_id, default_env, HOST_KEY_SENTINEL)
    original_resolver = getattr(tts_handler, resolver_name)
    resolver_calls: list[object] = []

    def observe_resolution(*args: object, **kwargs: object) -> str:
        resolver_calls.append((args, kwargs))
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(tts_handler, resolver_name, observe_resolution)
    upload_authorization, delete_authorization = _registered_voice_roundtrip(
        monkeypatch,
        tmp_path,
        service_id=service_id,
        service=service,
        api_key="",
        allow_environment_credentials=None,
    )

    expected = f"Bearer {HOST_KEY_SENTINEL}"
    assert upload_authorization == [expected]
    assert delete_authorization == [expected]
    assert len(resolver_calls) == 2


def test_environment_credential_flag_does_not_change_other_provider_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    upload_authorization, delete_authorization = _registered_voice_roundtrip(
        monkeypatch,
        tmp_path,
        service_id="chatterbox",
        service="Chatterbox",
        api_key="",
        allow_environment_credentials=False,
    )

    expected = f"Bearer {tts_handler.XTTS_OPENAI_PLACEHOLDER_API_KEY}"
    assert upload_authorization == [expected]
    assert delete_authorization == ["Bearer "]
