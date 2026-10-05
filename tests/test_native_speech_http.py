"""Native speech request destination and Response contracts, without provider calls."""

from __future__ import annotations

import pytest
import requests

from pandrator.logic import tts_handler

CUSTOM_BASE = "http://private-provider.invalid/v1"
VOICE = "voice/with spaces?x"
VOICE_URL = "http://private-provider.invalid/v1/text-to-speech/voice%2Fwith%20spaces%3Fx"


def native_response(status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = VOICE_URL
    result._content = b"fixture audio bytes"
    return result


def elevenlabs_settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setenv("FIXTURE_NATIVE_KEY", "fixture-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fixture-key")
    return {
        "service": "ElevenLabs",
        "speaker": VOICE,
        "provider_configs": [
            {
                "id": "elevenlabs",
                "api_base": CUSTOM_BASE,
                "api_key": "fixture-key",
                "api_key_env": "FIXTURE_NATIVE_KEY",
                "adapter": "elevenlabs_native",
            }
        ],
    }


@pytest.mark.parametrize("route", ("dispatcher", "resolved", "direct"))
@pytest.mark.parametrize("status", (200, 500))
def test_custom_elevenlabs_base_survives_endpoint_resolution_and_native_dispatch(
    monkeypatch: pytest.MonkeyPatch, route: str, status: int
):
    settings = elevenlabs_settings(monkeypatch)
    result = native_response(status)
    calls: list[str] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        assert url == VOICE_URL
        assert options == {
            "headers": {
                "Accept": "audio/mpeg",
                "xi-api-key": "fixture-key",
                "Content-Type": "application/json",
            },
            "params": {"output_format": "mp3_44100_128"},
            "json": {"text": "Hello", "model_id": "eleven_multilingual_v2"},
            "timeout": 300,
        }
        calls.append(url)
        return result

    monkeypatch.setattr(tts_handler.requests, "post", post)
    if route == "dispatcher":
        returned = tts_handler._request_openai_compatible_audio("Hello", settings)
    elif route == "resolved":
        endpoint, error = tts_handler.resolve_openai_audio_endpoint(settings)
        assert endpoint is not None and not error
        assert endpoint["base_url"] == CUSTOM_BASE and "api_base" not in endpoint
        returned = tts_handler._request_elevenlabs_audio("Hello", settings, endpoint=endpoint)
    else:
        returned = tts_handler._request_elevenlabs_audio("Hello", settings)
    assert returned is result
    assert calls == [VOICE_URL]
    assert bool(returned) is (status < 400)


@pytest.mark.parametrize("resolved_base", ("", CUSTOM_BASE))
def test_direct_endpoint_retains_api_base_compatibility_and_prefers_resolved_base(
    monkeypatch: pytest.MonkeyPatch, resolved_base: str
):
    elevenlabs_settings(monkeypatch)
    expected = (
        VOICE_URL if resolved_base else "http://direct-provider.invalid/v1/text-to-speech/voice"
    )
    calls: list[str] = []
    result = native_response()

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        assert url == expected
        assert options["headers"] == {
            "Accept": "audio/mpeg",
            "xi-api-key": "fixture-key",
            "Content-Type": "application/json",
        }
        calls.append(url)
        return result

    monkeypatch.setattr(tts_handler.requests, "post", post)
    endpoint: dict[str, object] = {
        "base_url": resolved_base,
        "api_base": "http://direct-provider.invalid/v1",
        "api_key": "fixture-key",
        "default_voice": VOICE if resolved_base else "voice",
    }
    assert tts_handler._request_elevenlabs_audio("Hello", {}, endpoint=endpoint) is result
    assert calls == [expected]


AZURE_URL = "https://azure.invalid/custom/speech"
AZURE_SSML = (
    '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
    'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="de-DE">'
    '<voice xml:lang="de-DE" name="de-DE-Klaus:MAI-Voice-2">'
    '<prosody rate="+25%">Hello</prosody></voice></speak>'
)


def native_request(provider: str, monkeypatch: pytest.MonkeyPatch) -> requests.Response:
    if provider == "elevenlabs":
        return tts_handler._request_elevenlabs_audio("Hello", elevenlabs_settings(monkeypatch))
    assert provider == "azure"
    monkeypatch.setenv("FIXTURE_NATIVE_KEY", "fixture-key")
    settings = {"model": "MAI-Voice-2", "voice": "de-DE-Klaus:MAI-Voice-2", "speed": 1.25}
    endpoint: dict[str, object] = {
        "base_url": "https://azure.invalid",
        "speech_path": "/custom/speech",
        "api_key": "fixture-key",
        "api_key_env": "FIXTURE_NATIVE_KEY",
        "request_defaults": {"output_format": "fixture-format"},
    }
    return tts_handler._request_azure_speech_audio("Hello", settings, endpoint)


def expected_request(provider: str, *, timeout: int = 300) -> tuple[str, dict[str, object]]:
    if provider == "azure":
        return AZURE_URL, {
            "headers": {
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "fixture-format",
                "Ocp-Apim-Subscription-Key": "fixture-key",
            },
            "data": AZURE_SSML,
            "timeout": timeout,
        }
    assert provider == "elevenlabs"
    return VOICE_URL, {
        "headers": {
            "Accept": "audio/mpeg",
            "xi-api-key": "fixture-key",
            "Content-Type": "application/json",
        },
        "params": {"output_format": "mp3_44100_128"},
        "json": {"text": "Hello", "model_id": "eleven_multilingual_v2"},
        "timeout": timeout,
    }


@pytest.mark.parametrize("provider", ("elevenlabs", "azure"))
@pytest.mark.parametrize("status", (200, 401, 503))
def test_native_response_status_is_left_to_the_caller_without_decoding_or_retry(
    monkeypatch: pytest.MonkeyPatch, provider: str, status: int
):
    result = native_response(status)
    calls: list[str] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        assert (url, options) == expected_request(provider)
        calls.append(url)
        return result

    monkeypatch.setattr(tts_handler.requests, "post", post)
    returned = native_request(provider, monkeypatch)
    assert returned is result
    assert returned.content == b"fixture audio bytes"
    assert bool(returned) is (status < 400)
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ("elevenlabs", "azure"))
@pytest.mark.parametrize(
    "error_type",
    (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.HTTPError,
        requests.exceptions.RequestException,
        ValueError,
        RuntimeError,
    ),
)
def test_native_transport_error_class_message_cause_and_unexpected_identity(
    monkeypatch: pytest.MonkeyPatch, provider: str, error_type: type[Exception]
):
    error = error_type("fixture transport detail")
    failed_response = native_response(503)
    if isinstance(error, requests.exceptions.HTTPError):
        error.response = failed_response
    calls: list[str] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls and (url, options) == expected_request(provider)
        calls.append(url)
        raise error

    monkeypatch.setattr(tts_handler.requests, "post", post)
    expected_type = (
        RuntimeError if isinstance(error, requests.exceptions.RequestException) else error_type
    )
    with pytest.raises(expected_type) as raised:
        native_request(provider, monkeypatch)
    if isinstance(error, requests.exceptions.RequestException):
        label = "Azure Speech" if provider == "azure" else "ElevenLabs speech"
        message = (
            f"{label} request timed out."
            if isinstance(error, requests.exceptions.Timeout)
            else f"{label} request failed: fixture transport detail"
        )
        assert str(raised.value) == message
        assert raised.value.__cause__ is error
        if isinstance(error, requests.exceptions.HTTPError):
            assert error.response is failed_response and not error.response
    else:
        assert raised.value is error
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error_type",
    (requests.exceptions.Timeout, requests.exceptions.ConnectionError, ValueError, RuntimeError),
)
def test_elevenlabs_header_failures_remain_within_the_post_catch_boundary(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
):
    error = error_type("fixture header detail")
    calls: list[str] = []

    def headers(key: str, *, audio: bool = False) -> dict[str, str]:
        assert key == "fixture-key" and audio is True
        calls.append("headers")
        raise error

    def post(url: str, **options: object) -> requests.Response:
        pytest.fail("Header failure must prevent POST")

    monkeypatch.setattr(tts_handler, "_elevenlabs_auth_headers", headers)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    expected_type = (
        RuntimeError if isinstance(error, requests.exceptions.RequestException) else error_type
    )
    with pytest.raises(expected_type) as raised:
        native_request("elevenlabs", monkeypatch)
    if isinstance(error, requests.exceptions.RequestException):
        message = (
            "ElevenLabs speech request timed out."
            if isinstance(error, requests.exceptions.Timeout)
            else "ElevenLabs speech request failed: fixture header detail"
        )
        assert str(raised.value) == message and raised.value.__cause__ is error
    else:
        assert raised.value is error
    assert calls == ["headers"]


