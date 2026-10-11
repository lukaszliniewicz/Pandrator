"""Literal, network-blocked contracts for Google voice catalogue HTTP paging."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any

import pytest
import requests

from pandrator.logic import google_tts_catalogue_http

ENDPOINT = "https://voices.example.invalid/catalogue?endpoint-secret"
HEADERS = {"Authorization": "Bearer fixture-secret", "Accept": "application/json"}
GEMINI_PARAMS: dict[str, str | int] = {"page_size": 1000}
VERTEX_PARAMS: dict[str, str | int] = {"pageSize": 50}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Network activity is forbidden in catalogue tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@dataclass
class FakeResponse:
    payload: object = None
    status_code: int = 200
    parse_error: Exception | None = None
    json_calls: int = 0

    def json(self) -> object:
        self.json_calls += 1
        if self.parse_error is not None:
            raise self.parse_error
        return self.payload


@dataclass
class Step:
    response: FakeResponse | Exception
    params: dict[str, str | int]


class GetScript:
    def __init__(self, steps: list[Step]):
        self.steps = steps
        self.index = 0
        self.calls: list[tuple[str, dict[str, str], dict[str, str | int], int, bool]] = []
        self.closed = False

    def get(self, endpoint: str, **options: object) -> FakeResponse:
        assert self.index < len(self.steps), "Unexpected extra GET"
        step = self.steps[self.index]
        expected: dict[str, object] = {
            "headers": HEADERS,
            "params": step.params,
            "timeout": 15,
            "allow_redirects": False,
        }
        assert endpoint == ENDPOINT
        assert options == expected
        params = options["params"]
        headers = options["headers"]
        assert isinstance(params, dict)
        assert isinstance(headers, dict)
        self.calls.append((endpoint, dict(headers), dict(params), 15, False))
        self.index += 1
        if isinstance(step.response, Exception):
            raise step.response
        return step.response

    def close(self) -> None:
        self.closed = True

    def install_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(google_tts_catalogue_http.requests, "get", self.get)

    def assert_consumed(self) -> None:
        assert self.index == len(self.steps)


class PostScript:
    def __init__(self, response: FakeResponse | Exception, expected_body: dict[str, object]):
        self.response = response
        self.expected_body = expected_body
        self.calls = 0
        self.close_calls = 0

    def post(self, endpoint: str, **options: object) -> FakeResponse:
        assert self.calls == 0, "Unexpected retry or extra POST"
        assert endpoint == ENDPOINT
        assert options == {
            "headers": HEADERS,
            "json": self.expected_body,
            "timeout": 60,
            "allow_redirects": False,
        }
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.close_calls += 1


def create_voice(
    request_session: object | None,
    *,
    vertex: bool = False,
    model: str = "gemini-3.8-flash-tts",
    language_code: str = "en-GB",
) -> dict[str, object]:
    return google_tts_catalogue_http.create_google_voice(
        ENDPOINT,
        headers=HEADERS,
        vertex=vertex,
        model=model,
        display_name="Fixture voice",
        prompt="Low, warm narration voice.",
        language_code=language_code,
        request_session=request_session,  # type: ignore[arg-type]
    )


def test_gemini_pages_use_literal_options_and_first_case_sensitive_ids_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {
        "id": "Voice-A",
        "displayName": "First display name",
        "description": "Description",
        "language_code": "en-US",
        "accent": "neutral",
        "gender": "FEMALE",
        "pitch": 1.25,
        "persona": "warm",
        "context": "narration",
        "regionCode": "us-central1",
        "type": "PREBUILT",
        "expireTime": "2030-01-01T00:00:00Z",
        "sample_audio": "must not escape",
        "api_key": "must not escape",
        "reference_audio": "must not escape",
        "prompted_config": {"secret": "must not escape"},
        "consent": "must not escape",
        "unknown": "must not escape",
    }
    duplicate = {"id": "Voice-A", "display_name": "Ignored duplicate"}
    different_case = {"id": "voice-a", "display_name": "Different ID"}
    script = GetScript(
        [
            Step(
                FakeResponse({"voices": [first, None, {"displayName": "missing id"}], "next_page_token": "page-two-secret"}),
                GEMINI_PARAMS,
            ),
            Step(
                FakeResponse({"voices": [duplicate, different_case], "next_page_token": ""}),
                {**GEMINI_PARAMS, "page_token": "page-two-secret"},
            ),
        ]
    )
    script.install_global(monkeypatch)

    voices = google_tts_catalogue_http.get_google_voice_catalog(ENDPOINT, headers=HEADERS)

    assert voices == [
        {
            "id": "Voice-A",
            "display_name": "First display name",
            "description": "Description",
            "language_code": "en-US",
            "accent": "neutral",
            "gender": "FEMALE",
            "pitch": "1.25",
            "persona": "warm",
            "context": "narration",
            "region_code": "us-central1",
            "type": "PREBUILT",
            "expire_time": "2030-01-01T00:00:00Z",
        },
        {"id": "voice-a", "display_name": "Different ID"},
    ]
    assert script.calls == [
        (ENDPOINT, HEADERS, GEMINI_PARAMS, 15, False),
        (
            ENDPOINT,
            HEADERS,
            {**GEMINI_PARAMS, "page_token": "page-two-secret"},
            15,
            False,
        ),
    ]
    script.assert_consumed()


def test_vertex_camel_case_response_and_resource_name_fallback_keep_borrowed_session_open() -> None:
    script = GetScript(
        [
            Step(
                FakeResponse(
                    {
                        "voices": [
                            {
                                "voiceId": "cloud-voice",
                                "displayName": "Cloud voice",
                                "languageCode": "en-GB",
                                "regionCode": "europe-west4",
                                "expireTime": "2031-01-01T00:00:00Z",
                                "credential": "must not escape",
                            }
                        ],
                        "nextPageToken": "vertex-page-secret",
                    }
                ),
                VERTEX_PARAMS,
            ),
            Step(
                FakeResponse(
                    {
                        "voices": [
                            {
                                "name": "projects/example/locations/global/voices/resource-voice",
                                "gender": "MALE",
                            }
                        ]
                    }
                ),
                {**VERTEX_PARAMS, "pageToken": "vertex-page-secret"},
            ),
        ]
    )

    voices = google_tts_catalogue_http.get_google_voice_catalog(
        ENDPOINT, headers=HEADERS, vertex=True, request_session=script  # type: ignore[arg-type]
    )

    assert voices == [
        {
            "id": "cloud-voice",
            "display_name": "Cloud voice",
            "language_code": "en-GB",
            "region_code": "europe-west4",
            "expire_time": "2031-01-01T00:00:00Z",
        },
        {"id": "resource-voice", "gender": "MALE"},
    ]
    script.assert_consumed()
    assert script.closed is False


@pytest.mark.parametrize(
    ("vertex", "response_token_key", "request_token_key", "initial_params"),
    (
        (False, "nextPageToken", "page_token", GEMINI_PARAMS),
        (True, "next_page_token", "pageToken", VERTEX_PARAMS),
    ),
)
def test_pagination_accepts_either_response_token_spelling(
    vertex: bool,
    response_token_key: str,
    request_token_key: str,
    initial_params: dict[str, str | int],
) -> None:
    token = "swapped-response-page"
    script = GetScript(
        [
            Step(FakeResponse({"voices": [{"id": "first"}], response_token_key: token}), initial_params),
            Step(
                FakeResponse({"voices": [{"id": "second"}]}),
                {**initial_params, request_token_key: token},
            ),
        ]
    )

    voices = google_tts_catalogue_http.get_google_voice_catalog(
        ENDPOINT, headers=HEADERS, vertex=vertex, request_session=script  # type: ignore[arg-type]
    )

    assert voices == [{"id": "first"}, {"id": "second"}]
    script.assert_consumed()


@pytest.mark.parametrize(("vertex", "params"), ((False, GEMINI_PARAMS), (True, VERTEX_PARAMS)))
def test_empty_catalogue_without_next_token_is_valid(
    vertex: bool, params: dict[str, str | int]
) -> None:
    script = GetScript([Step(FakeResponse({"voices": []}), params)])

    assert (
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, vertex=vertex, request_session=script  # type: ignore[arg-type]
        )
        == []
    )
    script.assert_consumed()


def test_resource_name_fallback_requires_a_voices_path_and_missing_id() -> None:
    script = GetScript(
        [
            Step(
                FakeResponse(
                    {
                        "voices": [
                            {"name": "voices/resource-id"},
                            {"id": "", "name": "voices/should-not-fallback"},
                            {"name": "projects/p/models/wrong-resource"},
                            {"name": "projects/p/voices/id/extra"},
                            {"id": "   "},
                        ]
                    }
                ),
                VERTEX_PARAMS,
            )
        ]
    )

    voices = google_tts_catalogue_http.get_google_voice_catalog(
        ENDPOINT, headers=HEADERS, vertex=True, request_session=script  # type: ignore[arg-type]
    )

    assert voices == [{"id": "resource-id"}]
    script.assert_consumed()


@pytest.mark.parametrize("status", (401, 403, 503))
def test_http_errors_are_sanitized_and_do_not_parse_body(status: int) -> None:
    failed = FakeResponse({"detail": "sensitive provider body"}, status_code=status)
    script = GetScript([Step(failed, GEMINI_PARAMS)])

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "Could not fetch the Google voice catalogue."
    assert failed.json_calls == 0
    _assert_sanitized(str(raised.value))
    script.assert_consumed()


def test_redirects_are_sanitized_without_following_them() -> None:
    redirected = FakeResponse({"location": "https://other.example.invalid/"}, status_code=302)
    script = GetScript([Step(redirected, GEMINI_PARAMS)])

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "The Google voice catalogue request was redirected."
    assert redirected.json_calls == 0
    _assert_sanitized(str(raised.value))
    script.assert_consumed()


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {},
        {"voices": None},
        {"voices": {}},
    ),
)
def test_malformed_envelopes_raise_sanitized_errors(payload: object) -> None:
    script = GetScript([Step(FakeResponse(payload), GEMINI_PARAMS)])

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "The Google voice catalogue response is invalid."
    _assert_sanitized(str(raised.value))
    script.assert_consumed()


def test_malformed_json_is_sanitized() -> None:
    script = GetScript(
        [Step(FakeResponse(parse_error=ValueError("sensitive body and page-token")), GEMINI_PARAMS)]
    )

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "The Google voice catalogue response is invalid."
    assert raised.value.__cause__ is None
    _assert_sanitized(str(raised.value))
    script.assert_consumed()


@pytest.mark.parametrize("token", (7, "   "))
def test_invalid_pagination_token_raises(token: object) -> None:
    script = GetScript(
        [Step(FakeResponse({"voices": [], "next_page_token": token}), GEMINI_PARAMS)]
    )

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "The Google voice catalogue pagination is invalid or incomplete."
    _assert_sanitized(str(raised.value))
    script.assert_consumed()


def test_repeated_pagination_token_raises_without_leaking_token() -> None:
    token = "repeated-page-token-secret"
    script = GetScript(
        [
            Step(FakeResponse({"voices": [], "next_page_token": token}), GEMINI_PARAMS),
            Step(
                FakeResponse({"voices": [], "next_page_token": token}),
                {**GEMINI_PARAMS, "page_token": token},
            ),
        ]
    )

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "The Google voice catalogue pagination is invalid or incomplete."
    assert token not in str(raised.value)
    script.assert_consumed()


def test_page_limit_raises_instead_of_silently_truncating() -> None:
    steps: list[Step] = []
    params = GEMINI_PARAMS
    for page_number in range(1, 101):
        token = f"page-{page_number}"
        steps.append(Step(FakeResponse({"voices": [], "next_page_token": token}), params))
        params = {**GEMINI_PARAMS, "page_token": token}
    script = GetScript(steps)

    with pytest.raises(RuntimeError, match="pagination is invalid or incomplete"):
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert len(script.calls) == 100
    script.assert_consumed()


def test_voice_entry_limit_raises_instead_of_silently_truncating() -> None:
    page = [{"id": f"voice-{index}"} for index in range(10_001)]
    script = GetScript([Step(FakeResponse({"voices": page}), GEMINI_PARAMS)])

    with pytest.raises(RuntimeError, match="pagination is invalid or incomplete"):
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    script.assert_consumed()


def test_transport_error_does_not_expose_request_exception() -> None:
    script = GetScript([Step(requests.Timeout(f"sensitive {ENDPOINT}"), GEMINI_PARAMS)])

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.get_google_voice_catalog(
            ENDPOINT, headers=HEADERS, request_session=script  # type: ignore[arg-type]
        )

    assert str(raised.value) == "Could not fetch the Google voice catalogue."
    assert raised.value.__cause__ is None
    _assert_sanitized(str(raised.value))
    script.assert_consumed()


def _assert_sanitized(message: str) -> None:
    assert ENDPOINT not in message
    assert "fixture-secret" not in message
    assert "endpoint-secret" not in message
    assert "sensitive" not in message
    assert "page-token" not in message


def test_create_gemini_uses_exact_body_and_filters_response_fields() -> None:
    body = {
        "store": True,
        "voice": {
            "type": "prompted",
            "model": "gemini-3.8-flash-tts",
            "display_name": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "language_code": "en-GB",
        },
    }
    response = FakeResponse(
        {
            "id": "voice_fixture123",
            "displayName": "Created fixture voice",
            "description": "A warm voice.",
            "languageCode": "en-GB",
            "sample_audio": {
                "data": "not-decoded-or-validated",
                "mime_type": "audio/wav",
                "reference_audio": "must not escape",
            },
            "usage": {
                "total_input_tokens": 12,
                "total_output_tokens": 100,
                "total_tokens": 112,
                "negative": -1,
                "boolean": True,
                "vendor_detail": "must not escape",
            },
            "consent": {"secret": "must not escape"},
            "credential": "must not escape",
            "unknown": "must not escape",
        }
    )
    script = PostScript(response, body)

    result = create_voice(script)

    assert result == {
        "id": "voice_fixture123",
        "display_name": "Created fixture voice",
        "description": "A warm voice.",
        "language_code": "en-GB",
        "sample_audio": {"data": "not-decoded-or-validated", "mime_type": "audio/wav"},
        "usage": {
            "total_input_tokens": 12,
            "total_output_tokens": 100,
            "total_tokens": 112,
        },
    }
    assert script.calls == 1
    assert script.close_calls == 0


def test_create_vertex_omits_model_uses_camel_body_and_closes_owned_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "store": True,
        "voice": {
            "type": "VOICE_TYPE_PROMPTED",
            "displayName": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "languageCode": "en-GB",
        },
    }
    response = FakeResponse(
        {
            "id": "voice_vertex456",
            "displayName": "Vertex fixture voice",
            "sampleAudio": {
                "data": "raw-pcm-data",
                "mimeType": "audio/l16; rate=24000; channels=1",
                "extra": "must not escape",
            },
            "usage": {
                "totalInputTokens": 10,
                "totalOutputTokens": 20,
                "totalTokens": 30,
                "is_cached": 1,
            },
        }
    )
    owned_session = PostScript(response, body)
    monkeypatch.setattr(google_tts_catalogue_http.requests, "Session", lambda: owned_session)

    result = google_tts_catalogue_http.create_google_voice(
        ENDPOINT,
        headers=HEADERS,
        vertex=True,
        model="gemini-3.8-flash-lite-tts",
        display_name="Fixture voice",
        prompt="Low, warm narration voice.",
        language_code="en-GB",
    )

    assert result == {
        "id": "voice_vertex456",
        "display_name": "Vertex fixture voice",
        "sample_audio": {
            "data": "raw-pcm-data",
            "mime_type": "audio/l16; rate=24000; channels=1",
        },
        "usage": {
            "total_input_tokens": 10,
            "total_output_tokens": 20,
            "total_tokens": 30,
        },
    }
    assert owned_session.calls == 1
    assert owned_session.close_calls == 1


@pytest.mark.parametrize(
    ("vertex", "body"),
    (
        (
            False,
            {
                "store": True,
                "voice": {
                    "type": "prompted",
                    "model": "gemini-3.8-flash-tts",
                    "display_name": "Fixture voice",
                    "prompted": {"input": "Low, warm narration voice."},
                },
            },
        ),
        (
            True,
            {
                "store": True,
                "voice": {
                    "type": "VOICE_TYPE_PROMPTED",
                    "displayName": "Fixture voice",
                    "prompted": {"input": "Low, warm narration voice."},
                },
            },
        ),
    ),
)
def test_create_omits_empty_optional_language_code(
    vertex: bool, body: dict[str, object]
) -> None:
    script = PostScript(FakeResponse({"id": "voice_without_language"}), body)

    result = create_voice(script, vertex=vertex, language_code="")

    assert result == {"id": "voice_without_language"}
    assert script.calls == 1
    assert script.close_calls == 0


@pytest.mark.parametrize("model", (" Gemini-3.8-flash-tts", "gemini-2.5-flash-tts", ""))
def test_create_rejects_non_exact_model_before_post(model: str) -> None:
    script = PostScript(FakeResponse({"id": "voice_never-created"}), {})

    with pytest.raises(RuntimeError, match="^Unsupported Google voice design model\\.$"):
        create_voice(script, model=model)

    assert script.calls == 0
    assert script.close_calls == 0


@pytest.mark.parametrize(
    "sample",
    (
        None,
        "not an object",
        {"data": 4, "mime_type": "audio/wav"},
        {"data": "data", "mimeType": ["audio/wav"]},
        {"data": "", "mime_type": "audio/wav"},
    ),
)
def test_create_preserves_voice_id_when_sample_is_absent_or_malformed(sample: object) -> None:
    payload = {"id": "voice_kept123", "display_name": "Kept voice"}
    if sample is not None:
        payload["sample_audio"] = sample  # type: ignore[assignment]
    script = PostScript(
        FakeResponse(payload),
        {
            "store": True,
            "voice": {
                "type": "prompted",
                "model": "gemini-3.8-flash-tts",
                "display_name": "Fixture voice",
                "prompted": {"input": "Low, warm narration voice."},
                "language_code": "en-GB",
            },
        },
    )

    result = create_voice(script)

    assert result == {"id": "voice_kept123", "display_name": "Kept voice"}


def test_create_accepts_only_stable_id_or_voice_resource_name_fallback() -> None:
    body = {
        "store": True,
        "voice": {
            "type": "prompted",
            "model": "gemini-3.8-flash-tts",
            "display_name": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "language_code": "en-GB",
        },
    }
    script = PostScript(
        FakeResponse({"name": "projects/p/locations/global/voices/voice_resource789"}), body
    )

    assert create_voice(script) == {"id": "voice_resource789"}


def test_create_usage_keeps_only_nonnegative_integer_token_counts() -> None:
    script = PostScript(
        FakeResponse(
            {
                "id": "voice_usage123",
                "usage": {
                    "total_input_tokens": True,
                    "totalInputTokens": 4,
                    "total_output_tokens": -1,
                    "totalOutputTokens": "9",
                    "total_tokens": 0,
                    "extra": 5,
                },
            }
        ),
        {
            "store": True,
            "voice": {
                "type": "prompted",
                "model": "gemini-3.8-flash-tts",
                "display_name": "Fixture voice",
                "prompted": {"input": "Low, warm narration voice."},
                "language_code": "en-GB",
            },
        },
    )

    result = create_voice(script)

    assert result == {
        "id": "voice_usage123",
        "usage": {"total_input_tokens": 4, "total_tokens": 0},
    }


@pytest.mark.parametrize(
    ("status", "payload"),
    (
        (401, {"detail": "sensitive provider body"}),
        (403, {"detail": "sensitive provider body"}),
        (429, {"detail": "sensitive provider body"}),
        (503, {"detail": "sensitive provider body"}),
        (302, {"location": "https://other.example.invalid/"}),
    ),
)
def test_create_http_failures_are_status_only_and_never_parse_body(
    status: int, payload: object
) -> None:
    response = FakeResponse(payload, status_code=status)
    body = {
        "store": True,
        "voice": {
            "type": "prompted",
            "model": "gemini-3.8-flash-tts",
            "display_name": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "language_code": "en-GB",
        },
    }
    script = PostScript(response, body)

    with pytest.raises(RuntimeError) as raised:
        create_voice(script)

    assert str(raised.value) == f"Google voice creation failed (HTTP {status})."
    assert isinstance(
        raised.value, google_tts_catalogue_http.GoogleVoiceCreationStatusError
    )
    assert raised.value.status_code == status
    assert response.json_calls == 0
    _assert_sanitized(str(raised.value))
    assert script.calls == 1
    assert script.close_calls == 0


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {},
        {"id": "other_123"},
        {"id": "voice_"},
        {"id": "voice_ bad"},
        {"id": "voice_bad/id"},
        {"id": None, "name": "voices/voice_fallback"},
    ),
)
def test_create_invalid_response_identity_is_sanitized(payload: object) -> None:
    body = {
        "store": True,
        "voice": {
            "type": "prompted",
            "model": "gemini-3.8-flash-tts",
            "display_name": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "language_code": "en-GB",
        },
    }
    script = PostScript(FakeResponse(payload), body)

    with pytest.raises(RuntimeError) as raised:
        create_voice(script)

    assert str(raised.value) == "Google voice creation failed (HTTP 200)."
    _assert_sanitized(str(raised.value))


def test_create_malformed_json_reports_status_without_exception_details() -> None:
    response = FakeResponse(parse_error=ValueError(f"sensitive {ENDPOINT} fixture-secret"), status_code=201)
    body = {
        "store": True,
        "voice": {
            "type": "prompted",
            "model": "gemini-3.8-flash-tts",
            "display_name": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "language_code": "en-GB",
        },
    }
    script = PostScript(response, body)

    with pytest.raises(RuntimeError) as raised:
        create_voice(script)

    assert str(raised.value) == "Google voice creation failed (HTTP 201)."
    assert raised.value.__cause__ is None
    _assert_sanitized(str(raised.value))


def test_create_transport_failure_is_sanitized_and_owned_session_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "store": True,
        "voice": {
            "type": "prompted",
            "model": "gemini-3.8-flash-tts",
            "display_name": "Fixture voice",
            "prompted": {"input": "Low, warm narration voice."},
            "language_code": "en-GB",
        },
    }
    owned_session = PostScript(requests.Timeout(f"sensitive {ENDPOINT} fixture-secret"), body)
    monkeypatch.setattr(google_tts_catalogue_http.requests, "Session", lambda: owned_session)

    with pytest.raises(RuntimeError) as raised:
        google_tts_catalogue_http.create_google_voice(
            ENDPOINT,
            headers=HEADERS,
            model="gemini-3.8-flash-tts",
            display_name="Fixture voice",
            prompt="Low, warm narration voice.",
            language_code="en-GB",
        )

    assert str(raised.value) == "Could not create the Google voice."
    assert raised.value.__cause__ is None
    _assert_sanitized(str(raised.value))
    assert owned_session.calls == 1
    assert owned_session.close_calls == 1
