"""Real native dispatch, Response status/cause handling, retry and WAV decoding."""

from __future__ import annotations

import copy
import io
import json
import struct
import wave
from threading import Event

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import retry_utils, tts_handler

PCM = struct.pack("<hhhh", 0, 64, -64, 0) * 40


def wav_bytes() -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(PCM)
        return buffer.getvalue()


def response(status: int = 200, *, audio: bytes | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = "https://fixture.invalid/speech"
    result.encoding = "utf-8"
    if status >= 400:
        result.headers.update({"Content-Type": "application/json", "Retry-After": "2"})
        result._content = json.dumps({"detail": "fixture provider failure"}).encode()
    else:
        result.headers["Content-Type"] = "audio/wav"
        result._content = wav_bytes() if audio is None else audio
    return result


def settings(provider: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setenv("FIXTURE_CALLER_KEY", "fixture-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fixture-key")
    if provider == "elevenlabs":
        return {
            "service": "ElevenLabs",
            "speaker": "voice/fixture",
            "provider_configs": [
                {
                    "id": "elevenlabs",
                    "adapter": "elevenlabs_native",
                    "api_base": "https://elevenlabs.invalid/v1",
                    "api_key": "fixture-key",
                    "api_key_env": "FIXTURE_CALLER_KEY",
                }
            ],
        }
    assert provider == "azure"
    return {
        "service": tts_handler.OPENAI_COMPAT_SERVICE,
        "openai_audio_endpoint": "azure-fixture",
        "model": "MAI-Voice-2",
        "voice": "de-DE-Klaus:MAI-Voice-2",
        "provider_configs": [
            {
                "id": "azure-fixture",
                "provider": "azure",
                "adapter": "azure_speech",
                "api_base": "https://azure.invalid",
                "speech_path": "/custom/speech",
                "api_key": "fixture-key",
                "api_key_env": "FIXTURE_CALLER_KEY",
            }
        ],
    }


def script(mode: str) -> list[requests.Response | Exception]:
    if mode in {"cancel_before_request", "cancel_in_wait"}:
        return [] if mode == "cancel_before_request" else [response(503)]
    if mode == "success":
        return [response()]
    if mode == "response_retry_then_success":
        return [response(503), response()]
    if mode == "response_retry_exhausted":
        return [response(503), response(503)]
    if mode == "response_nonretryable":
        return [response(401)]
    if mode in {"wrapped_retry_then_success", "wrapped_nonretryable"}:
        status = 503 if mode == "wrapped_retry_then_success" else 401
        error = requests.exceptions.HTTPError("fixture transport HTTP", response=response(status))
        return [error, response()] if status == 503 else [error]
    if mode == "empty_audio_retry_exhausted":
        return [response(audio=b""), response(audio=b"")]
    assert mode == "json_instead_of_audio_retry_exhausted"
    results: list[requests.Response | Exception] = []
    for _ in range(2):
        result = response()
        result.headers["Content-Type"] = "application/json"
        result._content = json.dumps({"detail": "fixture provider failure"}).encode()
        results.append(result)
    return results


@pytest.mark.parametrize("provider", ("elevenlabs", "azure"))
@pytest.mark.parametrize(
    "mode",
    (
        "success",
        "response_retry_then_success",
        "response_retry_exhausted",
        "response_nonretryable",
        "wrapped_retry_then_success",
        "wrapped_nonretryable",
        "cancel_before_request",
        "cancel_in_wait",
        "empty_audio_retry_exhausted",
        "json_instead_of_audio_retry_exhausted",
    ),
)
def test_native_caller_uses_real_status_retry_cause_cancellation_and_wav_decoder(
    monkeypatch: pytest.MonkeyPatch, provider: str, mode: str
):
    request_settings = settings(provider, monkeypatch)
    original_settings = copy.deepcopy(request_settings)
    steps = script(mode)
    calls: list[str] = []
    waits: list[float] = []
    retries: list[tuple[int, int, float]] = []
    cancel = Event()
    if mode == "cancel_before_request":
        cancel.set()

    def post(url: str, **options: object) -> requests.Response:
        index = len(calls)
        assert index < len(steps), "Unexpected additional native synthesis attempt"
        expected_url = (
            "https://elevenlabs.invalid/v1/text-to-speech/voice%2Ffixture"
            if provider == "elevenlabs"
            else "https://azure.invalid/custom/speech"
        )
        assert url == expected_url
        assert options["timeout"] == 300
        headers = options["headers"]
        assert isinstance(headers, dict)
        assert (
            headers["xi-api-key" if provider == "elevenlabs" else "Ocp-Apim-Subscription-Key"]
            == "fixture-key"
        )
        if provider == "elevenlabs":
            assert options["json"] == {"text": "Hello", "model_id": "eleven_multilingual_v2"}
            assert "data" not in options
        else:
            assert "json" not in options
            assert isinstance(options["data"], str) and ">Hello</prosody>" in options["data"]
        calls.append(url)
        result = steps[index]
        if isinstance(result, Exception):
            raise result
        return result

    def wait(delay: float, event: Event) -> bool:
        assert event is cancel
        waits.append(delay)
        if mode == "cancel_in_wait":
            cancel.set()
            return False
        return True

    def retry(attempt: int, maximum: int, delay: float) -> None:
        retries.append((attempt, maximum, delay))

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler, "wait_for_retry", wait)
    monkeypatch.setattr(retry_utils.random, "uniform", lambda _low, _high: 0.0)
    succeeds = mode in {"success", "response_retry_then_success", "wrapped_retry_then_success"}
    cancels = mode in {"cancel_before_request", "cancel_in_wait"}
    if succeeds or cancels:
        audio = tts_handler.text_to_audio(
            "  Hello\n ",
            request_settings,
            max_attempts=2,
            cancel_event=cancel,
            retry_callback=retry,
        )
        if cancels:
            assert audio is None
        else:
            assert isinstance(audio, AudioSegment)
            assert (audio.channels, audio.sample_width, audio.frame_rate) == (1, 2, 16000)
            assert audio.raw_data == PCM and len(audio) == 10
    else:
        with pytest.raises(tts_handler.TtsGenerationError) as raised:
            tts_handler.text_to_audio(
                "  Hello\n ",
                request_settings,
                max_attempts=2,
                cancel_event=cancel,
                retry_callback=retry,
            )
        assert raised.value.retryable is ("nonretryable" not in mode)
        assert f"after {len(steps)} attempt(s)" in str(raised.value)
        cause = raised.value.__cause__
        if mode.startswith("response_"):
            assert isinstance(cause, requests.exceptions.HTTPError)
            assert cause.response is steps[-1]
        elif mode == "wrapped_nonretryable":
            assert isinstance(cause, RuntimeError) and cause.__cause__ is steps[0]
        else:
            assert isinstance(cause, RuntimeError)
        status = (
            401 if "nonretryable" in mode else (503 if mode == "response_retry_exhausted" else 0)
        )
        assert tts_handler.status_code_from_error(raised.value) == status
    assert len(calls) == len(steps)
    expected_delay = 0.5 if "audio_retry_exhausted" in mode else 2.0
    retries_once = len(steps) == 2 or mode == "cancel_in_wait"
    assert waits == ([expected_delay] if retries_once else [])
    assert retries == ([(2, 2, expected_delay)] if retries_once else [])
    assert request_settings == original_settings
