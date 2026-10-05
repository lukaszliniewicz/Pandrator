"""Offline Response contracts for root local speech candidate requests."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import requests

from pandrator.logic import tts_handler

BASE = "http://fixture.invalid"
TEXT = "  Hello  世界 "
URLS = ("http://fixture.invalid/v1/audio/speech", "http://fixture.invalid/audio/speech")
PROVIDERS = ("magpie", "xtts", "voxcpm", "fishs2", "voxtral")


@pytest.fixture(autouse=True)
def synthetic_provider_key_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VOXCPM_API_KEY", "FISHS2_API_KEY", "VOXTRAL_API_KEY"):
        monkeypatch.setenv(name, "")


def response(status: int) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = URLS[0]
    result.headers["Content-Type"] = "audio/wav"
    result._content = b"invalid fixture audio"
    return result


def request(provider: str, settings: dict[str, object], base: str = BASE) -> requests.Response:
    if provider == "magpie":
        return tts_handler._request_magpie_audio(TEXT, settings, base)
    if provider == "xtts":
        return tts_handler._request_xtts_audio(TEXT, settings, base)
    if provider == "voxcpm":
        return tts_handler._request_voxcpm_audio(TEXT, settings, base)
    if provider == "fishs2":
        return tts_handler._request_fishs2_audio(TEXT, settings, base)
    assert provider == "voxtral"
    return tts_handler._request_voxtral_audio(TEXT, settings, base)


def default_options(provider: str, timeout: int = 300) -> dict[str, object]:
    if provider == "magpie":
        return {
            "json": {
                "model": "magpie-tts",
                "input": TEXT,
                "voice": "Magpie-Multilingual.EN-US.Aria",
                "language": None,
                "speed": 1.0,
                "use_cfg": True,
                "apply_text_normalization": False,
                "response_format": "wav",
            },
            "timeout": timeout,
        }
    if provider == "voxcpm":
        return {
            "headers": {"Authorization": "Bearer sk-placeholder"},
            "json": {
                "model": "openbmb/VoxCPM2",
                "input": TEXT,
                "voice": "default",
                "response_format": "wav",
                "speed": 1.0,
                "voxcpm": {
                    "cfg_value": 1.5,
                    "inference_timesteps": 15,
                    "normalize": False,
                    "denoise": False,
                    "retry_badcase": True,
                    "retry_badcase_max_times": 3,
                    "retry_badcase_ratio_threshold": 6.0,
                    "min_len": 2,
                    "max_len": 4096,
                },
            },
            "timeout": timeout,
        }
    if provider == "fishs2":
        return {
            "headers": {"Authorization": "Bearer sk-placeholder"},
            "json": {
                "model": "fishaudio/s2-pro",
                "input": TEXT,
                "voice": "default",
                "response_format": "wav",
                "speed": 1.0,
                "temperature": 0.7,
                "top_p": 0.7,
                "chunk_length": 200,
                "latency": "balanced",
                "normalize": True,
                "prosody": {"speed": 1.0, "volume": 0.0, "normalize_loudness": True},
            },
            "timeout": timeout,
        }
    if provider == "voxtral":
        return {
            "headers": {"Authorization": "Bearer sk-placeholder"},
            "json": {
                "model": "auto",
                "input": TEXT,
                "voice": "casual_female",
                "response_format": "wav",
                "speed": 1.0,
                "instructions": (
                    'voxtral_options:{"max_frames": 1024, "euler_steps": 8, "chunk": false, '
                    '"max_chunk_chars": 500, "chunk_silence_ms": 0, "strip_quotes": false, '
                    '"strip_diacritics": false, "level_audio": false}'
                ),
            },
            "timeout": timeout,
        }
    assert provider == "xtts"
    return {
        "headers": {"Authorization": "Bearer sk-placeholder"},
        "json": {
            "model": "tts_models/multilingual/multi-dataset/xtts_v2",
            "input": TEXT,
            "voice": "default",
            "language": "en",
            "speed": 1.0,
            "response_format": "wav",
            "instructions": '{"language": "en"}',
        },
        "timeout": timeout,
    }


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "statuses", ((200,), (401,), (429,), (503,), (404, 200), (405, 401), (501, 503), (404, 405))
)
def test_literal_default_requests_and_fallback_responses(
    monkeypatch: pytest.MonkeyPatch, provider: str, statuses: tuple[int, ...]
) -> None:
    settings: dict[str, object] = {}
    original = deepcopy(settings)
    results = [response(status) for status in statuses]
    calls: list[tuple[str, dict[str, object]]] = []
    payloads: list[object] = []

    def post(url: str, **options: object) -> requests.Response:
        assert len(calls) < len(results)
        payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        return results[len(calls) - 1]

    monkeypatch.setattr(tts_handler.requests, "post", post)
    returned = request(provider, settings)

    assert returned is results[-1]
    assert bool(returned) is (statuses[-1] < 400)
    assert calls == [(url, default_options(provider)) for url in URLS[: len(statuses)]]
    assert all(payload is payloads[0] for payload in payloads)
    assert settings == original


@pytest.mark.parametrize("provider", ("magpie", "xtts"))
def test_custom_payload_and_bounded_xtts_instruction_overrides(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings: dict[str, object] = {
        "speaker": " voice ",
        "xtts_model": " model ",
        "language": " de ",
        "speed": 1.25,
        "openai_audio_instructions": (
            '{"temp": 0.1, "temperature": 0.2, "trace": "keep", '
            '"xtts": {"temperature": 0.3, "top_k": 7, "unknown": "keep"}}'
        ),
        "xtts_send_top_p": True,
        "top_p": 0.9,
    }
    original = deepcopy(settings)
    result = response(200)
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        return result

    monkeypatch.setattr(tts_handler.requests, "post", post)
    assert request(provider, settings) is result
    actual = calls[0][1]
    if provider == "magpie":
        expected = {
            "json": {
                "model": "model",
                "input": TEXT,
                "voice": "voice",
                "language": "de",
                "speed": 1.25,
                "use_cfg": True,
                "apply_text_normalization": False,
                "response_format": "wav",
            },
            "timeout": 300,
        }
    else:
        payload = actual["json"]
        assert isinstance(payload, dict)
        instructions = payload["instructions"]
        assert isinstance(instructions, str)
        payload["instructions"] = json.loads(instructions)
        expected = {
            "headers": {"Authorization": "Bearer sk-placeholder"},
            "json": {
                "model": "model",
                "input": TEXT,
                "voice": "voice",
                "language": "de",
                "speed": 1.25,
                "response_format": "wav",
                "instructions": {
                    "trace": "keep",
                    "xtts": {"unknown": "keep", "top_p": 0.9},
                    "language": "de",
                },
            },
            "timeout": 300,
        }
    assert calls == [(URLS[0], expected)]
    assert settings == original


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("error_type", (requests.Timeout, requests.ConnectionError, RuntimeError))
def test_post_exceptions_keep_identity_message_and_cause_without_fallback(
    monkeypatch: pytest.MonkeyPatch, provider: str, error_type: type[Exception]
) -> None:
    error = error_type("fixture request failure")
    cause = RuntimeError("fixture original cause")
    error.__cause__ = cause
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        raise error

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(error_type) as raised:
        request(provider, {})
    assert raised.value is error
    assert str(raised.value) == "fixture request failure"
    assert raised.value.__cause__ is cause
    assert calls == [(URLS[0], default_options(provider))]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_empty_root_candidates_raise_without_post(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    calls: list[str] = []

    def post(url: str, **options: object) -> requests.Response:
        calls.append(url)
        raise AssertionError("POST must not run for an empty candidate sequence")

    monkeypatch.setattr(tts_handler, "_openai_audio_speech_urls", lambda base: [])
    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(RuntimeError) as raised:
        request(provider, {})
    name = {
        "magpie": "Magpie",
        "xtts": "XTTS",
        "voxcpm": "VoxCPM",
        "fishs2": "FishS2",
        "voxtral": "Voxtral",
    }[provider]
    assert str(raised.value) == f"No {name} speech endpoint could be resolved for '{BASE}'."
    assert calls == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_versioned_base_has_one_candidate_and_returns_actual_falsey_error(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    result = response(404)
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        return result

    monkeypatch.setattr(tts_handler.requests, "post", post)
    returned = request(provider, {}, "http://fixture.invalid/v1")
    assert returned is result
    assert not returned
    assert calls == [(URLS[0], default_options(provider))]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_candidate_loop_preserves_root_resolution_and_option_evaluation_order(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    events: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []
    payloads: list[object] = []
    first, terminal = response(404), response(200)

    def unexpected_post(url: str, **options: object) -> requests.Response:
        raise AssertionError("The POST callable must resolve before its header callback")

    def urls(base: str) -> list[str]:
        assert base == BASE
        events.append("urls")
        return list(URLS)

    def second_headers(api_key: str = "sk-placeholder") -> dict[str, str]:
        assert api_key == "sk-placeholder"
        events.append("headers-2")
        monkeypatch.setattr(tts_handler.requests, "post", unexpected_post)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 333)
        return {"Authorization": "Bearer fixture-second"}

    def first_headers(api_key: str = "sk-placeholder") -> dict[str, str]:
        assert api_key == "sk-placeholder"
        events.append("headers-1")
        monkeypatch.setattr(tts_handler.requests, "post", unexpected_post)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 111)
        return {"Authorization": "Bearer fixture-first"}

    def second_fallback(status: int) -> bool:
        assert status == 200
        events.append("fallback-2")
        return False

    def first_fallback(status: int) -> bool:
        assert status == 404
        events.append("fallback-1")
        monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", second_fallback)
        return True

    def second_post(url: str, **options: object) -> requests.Response:
        events.append("post-2")
        payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        return terminal

    def first_post(url: str, **options: object) -> requests.Response:
        events.append("post-1")
        payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        monkeypatch.setattr(tts_handler.requests, "post", second_post)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 222)
        if provider != "magpie":
            monkeypatch.setattr(tts_handler, "_openai_auth_headers", second_headers)
        return first

    monkeypatch.setattr(tts_handler, "_openai_audio_speech_urls", urls)
    monkeypatch.setattr(tts_handler.requests, "post", first_post)
    monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", first_fallback)
    if provider != "magpie":
        monkeypatch.setattr(tts_handler, "_openai_auth_headers", first_headers)
    assert request(provider, {}) is terminal
    assert not first
    assert payloads[0] is payloads[1]
    expected_first = default_options(provider, 111 if provider != "magpie" else 300)
    expected_second = default_options(provider, 333 if provider != "magpie" else 222)
    if provider != "magpie":
        expected_first["headers"] = {"Authorization": "Bearer fixture-first"}
        expected_second["headers"] = {"Authorization": "Bearer fixture-second"}
        assert events == [
            "urls",
            "headers-1",
            "post-1",
            "fallback-1",
            "headers-2",
            "post-2",
            "fallback-2",
        ]
    else:
        assert events == ["urls", "post-1", "fallback-1", "post-2", "fallback-2"]
    assert calls == [(URLS[0], expected_first), (URLS[1], expected_second)]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_malformed_speed_keeps_provider_specific_preparation_behavior(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings: dict[str, object] = {"speed": "bad float"}
    original = deepcopy(settings)
    result = response(200)
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        return result

    monkeypatch.setattr(tts_handler.requests, "post", post)
    if provider == "magpie":
        with pytest.raises(ValueError):
            request(provider, settings)
        assert calls == []
    else:
        assert request(provider, settings) is result
        assert calls == [(URLS[0], default_options(provider))]
    assert settings == original


@pytest.mark.parametrize("provider", ("voxcpm", "fishs2", "voxtral"))
def test_local_custom_payload_and_environment_key_precedence(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings: dict[str, object] = {
        "speaker": " demo ",
        "speed": 1.25,
        "provider_configs": [
            {"id": provider, "api_key_env": "LOCAL_PACKET_KEY", "api_key": "explicit-key"}
        ],
    }
    if provider == "voxcpm":
        settings.update({"xtts_model": "voxcpm2", "openai_audio_instructions": " custom "})
        expected_payload = {
            "model": "openbmb/VoxCPM2",
            "input": TEXT,
            "voice": "demo",
            "response_format": "wav",
            "speed": 1.25,
            "voxcpm": {
                "cfg_value": 1.5,
                "inference_timesteps": 15,
                "normalize": False,
                "denoise": False,
                "retry_badcase": True,
                "retry_badcase_max_times": 3,
                "retry_badcase_ratio_threshold": 6.0,
                "min_len": 2,
                "max_len": 4096,
            },
            "instructions": "custom",
        }
    elif provider == "fishs2":
        settings["xtts_model"] = "fish-s2"
        expected_payload = {
            "model": "fishaudio/s2-pro",
            "input": TEXT,
            "voice": "demo",
            "response_format": "wav",
            "speed": 1.25,
            "temperature": 0.7,
            "top_p": 0.7,
            "chunk_length": 200,
            "latency": "balanced",
            "normalize": True,
            "prosody": {"speed": 1.25, "volume": 0.0, "normalize_loudness": True},
        }
    else:
        settings.update(
            {
                "xtts_model": "namespace/BF16",
                "openai_audio_instructions": '{"trace": "keep", "language": "de", "max_frames": 1}',
            }
        )
        expected_payload = {
            "model": "bf16",
            "input": TEXT,
            "voice": "demo",
            "response_format": "wav",
            "speed": 1.25,
            "instructions": (
                'voxtral_options:{"trace": "keep", "max_frames": 1024, "euler_steps": 8, "chunk": false, '
                '"max_chunk_chars": 500, "chunk_silence_ms": 0, "strip_quotes": false, '
                '"strip_diacritics": false, "level_audio": false}'
            ),
        }
    original = deepcopy(settings)
    result = response(200)
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        return result

    monkeypatch.setenv("LOCAL_PACKET_KEY", " env-key ")
    monkeypatch.setattr(tts_handler.requests, "post", post)
    assert request(provider, settings) is result
    assert calls == [
        (
            URLS[0],
            {
                "headers": {"Authorization": "Bearer env-key"},
                "json": expected_payload,
                "timeout": 300,
            },
        )
    ]
    assert settings == original
