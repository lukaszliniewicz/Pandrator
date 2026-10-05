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
