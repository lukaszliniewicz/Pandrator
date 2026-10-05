"""Literal ElevenLabs catalogue requests and native Response/error contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
import requests

from pandrator.logic import tts_handler
from pandrator.web.tts_providers import ElevenLabsAdapter, TtsProviderError

BASE = "http://provider.invalid"
INITIAL_PARAMS: dict[str, str | int] = {"show_legacy": "true", "page_size": 100}
HEADERS = {"Accept": "application/json", "xi-api-key": "fixture-key"}


def response(
    payload: object = None, *, status: int = 200, malformed: bool = False
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result.url = BASE
    result._content = b"sensitive fixture NOT JSON" if malformed else json.dumps(payload).encode()
    return result


@dataclass(frozen=True)
class Step:
    result: requests.Response | Exception
    params: dict[str, str | int] | None = None


class GetScript:
    def __init__(self, kind: str, steps: list[Step]):
        assert kind in {"models", "voices"}
        self.url = BASE + ("/v1/models" if kind == "models" else "/v2/voices")
        self.steps = steps
        self.index = 0
        self.params: list[dict[str, str | int] | None] = []

    def get(self, url: str, **options: object) -> requests.Response:
        assert self.index < len(self.steps), "Unexpected extra GET"
        step = self.steps[self.index]
        expected: dict[str, object] = {"headers": HEADERS, "timeout": 15}
        if step.params is not None:
            expected["params"] = step.params
        assert (url, options) == (self.url, expected)
        # Freeze each request before the production loop mutates its shared dictionary.
        params = options.get("params")
        if params is None:
            self.params.append(None)
        else:
            assert isinstance(params, dict)
            frozen: dict[str, str | int] = {}
            for key, value in params.items():
                assert isinstance(key, str) and isinstance(value, (str, int))
                frozen[key] = value
            self.params.append(frozen)
        self.index += 1
        if isinstance(step.result, Exception):
            raise step.result
        return step.result

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tts_handler.requests, "get", self.get)

    def assert_consumed(self) -> None:
        assert self.index == len(self.steps)


def catalogue(kind: str, *, strict: bool) -> list[dict[str, object]]:
    if kind == "models":
        return tts_handler.get_elevenlabs_model_catalog(
            BASE, api_key=" fixture-key ", strict=strict
        )
    return tts_handler.get_elevenlabs_voice_catalog(BASE, api_key=" fixture-key ", strict=strict)


def fail_result(failure: int | str) -> requests.Response | Exception:
    if failure == "json":
        return response(malformed=True)
    if failure == "timeout":
        return requests.exceptions.Timeout("sensitive fixture timeout")
    if failure == "connection":
        return requests.exceptions.ConnectionError("sensitive fixture connection")
    assert isinstance(failure, int)
    return response({"error": "sensitive fixture body"}, status=failure)


def assert_error(error: tts_handler.ElevenLabsCatalogError, kind: str, status: int) -> None:
    assert error.operation == kind
    assert error.status_code == status
    assert "sensitive fixture" not in str(error)
    if status in {401, 403}:
        assert str(error) == f"ElevenLabs API key was rejected while listing {kind}."
    elif status == 429:
        assert str(error) == f"ElevenLabs rate limit reached while listing {kind}."
    elif status:
        assert str(error) == f"ElevenLabs returned HTTP {status} while listing {kind}."
    else:
        assert str(error) == f"Could not reach ElevenLabs while listing {kind}."


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("failure", (401, 403, 429, 500, "json", "timeout", "connection"))
def test_native_http_json_and_transport_errors(
    monkeypatch: pytest.MonkeyPatch, kind: str, strict: bool, failure: int | str
):
    result = fail_result(failure)
    script = GetScript(kind, [Step(result, None if kind == "models" else INITIAL_PARAMS.copy())])
    script.install(monkeypatch)
    if strict:
        with pytest.raises(tts_handler.ElevenLabsCatalogError) as raised:
            catalogue(kind, strict=True)
        status = failure if isinstance(failure, int) else 0
        assert_error(raised.value, kind, status)
        cause = raised.value.__cause__
        if isinstance(failure, int):
            assert isinstance(cause, requests.exceptions.HTTPError)
            assert cause.response is result
            assert isinstance(result, requests.Response) and not result
        elif isinstance(result, Exception):
            assert cause is result
        else:
            assert isinstance(cause, ValueError)
    else:
        assert catalogue(kind, strict=False) == []
    script.assert_consumed()


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize("strict", (False, True))
def test_unrecognized_envelope_retains_strict_policy(
    monkeypatch: pytest.MonkeyPatch, kind: str, strict: bool
):
    payload: object = {} if kind == "models" else {"voices": None}
    script = GetScript(
        kind, [Step(response(payload), None if kind == "models" else INITIAL_PARAMS.copy())]
    )
    script.install(monkeypatch)
    if strict:
        with pytest.raises(tts_handler.ElevenLabsCatalogError) as raised:
            catalogue(kind, strict=True)
        assert_error(raised.value, kind, 0)
        assert raised.value.__cause__ is None
    else:
        assert catalogue(kind, strict=False) == []
    script.assert_consumed()


def test_model_fields_languages_and_first_normalized_id_win(monkeypatch: pytest.MonkeyPatch):
    payload = [
        {
            "model_id": " model ",
            "name": " Name ",
            "can_do_text_to_speech": True,
            "description": " Description ",
            "languages": [
                {"language_id": " pl ", "name": " Polish "},
                {"language_id": "en"},
                {"language_id": ""},
                None,
            ],
        },
        {"model_id": "model", "name": "Ignored duplicate"},
        {"model_id": "vc", "can_do_text_to_speech": False},
        {"model_id": "optional-capability"},
        {},
        "ignored",
    ]
    script = GetScript("models", [Step(response(payload))])
    script.install(monkeypatch)
    assert tts_handler.get_elevenlabs_model_catalog(
        "  http://provider.invalid/v1///  ", api_key=" fixture-key ", strict=True
    ) == [
        {
            "id": "model",
            "model_id": "model",
            "name": "Name",
            "description": "Description",
            "languages": [{"language_id": "pl", "name": "Polish"}, {"language_id": "en"}],
        },
        {
            "id": "optional-capability",
            "model_id": "optional-capability",
            "name": "optional-capability",
        },
    ]
    script.assert_consumed()


def test_default_models_endpoint_and_blank_key_omit_auth_header(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def get(url: str, **options: object) -> requests.Response:
        assert not calls
        assert url == "https://api.elevenlabs.io/v1/models"
        assert options == {"headers": {"Accept": "application/json"}, "timeout": 15}
        calls.append(url)
        return response([])

    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert tts_handler.get_elevenlabs_model_catalog(api_key="  ") == []
    assert calls == ["https://api.elevenlabs.io/v1/models"]


@pytest.mark.parametrize("strict", (False, True))
def test_paginated_voice_requests_use_documented_next_page_token(
    monkeypatch: pytest.MonkeyPatch, strict: bool
):
    first = {"voice_id": " voice-1 ", "name": "One", "labels": {"fixture": "preserved"}}
    second = {"voice_id": "voice-2", "name": "Two", "nested": {"custom": 17}}
    script = GetScript(
        "voices",
        [
            Step(
                response(
                    {"voices": [first, {}, None], "has_more": True, "next_page_token": " page-two "}
                ),
                INITIAL_PARAMS.copy(),
            ),
            Step(
                response(
                    {
                        "voices": [{"voice_id": "voice-1"}, second],
                        "has_more": False,
                        "next_page_token": None,
                    }
                ),
                {**INITIAL_PARAMS, "next_page_token": "page-two"},
            ),
        ],
    )
    script.install(monkeypatch)
    assert catalogue("voices", strict=strict) == [first, second]
    script.assert_consumed()
    assert script.params == [INITIAL_PARAMS, {**INITIAL_PARAMS, "next_page_token": "page-two"}]


@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("failure", (500, "json"))
def test_later_voice_page_failure_preserves_partial_or_raises(
    monkeypatch: pytest.MonkeyPatch, strict: bool, failure: int | str
):
    first = {"voice_id": "voice-1", "name": "One"}
    result = fail_result(failure)
    script = GetScript(
        "voices",
        [
            Step(
                response({"voices": [first], "has_more": True, "next_page_token": "next"}),
                INITIAL_PARAMS.copy(),
            ),
            Step(result, {**INITIAL_PARAMS, "next_page_token": "next"}),
        ],
    )
    script.install(monkeypatch)
    if strict:
        with pytest.raises(tts_handler.ElevenLabsCatalogError) as raised:
            catalogue("voices", strict=True)
        assert_error(raised.value, "voices", failure if isinstance(failure, int) else 0)
        assert isinstance(raised.value.__cause__, (requests.exceptions.HTTPError, ValueError))
    else:
        assert catalogue("voices", strict=False) == [first]
    script.assert_consumed()


@pytest.mark.parametrize("kind", ("models", "voices"))
def test_unexpected_catalogue_error_is_not_wrapped(monkeypatch: pytest.MonkeyPatch, kind: str):
    error = RuntimeError("unexpected fixture error")
    script = GetScript(kind, [Step(error, None if kind == "models" else INITIAL_PARAMS.copy())])
    script.install(monkeypatch)
    with pytest.raises(RuntimeError) as raised:
        catalogue(kind, strict=True)
    assert raised.value is error
    script.assert_consumed()


INCOMPLETE_MESSAGE = "ElevenLabs voice pagination did not finish; the catalogue may be incomplete."


def assert_incomplete_catalogue(
    strict: bool,
    expected: list[dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    if strict:
        with pytest.raises(tts_handler.ElevenLabsCatalogError) as raised:
            catalogue("voices", strict=True)
        assert raised.value.operation == "voices"
        assert raised.value.status_code == 0
        assert raised.value.incomplete is True
        assert str(raised.value) == INCOMPLETE_MESSAGE
        assert raised.value.__cause__ is None
    else:
        assert catalogue("voices", strict=False) == expected
        assert "Incomplete ElevenLabs voice catalogue" in caplog.text


@pytest.mark.parametrize("strict", (False, True))
def test_explicit_final_page_ignores_stale_next_cursor(
    monkeypatch: pytest.MonkeyPatch, strict: bool
):
    first = {"voice_id": "voice-1"}
    script = GetScript(
        "voices",
        [
            Step(
                response({"voices": [first], "has_more": False, "next_page_token": "stale"}),
                INITIAL_PARAMS.copy(),
            )
        ],
    )
    script.install(monkeypatch)
    assert catalogue("voices", strict=strict) == [first]
    script.assert_consumed()


@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("token", (None, "  "))
def test_more_pages_without_usable_cursor_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    strict: bool,
    token: str | None,
):
    first: dict[str, object] = {"voice_id": "voice-1"}
    script = GetScript(
        "voices",
        [
            Step(
                response({"voices": [first], "has_more": True, "next_page_token": token}),
                INITIAL_PARAMS.copy(),
            )
        ],
    )
    script.install(monkeypatch)
    assert_incomplete_catalogue(strict, [first], caplog)
    script.assert_consumed()


@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("tokens", (("repeat", "repeat"), ("a", "b", "a")))
def test_repeated_cursor_stops_before_requesting_the_same_page_again(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    strict: bool,
    tokens: tuple[str, ...],
):
    rows: list[dict[str, object]] = []
    steps: list[Step] = []
    params = INITIAL_PARAMS.copy()
    for index, token in enumerate(tokens):
        row: dict[str, object] = {"voice_id": f"voice-{index}"}
        rows.append(row)
        steps.append(
            Step(
                response({"voices": [row], "has_more": True, "next_page_token": token}),
                params.copy(),
            )
        )
        params["next_page_token"] = token
    script = GetScript("voices", steps)
    script.install(monkeypatch)
    assert_incomplete_catalogue(strict, rows, caplog)
    script.assert_consumed()


@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("complete", (False, True))
def test_twentieth_page_distinguishes_complete_results_from_safety_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, strict: bool, complete: bool
):
    rows: list[dict[str, object]] = []
    steps: list[Step] = []
    params = INITIAL_PARAMS.copy()
    for index in range(20):
        row: dict[str, object] = {"voice_id": f"voice-{index}"}
        rows.append(row)
        payload = {
            "voices": [row],
            "has_more": not (complete and index == 19),
            "next_page_token": f"page-{index + 1}",
        }
        steps.append(Step(response(payload), params.copy()))
        params["next_page_token"] = f"page-{index + 1}"
    script = GetScript("voices", steps)
    script.install(monkeypatch)
    if complete:
        assert catalogue("voices", strict=strict) == rows
        assert "Incomplete ElevenLabs voice catalogue" not in caplog.text
    else:
        assert_incomplete_catalogue(strict, rows, caplog)
    script.assert_consumed()


@pytest.mark.parametrize("strict", (False, True))
@pytest.mark.parametrize("legacy_envelope", (False, True))
def test_empty_page_with_new_cursor_keeps_paging_and_missing_has_more_stays_compatible(
    monkeypatch: pytest.MonkeyPatch, strict: bool, legacy_envelope: bool
):
    row = {"voice_id": "voice-1"}
    first: dict[str, object] = {"voices": [], "next_page_token": "second"}
    last: dict[str, object] = {"voices": [row]}
    if not legacy_envelope:
        first["has_more"] = True
        last["has_more"] = False
    script = GetScript(
        "voices",
        [
            Step(response(first), INITIAL_PARAMS.copy()),
            Step(response(last), {**INITIAL_PARAMS, "next_page_token": "second"}),
        ],
    )
    script.install(monkeypatch)
    assert catalogue("voices", strict=strict) == [row]
    script.assert_consumed()


@pytest.mark.parametrize("repeat", (False, True))
def test_adapter_rejects_incomplete_catalogue_instead_of_enriching_partial_rows(
    monkeypatch: pytest.MonkeyPatch, repeat: bool
):
    models = GetScript("models", [Step(response([{"model_id": "model"}]))])
    steps = [
        Step(
            response(
                {
                    "voices": [{"voice_id": "first"}],
                    "has_more": True,
                    "next_page_token": "next" if repeat else None,
                }
            ),
            INITIAL_PARAMS.copy(),
        )
    ]
    if repeat:
        steps.append(
            Step(
                response(
                    {
                        "voices": [{"voice_id": "second"}],
                        "has_more": True,
                        "next_page_token": "next",
                    }
                ),
                {**INITIAL_PARAMS, "next_page_token": "next"},
            )
        )
    voices = GetScript("voices", steps)

    def get(url: str, **options: object) -> requests.Response:
        return (models if url == models.url else voices).get(url, **options)

    monkeypatch.setattr(tts_handler.requests, "get", get)
    with pytest.raises(TtsProviderError) as raised:
        ElevenLabsAdapter("elevenlabs").enrich_catalog(
            {"id": "elevenlabs", "api_base": BASE}, api_key="fixture-key"
        )
    assert raised.value.retryable is True
    assert str(raised.value) == INCOMPLETE_MESSAGE
    cause = raised.value.__cause__
    assert isinstance(cause, tts_handler.ElevenLabsCatalogError)
    assert cause.incomplete is True
    assert cause.status_code == 0
    models.assert_consumed()
    voices.assert_consumed()


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize("error_type", (ValueError, RuntimeError))
def test_url_policy_errors_remain_outside_catalogue_catch_boundary(
    monkeypatch: pytest.MonkeyPatch, kind: str, error_type: type[Exception]
):
    error = error_type("fixture URL policy")

    def normalize(value: str) -> str:
        assert value == BASE
        raise error

    script = GetScript(kind, [])
    script.install(monkeypatch)
    monkeypatch.setattr(tts_handler, "_elevenlabs_base_url", normalize)
    with pytest.raises(error_type) as raised:
        catalogue(kind, strict=True)
    assert raised.value is error
    script.assert_consumed()


@pytest.mark.parametrize("kind", ("models", "voices"))
def test_status_policy_rebound_during_get_is_resolved_after_http_failure(
    monkeypatch: pytest.MonkeyPatch, kind: str
):
    result = response({}, status=500)
    observed: list[BaseException] = []

    def status(error: BaseException) -> int:
        observed.append(error)
        return 429

    def get(url: str, **options: object) -> requests.Response:
        assert url == BASE + ("/v1/models" if kind == "models" else "/v2/voices")
        assert options["headers"] == HEADERS and options["timeout"] == 15
        monkeypatch.setattr(tts_handler, "_elevenlabs_catalog_status", status)
        return result

    monkeypatch.setattr(tts_handler.requests, "get", get)
    with pytest.raises(tts_handler.ElevenLabsCatalogError) as raised:
        catalogue(kind, strict=True)
    assert_error(raised.value, kind, 429)
    assert observed == [raised.value.__cause__]
    assert isinstance(raised.value.__cause__, requests.exceptions.HTTPError)
    assert raised.value.__cause__.response is result


@pytest.mark.parametrize("kind", ("models", "voices"))
def test_error_class_rebound_during_get_is_used_for_strict_failure(
    monkeypatch: pytest.MonkeyPatch, kind: str
):
    result = response({}, status=500)
    constructors: list[tuple[str, int, bool]] = []

    class LateCatalogError(tts_handler.ElevenLabsCatalogError):
        def __init__(self, operation: str, status_code: int = 0, *, incomplete: bool = False):
            constructors.append((operation, status_code, incomplete))
            super().__init__(operation, status_code, incomplete=incomplete)

    def get(url: str, **options: object) -> requests.Response:
        assert url == BASE + ("/v1/models" if kind == "models" else "/v2/voices")
        assert options["headers"] == HEADERS and options["timeout"] == 15
        monkeypatch.setattr(tts_handler, "ElevenLabsCatalogError", LateCatalogError)
        return result

    monkeypatch.setattr(tts_handler.requests, "get", get)
    with pytest.raises(LateCatalogError) as raised:
        catalogue(kind, strict=True)
    assert constructors == [(kind, 500, False)]
    assert isinstance(raised.value.__cause__, requests.exceptions.HTTPError)
    assert raised.value.__cause__.response is result


def test_voice_headers_rebound_during_get_are_resolved_for_the_next_page(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []
    rebound_headers = {**HEADERS, "fixture-policy": "second"}

    def headers(key: str) -> dict[str, str]:
        assert key == " fixture-key "
        return rebound_headers

    def get(url: str, **options: object) -> requests.Response:
        assert url == BASE + "/v2/voices"
        assert options["timeout"] == 15
        assert len(calls) < 2
        if not calls:
            assert options == {"headers": HEADERS, "timeout": 15, "params": INITIAL_PARAMS}
            monkeypatch.setattr(tts_handler, "_elevenlabs_auth_headers", headers)
            calls.append("first")
            return response({"voices": [], "has_more": True, "next_page_token": "second"})
        assert options == {
            "headers": rebound_headers,
            "timeout": 15,
            "params": {**INITIAL_PARAMS, "next_page_token": "second"},
        }
        calls.append("second")
        return response({"voices": [{"voice_id": "voice-1"}], "has_more": False})

    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert catalogue("voices", strict=True) == [{"voice_id": "voice-1"}]
    assert calls == ["first", "second"]


@pytest.mark.parametrize("strict", (False, True))
def test_second_page_header_value_error_remains_inside_catch_boundary(
    monkeypatch: pytest.MonkeyPatch, strict: bool
):
    error = ValueError("fixture header policy")
    first = {"voice_id": "voice-1"}
    script = GetScript(
        "voices",
        [
            Step(
                response({"voices": [first], "has_more": True, "next_page_token": "second"}),
                INITIAL_PARAMS.copy(),
            )
        ],
    )
    original = tts_handler._elevenlabs_auth_headers
    calls: list[str] = []

    def headers(key: str) -> dict[str, str]:
        calls.append(key)
        if len(calls) == 2:
            raise error
        return original(key)

    script.install(monkeypatch)
    monkeypatch.setattr(tts_handler, "_elevenlabs_auth_headers", headers)
    if strict:
        with pytest.raises(tts_handler.ElevenLabsCatalogError) as raised:
            catalogue("voices", strict=True)
        assert_error(raised.value, "voices", 0)
        assert raised.value.__cause__ is error
    else:
        assert catalogue("voices", strict=False) == [first]
    assert calls == [" fixture-key ", " fixture-key "]
    script.assert_consumed()
