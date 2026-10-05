"""Offline native Response contracts for root prepared speech POST routes."""

from __future__ import annotations

import base64
import io
import json
import wave
from copy import deepcopy

import pytest
import requests

from pandrator.logic import tts_handler

TEXT = "Hello 世界"
BASE = "https://fixture.invalid/proxy/v1"
CUSTOM_URL = "https://fixture.invalid/custom/speech"
COMPAT_URL = "https://fixture.invalid/proxy/v1/audio/speech"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-tts-preview:generateContent"
VERTEX_URL = "https://aiplatform.googleapis.com/v1beta1/projects/project%2Ffixture/locations/region%2Ffixture/publishers/google/models/gemini-3.1-flash-tts-preview:generateContent"
PCM = b"\x00\x00\x40\x00\xc0\xff\x00\x00"
ROUTES = ("audio_cpp", "generic_json", "gemini", "vertex")


@pytest.fixture(autouse=True)
def synthetic_key_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREPARED_PACKET_KEY", "")


def native_response(status: int, *, cloud: bool = False) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = "https://response.invalid/speech"
    result.encoding = "utf-8"
    if cloud and status == 200:
        result.headers["Content-Type"] = "application/json"
        result._content = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "fixture metadata"},
                                {
                                    "inlineData": {
                                        "mimeType": "audio/L16;rate=16000;channels=1",
                                        "data": base64.b64encode(PCM).decode("ascii"),
                                    }
                                },
                            ]
                        }
                    }
                ]
            }
        ).encode("utf-8")
    elif status >= 400:
        result.headers["Content-Type"] = "application/json"
        result._content = b'{"detail":"fixture HTTP failure"}'
    else:
        result.headers["Content-Type"] = "audio/wav"
        result._content = b"invalid fixture audio"
    return result


