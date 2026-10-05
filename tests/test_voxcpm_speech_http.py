"""Finite offline VoxCPM pairing retries through root request patch seams."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import requests

from pandrator.logic import tts_handler

BASE = "http://fixture.invalid"
TEXT = "  Hello  世界 "
URLS = ("http://fixture.invalid/v1/audio/speech", "http://fixture.invalid/audio/speech")
PHRASE = "prompt_wav_path and prompt_text must both be provided or both be none"


@pytest.fixture(autouse=True)
def synthetic_provider_key_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOXCPM_API_KEY", "")


def payload() -> dict[str, object]:
    return {
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
    }


def response(status: int, body: str = "audio") -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = URLS[0]
    result.encoding = "utf-8"
    result.headers["Content-Type"] = "application/json" if status >= 400 else "audio/wav"
    if body == "pairing":
        result._content = json.dumps({"error": {"message": PHRASE}}).encode("utf-8")
    elif body == "uppercase-plaintext":
        result._content = ("Malformed JSON: " + PHRASE.upper()).encode("utf-8")
    elif body == "unrelated":
        result._content = b'{"error": {"message": "unrelated fixture validation error"}}'
    else:
        result._content = b"invalid fixture audio"
    return result


@pytest.mark.parametrize(
    ("steps", "url_indices", "hifi", "fallback_statuses", "initial_mode"),
    (
        (((422, "pairing"), (200, "audio")), (0, 0), (False, True), (200,), False),
        (((422, "pairing"), (503, "audio")), (0, 0), (False, True), (503,), False),
        (
            ((422, "pairing"), (404, "audio"), (200, "audio")),
            (0, 0, 1),
            (False, True, False),
            (404, 200),
            False,
        ),
        (
            ((422, "pairing"), (404, "audio"), (422, "pairing"), (405, "audio")),
            (0, 0, 1, 1),
            (False, True, False, True),
            (404, 405),
            False,
        ),
        (((422, "unrelated"),), (0,), (False,), (422,), False),
        (((422, "uppercase-plaintext"), (200, "audio")), (0, 0), (False, True), (200,), False),
        (((400, "pairing"),), (0,), (False,), (400,), False),
        (((422, "pairing"),), (0,), (False,), (422,), True),
        (((422, "pairing"), (422, "pairing")), (0, 0), (False, True), (422,), False),
    ),
    ids=(
        "pairing-to-success",
        "pairing-to-terminal-503",
        "pairing-to-fallback-original-payload",
        "pairing-at-each-candidate",
        "unrelated-422",
        "uppercase-plaintext-pairing",
        "phrase-on-400",
        "already-hifi",
        "second-pairing-response-is-terminal",
    ),
)
def test_pairing_response_scripts_preserve_payload_identity_and_terminal_policy(
    monkeypatch: pytest.MonkeyPatch,
    steps: tuple[tuple[int, str], ...],
    url_indices: tuple[int, ...],
    hifi: tuple[bool, ...],
    fallback_statuses: tuple[int, ...],
    initial_mode: bool,
) -> None:
    settings: dict[str, object] = {}
    original_settings = deepcopy(settings)
    results = [response(status, body) for status, body in steps]
    calls: list[tuple[str, dict[str, object]]] = []
    actual_payloads: list[object] = []
    detector_responses: list[requests.Response] = []
    fallback_calls: list[int] = []
    warnings: list[tuple[str, tuple[object, ...]]] = []
    detect = tts_handler._is_voxcpm_prompt_pairing_error
    should_try_next = tts_handler._should_try_next_openai_candidate
    expected_payload = payload()
    if initial_mode:
        expected_payload["mode"] = " hIfI "

        def already_hifi_payload(
            text: str, request_settings: dict[str, object]
        ) -> dict[str, object]:
            assert text == TEXT
            assert request_settings is settings
            return expected_payload

        monkeypatch.setattr(tts_handler, "_build_voxcpm_payload", already_hifi_payload)

    def post(url: str, **options: object) -> requests.Response:
        assert len(calls) < len(results)
        actual_payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        return results[len(calls) - 1]

    def detector(result: requests.Response) -> bool:
        detector_responses.append(result)
        return detect(result)

    def fallback(status: int) -> bool:
        fallback_calls.append(status)
        return should_try_next(status)

    def warning(message: str, *arguments: object) -> None:
        warnings.append((message, deepcopy(arguments)))

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler, "_is_voxcpm_prompt_pairing_error", detector)
    monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", fallback)
    monkeypatch.setattr(tts_handler.logging, "warning", warning)
    returned = tts_handler._request_voxcpm_audio(TEXT, settings, BASE)

    assert returned is results[-1]
    assert bool(returned) is (steps[-1][0] < 400)
    assert all(not result for result in results if result.status_code >= 400)
    expected_calls: list[tuple[str, dict[str, object]]] = []
    for index, is_hifi in enumerate(hifi):
        expected = deepcopy(expected_payload)
        if is_hifi:
            expected["mode"] = "hifi"
        expected_calls.append(
            (
                URLS[url_indices[index]],
                {
                    "headers": {"Authorization": "Bearer sk-placeholder"},
                    "json": expected,
                    "timeout": 300,
                },
            )
        )
        if is_hifi:
            current = actual_payloads[index]
            original = actual_payloads[0]
            assert isinstance(current, dict) and isinstance(original, dict)
            assert current is not original
            assert current["voxcpm"] is original["voxcpm"]
            assert all(current is not earlier for earlier in actual_payloads[:index])
        else:
            assert actual_payloads[index] is actual_payloads[0]
    assert calls == expected_calls
    assert detector_responses == [
        results[index] for index, is_hifi in enumerate(hifi) if not is_hifi
    ]
    assert all(
        actual is results[index]
        for actual, index in zip(
            detector_responses, (i for i, flag in enumerate(hifi) if not flag), strict=True
        )
    )
    assert fallback_calls == list(fallback_statuses)
    assert warnings == [
        (
            "Retrying VoxCPM request in hifi mode after prompt pairing error for voice '%s'.",
            ("default",),
        )
    ] * sum(hifi)
    assert settings == original_settings


@pytest.mark.parametrize("error_type", (requests.Timeout, requests.ConnectionError))
def test_hifi_transport_errors_keep_identity_without_alternate_candidate(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    settings: dict[str, object] = {}
    original_settings = deepcopy(settings)
    first = response(422, "pairing")
    error = error_type("fixture hifi transport failure")
    cause = RuntimeError("fixture original cause")
    error.__cause__ = cause
    calls: list[tuple[str, dict[str, object]]] = []
    actual_payloads: list[object] = []
    fallback_calls: list[int] = []
    warnings: list[tuple[str, tuple[object, ...]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert len(calls) < 2
        actual_payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        if len(calls) == 1:
            return first
        raise error

    def fallback(status: int) -> bool:
        fallback_calls.append(status)
        raise AssertionError("Neither Response reaches fallback before the hifi exception")

    def warning(message: str, *arguments: object) -> None:
        warnings.append((message, deepcopy(arguments)))

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", fallback)
    monkeypatch.setattr(tts_handler.logging, "warning", warning)
    with pytest.raises(error_type) as raised:
        tts_handler._request_voxcpm_audio(TEXT, settings, BASE)
    assert raised.value is error
    assert str(raised.value) == "fixture hifi transport failure"
    assert raised.value.__cause__ is cause
    expected_hifi = payload()
    expected_hifi["mode"] = "hifi"
    assert calls == [
        (
            URLS[0],
            {
                "headers": {"Authorization": "Bearer sk-placeholder"},
                "json": payload(),
                "timeout": 300,
            },
        ),
        (
            URLS[0],
            {
                "headers": {"Authorization": "Bearer sk-placeholder"},
                "json": expected_hifi,
                "timeout": 300,
            },
        ),
    ]
    original, retry = actual_payloads
    assert isinstance(original, dict) and isinstance(retry, dict)
    assert retry is not original
    assert retry["voxcpm"] is original["voxcpm"]
    assert fallback_calls == []
    assert warnings == [
        (
            "Retrying VoxCPM request in hifi mode after prompt pairing error for voice '%s'.",
            ("default",),
        )
    ]
    assert settings == original_settings


def test_hifi_post_uses_late_root_bindings_and_captured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings: dict[str, object] = {
        "provider_configs": [
            {"id": "voxcpm", "api_key_env": "LOCAL_PAIRING_KEY", "api_key": "explicit-key"}
        ],
    }
    original_settings = deepcopy(settings)
    first, terminal = response(422, "pairing"), response(200)
    events: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []
    actual_payloads: list[object] = []
    detector_responses: list[requests.Response] = []
    warnings: list[tuple[str, tuple[object, ...]]] = []
    detect = tts_handler._is_voxcpm_prompt_pairing_error

    def unexpected_post(url: str, **options: object) -> requests.Response:
        raise AssertionError("The hifi POST callable must resolve before its header callback")

    def stale_detector(result: requests.Response) -> bool:
        raise AssertionError("Pairing detection must resolve after the first POST")

    def detector(result: requests.Response) -> bool:
        events.append("detector-new")
        assert result is first
        detector_responses.append(result)
        return detect(result)

    def first_headers(api_key: str = "sk-placeholder") -> dict[str, str]:
        assert api_key == "fixture-key"
        events.append("headers-1")
        return {"Authorization": "Bearer fixture-key"}

    def second_headers(api_key: str = "sk-placeholder") -> dict[str, str]:
        assert api_key == "fixture-key"
        events.append("headers-2")
        monkeypatch.setattr(tts_handler.requests, "post", unexpected_post)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 333)
        return {"Authorization": "Bearer fixture-hifi"}

    def second_post(url: str, **options: object) -> requests.Response:
        assert len(calls) == 1
        events.append("post-2")
        actual_payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        return terminal

    def first_post(url: str, **options: object) -> requests.Response:
        assert not calls
        events.append("post-1")
        actual_payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        monkeypatch.setattr(tts_handler.requests, "post", second_post)
        monkeypatch.setattr(tts_handler, "_openai_auth_headers", second_headers)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 222)
        monkeypatch.setattr(tts_handler, "_is_voxcpm_prompt_pairing_error", detector)
        monkeypatch.setenv("LOCAL_PAIRING_KEY", "changed-key")
        return first

    def fallback(status: int) -> bool:
        assert status == 200
        events.append("fallback-200")
        return False

    def warning(message: str, *arguments: object) -> None:
        events.append("warning")
        warnings.append((message, deepcopy(arguments)))

    monkeypatch.setenv("LOCAL_PAIRING_KEY", " fixture-key ")
    monkeypatch.setattr(tts_handler.requests, "post", first_post)
    monkeypatch.setattr(tts_handler, "_openai_auth_headers", first_headers)
    monkeypatch.setattr(tts_handler, "_is_voxcpm_prompt_pairing_error", stale_detector)
    monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", fallback)
    monkeypatch.setattr(tts_handler.logging, "warning", warning)
    assert tts_handler._request_voxcpm_audio(TEXT, settings, BASE) is terminal
    assert events == [
        "headers-1",
        "post-1",
        "detector-new",
        "warning",
        "headers-2",
        "post-2",
        "fallback-200",
    ]
    expected_hifi = payload()
    expected_hifi["mode"] = "hifi"
    assert calls == [
        (
            URLS[0],
            {"headers": {"Authorization": "Bearer fixture-key"}, "json": payload(), "timeout": 300},
        ),
        (
            URLS[0],
            {
                "headers": {"Authorization": "Bearer fixture-hifi"},
                "json": expected_hifi,
                "timeout": 333,
            },
        ),
    ]
    original, retry = actual_payloads
    assert isinstance(original, dict) and isinstance(retry, dict)
    assert retry is not original
    assert retry["voxcpm"] is original["voxcpm"]
    assert len(detector_responses) == 1 and detector_responses[0] is first
    assert warnings == [
        (
            "Retrying VoxCPM request in hifi mode after prompt pairing error for voice '%s'.",
            ("default",),
        )
    ]
    assert settings == original_settings