@pytest.mark.parametrize("provider", ("elevenlabs", "azure"))
@pytest.mark.parametrize("error_type", (ValueError, requests.exceptions.Timeout))
def test_request_preparation_errors_remain_outside_the_transport_catch_boundary(
    monkeypatch: pytest.MonkeyPatch, provider: str, error_type: type[Exception]
):
    error = error_type("fixture preparation detail")

    def normalize(base: str) -> str:
        assert base == CUSTOM_BASE
        raise error

    def ssml(text: str, model: str, voice: str, settings: dict) -> str:
        assert text == "Hello" and model == "MAI-Voice-2" and voice == "de-DE-Klaus:MAI-Voice-2"
        raise error

    def post(url: str, **options: object) -> requests.Response:
        pytest.fail("Preparation failure must prevent POST")

    monkeypatch.setattr(tts_handler.requests, "post", post)
    if provider == "elevenlabs":
        monkeypatch.setattr(tts_handler, "_elevenlabs_base_url", normalize)
    else:
        monkeypatch.setattr(tts_handler, "_azure_speech_ssml", ssml)
    with pytest.raises(error_type) as raised:
        native_request(provider, monkeypatch)
    assert raised.value is error


def test_post_callable_is_resolved_before_headers_while_timeout_stays_late(
    monkeypatch: pytest.MonkeyPatch,
):
    original_headers = tts_handler._elevenlabs_auth_headers
    result = native_response()
    calls: list[str] = []

    def unexpected_post(url: str, **options: object) -> requests.Response:
        pytest.fail("Header rebinding must not replace the already-resolved POST")

    def headers(key: str, *, audio: bool = False) -> dict[str, str]:
        calls.append("headers")
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 17)
        monkeypatch.setattr(tts_handler.requests, "post", unexpected_post)
        return original_headers(key, audio=audio)

    def post(url: str, **options: object) -> requests.Response:
        assert (url, options) == expected_request("elevenlabs", timeout=17)
        calls.append("post")
        return result

    monkeypatch.setattr(tts_handler, "_elevenlabs_auth_headers", headers)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    assert native_request("elevenlabs", monkeypatch) is result
    assert calls == ["headers", "post"]


def test_azure_timeout_remains_late_after_ssml_preparation(monkeypatch: pytest.MonkeyPatch):
    original_ssml = tts_handler._azure_speech_ssml
    result = native_response()
    calls: list[str] = []

    def ssml(text: str, model: str, voice: str, settings: dict) -> str:
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 19)
        calls.append("ssml")
        return original_ssml(text, model, voice, settings)

    def post(url: str, **options: object) -> requests.Response:
        assert (url, options) == expected_request("azure", timeout=19)
        calls.append("post")
        return result

    monkeypatch.setattr(tts_handler, "_azure_speech_ssml", ssml)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    assert native_request("azure", monkeypatch) is result
    assert calls == ["ssml", "post"]