def expected_options(route: str, timeout: int = 300) -> dict[str, object]:
    if route == "audio_cpp":
        return {
            "headers": {"Authorization": "Bearer fixture-key"},
            "json": {
                "model": "fixture-model",
                "input": TEXT,
                "voice": "fixture-voice",
            },
            "timeout": timeout,
        }
    if route == "generic_json":
        return {
            "headers": {"Authorization": "Bearer fixture-key"},
            "json": {
                "fixed": "keep",
                "prompt": TEXT,
                "model_id": "fixture-model",
                "voice_id": "fixture-voice",
                "rate": 0,
                "encoding": "wav",
            },
            "timeout": timeout,
        }
    assert route in {"gemini", "vertex"}
    headers = (
        {"x-goog-api-key": "fixture-key", "Content-Type": "application/json"}
        if route == "gemini"
        else {"Authorization": "Bearer fixture-token", "Content-Type": "application/json"}
    )
    return {
        "headers": headers,
        "json": {
            "contents": [{"role": "user", "parts": [{"text": TEXT}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
            },
        },
        "timeout": timeout,
    }


def expected_url(route: str) -> str:
    return GEMINI_URL if route == "gemini" else VERTEX_URL if route == "vertex" else CUSTOM_URL


def configure_route(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> tuple[dict[str, object], dict[str, object]]:
    if route == "vertex":
        settings: dict[str, object] = {
            "service": "Google Vertex AI",
            "model": "gemini-3.1-flash-tts-preview",
            "voice": "Kore",
            "provider_configs": [
                {
                    "id": "vertex_ai",
                    "vertex_project": "project/fixture",
                    "vertex_location": "region/fixture",
                }
            ],
        }

        def token(service: dict[str, object]) -> tuple[str, str]:
            assert service["vertex_project"] == "project/fixture"
            assert service["vertex_location"] == "region/fixture"
            return "fixture-token", "project/fixture"

        monkeypatch.setattr(tts_handler, "_vertex_access_token", token)
        return settings, {}
    settings = {"speed": 0}
    endpoint: dict[str, object] = {
        "name": "fixture",
        "adapter": route if route != "gemini" else "openai_compatible",
        "provider": "gemini" if route == "gemini" else "openai",
        "base_url": BASE,
        "speech_path": "/custom/speech",
        "default_model": "fixture-model",
        "default_voice": "fixture-voice",
        "api_key_env": "PREPARED_PACKET_KEY",
        "api_key": "fixture-key",
        "direct_http": True,
    }
    if route == "generic_json":
        endpoint.update(
            {
                "request_defaults": {"fixed": "keep", "prompt": "old"},
                "request_fields": {
                    "text": "prompt",
                    "model": "model_id",
                    "voice": "voice_id",
                    "speed": "rate",
                    "format": "encoding",
                },
            }
        )
    elif route == "gemini":
        settings = {"xtts_model": "gemini-3.1-flash-tts-preview", "speaker": "Kore"}
        endpoint.update(
            {
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "default_model": "gemini-3.1-flash-tts-preview",
                "default_voice": "Kore",
            }
        )
    else:
        assert route == "audio_cpp"

        def build(
            text: str, request_settings: dict[str, object], request_endpoint: dict[str, object]
        ) -> dict[str, object]:
            assert text == TEXT
            assert request_settings is settings
            assert request_endpoint is endpoint
            return {"model": "fixture-model", "input": TEXT, "voice": "fixture-voice"}

        monkeypatch.setattr(tts_handler, "_build_audio_cpp_audio_payload", build)

    def resolve(request_settings: dict[str, object]) -> tuple[dict[str, object], str]:
        assert request_settings is settings
        return endpoint, ""

    monkeypatch.setattr(tts_handler, "resolve_openai_audio_endpoint", resolve)
    return settings, endpoint


def request(
    route: str, settings: dict[str, object], session: requests.Session | None = None
) -> requests.Response:
    if route == "vertex":
        return tts_handler._request_vertex_ai_audio(TEXT, settings)
    return tts_handler._request_openai_compatible_audio(TEXT, settings, request_session=session)


def assert_returned(
    route: str, status: int, returned: requests.Response, posted: requests.Response
) -> None:
    if status == 200 and route in {"gemini", "vertex"}:
        assert returned is not posted
        assert returned.status_code == 200
        assert returned.url == expected_url(route)
        assert returned.headers["Content-Type"] == "audio/wav"
        with wave.open(io.BytesIO(returned.content), "rb") as audio:
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getframerate() == 16000
            assert audio.readframes(audio.getnframes()) == PCM
    else:
        assert returned is posted
        assert bool(returned) is (status < 400)


def forbidden_post(url: str, **options: object) -> requests.Response:
    raise AssertionError(f"Unexpected POST target: {url}; keyword names: {sorted(options)}")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("status", (200, 401, 503))
def test_prepared_routes_return_native_responses_or_exact_pcm_wav(
    monkeypatch: pytest.MonkeyPatch, route: str, status: int
) -> None:
    settings, endpoint = configure_route(monkeypatch, route)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    posted = native_response(status, cloud=route in {"gemini", "vertex"})
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        return posted

    monkeypatch.setattr(tts_handler.requests, "post", post)
    returned = request(route, settings)
    assert_returned(route, status, returned, posted)
    assert calls == [(expected_url(route), expected_options(route))]
    assert settings == original and endpoint == original_endpoint


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("error_type", (requests.Timeout, requests.ConnectionError, RuntimeError))
def test_prepared_transport_exceptions_keep_identity_message_cause(
    monkeypatch: pytest.MonkeyPatch, route: str, error_type: type[Exception]
) -> None:
    settings, endpoint = configure_route(monkeypatch, route)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    error = error_type("fixture prepared failure")
    cause = RuntimeError("fixture original cause")
    error.__cause__ = cause
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        raise error

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(error_type) as raised:
        request(route, settings)
    assert raised.value is error
    assert str(raised.value) == "fixture prepared failure"
    assert raised.value.__cause__ is cause
    assert calls == [(expected_url(route), expected_options(route))]
    assert settings == original and endpoint == original_endpoint


@pytest.mark.parametrize("route", ("audio_cpp", "gemini"))
@pytest.mark.parametrize("outcome", (200, 503, "timeout"))
def test_borrowed_native_session_is_selected_and_never_closed(
    monkeypatch: pytest.MonkeyPatch, route: str, outcome: int | str
) -> None:
    settings, endpoint = configure_route(monkeypatch, route)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    session = requests.Session()
    close = session.close
    close_calls: list[bool] = []
    calls: list[tuple[str, dict[str, object]]] = []
    posted = native_response(200 if outcome == "timeout" else int(outcome), cloud=route == "gemini")
    error = requests.Timeout("fixture borrowed timeout")
    cause = RuntimeError("fixture original cause")
    error.__cause__ = cause

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        if outcome == "timeout":
            raise error
        return posted

    monkeypatch.setattr(session, "post", post)
    monkeypatch.setattr(session, "close", lambda: close_calls.append(True))
    monkeypatch.setattr(tts_handler.requests, "post", forbidden_post)
    try:
        if outcome == "timeout":
            with pytest.raises(requests.Timeout) as raised:
                request(route, settings, session)
            assert raised.value is error and raised.value.__cause__ is cause
            assert str(raised.value) == "fixture borrowed timeout"
        else:
            assert_returned(route, int(outcome), request(route, settings, session), posted)
        assert calls == [(expected_url(route), expected_options(route))]
        assert close_calls == []
        assert settings == original and endpoint == original_endpoint
    finally:
        close()


class FalseySession(requests.Session):
    def __bool__(self) -> bool:
        return False


@pytest.mark.parametrize("route", ("audio_cpp", "gemini", "generic_json"))
def test_falsey_or_ignored_borrowed_session_uses_module_post_without_close(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    settings, endpoint = configure_route(monkeypatch, route)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    session = requests.Session() if route == "generic_json" else FalseySession()
    close = session.close
    close_calls: list[bool] = []
    calls: list[tuple[str, dict[str, object]]] = []
    posted = native_response(200, cloud=route == "gemini")

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        return posted

    monkeypatch.setattr(session, "post", forbidden_post)
    monkeypatch.setattr(session, "close", lambda: close_calls.append(True))
    monkeypatch.setattr(tts_handler.requests, "post", post)
    try:
        assert_returned(route, 200, request(route, settings, session), posted)
        assert calls == [(expected_url(route), expected_options(route))]
        assert close_calls == []
        assert settings == original and endpoint == original_endpoint
    finally:
        close()


@pytest.mark.parametrize(
    ("route", "borrowed"), (("audio_cpp", False), ("generic_json", False), ("audio_cpp", True))
)
def test_prepared_callable_resolves_before_url_and_options_stay_late(
    monkeypatch: pytest.MonkeyPatch, route: str, borrowed: bool
) -> None:
    settings, endpoint = configure_route(monkeypatch, route)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    events: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []
    posted = native_response(200)
    session = requests.Session() if borrowed else None
    close = session.close if session is not None else None
    close_calls: list[bool] = []

    def headers(selected_endpoint: dict[str, object]) -> dict[str, str]:
        assert selected_endpoint is endpoint
        events.append("headers")
        return {"Authorization": "Bearer rebound-key"}

    def url(base: str, path: str) -> str:
        assert base == BASE and path == "/custom/speech"
        events.append("url")
        if session is not None:
            monkeypatch.setattr(session, "post", forbidden_post)
        else:
            monkeypatch.setattr(tts_handler.requests, "post", forbidden_post)
        monkeypatch.setattr(tts_handler, "_configured_endpoint_auth_headers", headers)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 111)
        return CUSTOM_URL

    def post(actual_url: str, **options: object) -> requests.Response:
        assert not calls
        events.append("post")
        calls.append((actual_url, deepcopy(options)))
        return posted

    monkeypatch.setattr(tts_handler, "_configured_endpoint_url", url)
    monkeypatch.setattr(tts_handler.requests, "post", forbidden_post if borrowed else post)
    if session is not None:
        monkeypatch.setattr(session, "post", post)
        monkeypatch.setattr(session, "close", lambda: close_calls.append(True))
    try:
        assert request(route, settings, session) is posted
        expected = expected_options(route, 111)
        expected["headers"] = {"Authorization": "Bearer rebound-key"}
        assert calls == [(CUSTOM_URL, expected)]
        assert events == ["url", "headers", "post"]
        assert close_calls == []
        assert settings == original and endpoint == original_endpoint
    finally:
        if close is not None:
            close()


def configure_candidate(
    monkeypatch: pytest.MonkeyPatch, *, direct_http: bool = True
) -> tuple[dict[str, object], dict[str, object]]:
    settings: dict[str, object] = {}
    endpoint: dict[str, object] = {
        "name": "fixture-candidate",
        "adapter": "openai_compatible",
        "provider": "openai",
        "base_url": BASE,
        "speech_path": "/custom/speech",
        "default_model": "gpt-4o-mini-tts",
        "default_voice": "alloy",
        "api_key_env": "PREPARED_PACKET_KEY",
        "api_key": "fixture-key",
        "direct_http": direct_http,
    }

    def resolve(request_settings: dict[str, object]) -> tuple[dict[str, object], str]:
        assert request_settings is settings
        return endpoint, ""

    monkeypatch.setattr(tts_handler, "resolve_openai_audio_endpoint", resolve)
    return settings, endpoint


def candidate_options(timeout: int = 300) -> dict[str, object]:
    return {
        "headers": {"Authorization": "Bearer fixture-key"},
        "json": {
            "model": "gpt-4o-mini-tts",
            "input": TEXT,
            "voice": "alloy",
            "response_format": "wav",
            "speed": 1.0,
        },
        "timeout": timeout,
    }


@pytest.mark.parametrize("statuses", ((200,), (401,), (503,), (404, 200), (405, 501)))
def test_general_candidates_keep_configured_route_then_versioned_route_and_ignore_session(
    monkeypatch: pytest.MonkeyPatch, statuses: tuple[int, ...]
) -> None:
    settings, endpoint = configure_candidate(monkeypatch)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    results = [native_response(status) for status in statuses]
    calls: list[tuple[str, dict[str, object]]] = []
    payloads: list[object] = []
    session = requests.Session()
    close = session.close
    close_calls: list[bool] = []

    def post(url: str, **options: object) -> requests.Response:
        assert len(calls) < len(results)
        payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        return results[len(calls) - 1]

    monkeypatch.setattr(session, "post", forbidden_post)
    monkeypatch.setattr(session, "close", lambda: close_calls.append(True))
    monkeypatch.setattr(tts_handler.requests, "post", post)
    try:
        returned = request("candidate", settings, session)
        assert returned is results[-1]
        assert bool(returned) is (statuses[-1] < 400)
        assert calls == [
            (url, candidate_options()) for url in (CUSTOM_URL, COMPAT_URL)[: len(statuses)]
        ]
        assert all(actual is payloads[0] for actual in payloads)
        assert close_calls == []
        assert settings == original and endpoint == original_endpoint
    finally:
        close()


@pytest.mark.parametrize("error_type", (requests.Timeout, requests.ConnectionError))
def test_general_candidate_post_exception_keeps_identity_without_next_url(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    settings, endpoint = configure_candidate(monkeypatch)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    error = error_type("fixture candidate failure")
    cause = RuntimeError("fixture original cause")
    error.__cause__ = cause
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **options: object) -> requests.Response:
        assert not calls
        calls.append((url, deepcopy(options)))
        raise error

    monkeypatch.setattr(tts_handler.requests, "post", post)
    with pytest.raises(error_type) as raised:
        request("candidate", settings)
    assert raised.value is error and raised.value.__cause__ is cause
    assert str(raised.value) == "fixture candidate failure"
    assert calls == [(CUSTOM_URL, candidate_options())]
    assert settings == original and endpoint == original_endpoint


def test_empty_general_candidates_raise_exact_message_without_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, endpoint = configure_candidate(monkeypatch)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)

    def urls(selected_endpoint: dict[str, object], key: str, fallback: list[str]) -> list[str]:
        assert selected_endpoint is endpoint
        assert key == "speech_path" and fallback == [COMPAT_URL]
        return []

    monkeypatch.setattr(tts_handler, "_configured_openai_urls", urls)
    monkeypatch.setattr(tts_handler.requests, "post", forbidden_post)
    with pytest.raises(RuntimeError) as raised:
        request("candidate", settings)
    assert str(raised.value) == "No speech endpoint could be resolved for 'fixture-candidate'."
    assert settings == original and endpoint == original_endpoint


def test_general_candidate_root_options_and_fallback_are_resolved_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, endpoint = configure_candidate(monkeypatch)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    first, terminal = native_response(404), native_response(200)
    events: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []
    payloads: list[object] = []

    def first_headers(selected_endpoint: dict[str, object]) -> dict[str, str]:
        assert selected_endpoint is endpoint
        events.append("headers-1")
        monkeypatch.setattr(tts_handler.requests, "post", forbidden_post)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 111)
        return {"Authorization": "Bearer first-key"}

    def second_headers(selected_endpoint: dict[str, object]) -> dict[str, str]:
        assert selected_endpoint is endpoint
        events.append("headers-2")
        monkeypatch.setattr(tts_handler.requests, "post", forbidden_post)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 333)
        return {"Authorization": "Bearer second-key"}

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
        assert len(calls) == 1
        events.append("post-2")
        payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        return terminal

    def first_post(url: str, **options: object) -> requests.Response:
        assert not calls
        events.append("post-1")
        payloads.append(options["json"])
        calls.append((url, deepcopy(options)))
        monkeypatch.setattr(tts_handler.requests, "post", second_post)
        monkeypatch.setattr(tts_handler, "_configured_endpoint_auth_headers", second_headers)
        monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 222)
        return first

    monkeypatch.setattr(tts_handler.requests, "post", first_post)
    monkeypatch.setattr(tts_handler, "_configured_endpoint_auth_headers", first_headers)
    monkeypatch.setattr(tts_handler, "_should_try_next_openai_candidate", first_fallback)
    assert request("candidate", settings) is terminal
    assert events == ["headers-1", "post-1", "fallback-1", "headers-2", "post-2", "fallback-2"]
    expected_first, expected_second = candidate_options(111), candidate_options(333)
    expected_first["headers"], expected_second["headers"] = (
        {"Authorization": "Bearer first-key"},
        {"Authorization": "Bearer second-key"},
    )
    assert calls == [(CUSTOM_URL, expected_first), (COMPAT_URL, expected_second)]
    assert payloads[0] is payloads[1]
    assert settings == original and endpoint == original_endpoint


@pytest.mark.parametrize("sdk_fails", (False, True))
def test_sdk_response_status_does_not_fallback_but_exception_does(
    monkeypatch: pytest.MonkeyPatch, sdk_fails: bool
) -> None:
    settings, endpoint = configure_candidate(monkeypatch, direct_http=False)
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    sdk_response, posted = native_response(503), native_response(200)
    sdk_calls: list[tuple[dict[str, object], dict[str, object]]] = []
    calls: list[tuple[str, dict[str, object]]] = []
    warnings: list[tuple[str, tuple[object, ...]]] = []
    error = RuntimeError("fixture SDK failure")

    def sdk(payload: dict[str, object], selected_endpoint: dict[str, object]) -> requests.Response:
        assert not sdk_calls and selected_endpoint is endpoint
        sdk_calls.append((deepcopy(payload), deepcopy(selected_endpoint)))
        if sdk_fails:
            raise error
        return sdk_response

    def post(url: str, **options: object) -> requests.Response:
        assert sdk_fails and not calls
        calls.append((url, deepcopy(options)))
        return posted

    def warning(message: str, *arguments: object) -> None:
        warnings.append((message, arguments))

    monkeypatch.setattr(tts_handler, "_request_litellm_audio", sdk)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(tts_handler.logging, "warning", warning)
    returned = request("candidate", settings)
    assert returned is (posted if sdk_fails else sdk_response)
    assert sdk_calls == [(candidate_options()["json"], original_endpoint)]
    if sdk_fails:
        assert calls == [(CUSTOM_URL, candidate_options())]
        assert len(warnings) == 1
        assert warnings[0] == (
            "LiteLLM speech call failed for endpoint '%s', falling back to direct HTTP: %s",
            ("fixture-candidate", error),
        )
    else:
        assert not returned
        assert calls == [] and warnings == []
    assert settings == original and endpoint == original_endpoint


@pytest.mark.parametrize("missing", ("text", "route"))
def test_generic_json_missing_mapping_or_route_raises_before_post(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    settings, endpoint = configure_route(monkeypatch, "generic_json")
    if missing == "text":
        endpoint["request_fields"] = {"model": "model_id"}
        message = "Endpoint 'fixture' has no configured text request field."
    else:
        endpoint["speech_path"] = " "
        message = "Endpoint 'fixture' has no configured speech route."
    original, original_endpoint = deepcopy(settings), deepcopy(endpoint)
    monkeypatch.setattr(tts_handler.requests, "post", forbidden_post)
    with pytest.raises(RuntimeError) as raised:
        request("generic_json", settings)
    assert str(raised.value) == message
    assert settings == original and endpoint == original_endpoint
