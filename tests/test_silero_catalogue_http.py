"""Silero catalogue shape and transport contracts with native HTTP responses."""

from __future__ import annotations

import json

import pytest
import requests

from pandrator.logic import tts_handler

BASE = "http://provider.invalid"


def response(
    payload: object = None, *, status: int = 200, malformed: bool = False
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = b"not JSON" if malformed else json.dumps(payload).encode("utf-8")
    return result


def fetch(
    monkeypatch: pytest.MonkeyPatch, kind: str, result: requests.Response | Exception
) -> list[dict[str, object]]:
    calls: list[str] = []
    assert kind in {"models", "voices"}
    expected_path = "/v1/models" if kind == "models" else "/v1/audio/voices"
    expected_options: dict[str, object] = (
        {"timeout": 10}
        if kind == "models"
        else {"params": {"include_unavailable": "false"}, "timeout": 15}
    )

    def get(url: str, **options: object) -> requests.Response:
        assert not calls, "Unexpected extra catalogue request"
        assert (url, options) == (BASE + expected_path, expected_options)
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tts_handler.requests, "get", get)
    try:
        if kind == "models":
            return tts_handler.get_silero_model_catalog("  http://provider.invalid///  ")
        return tts_handler.get_silero_voice_catalog("  http://provider.invalid///  ")
    finally:
        assert calls == [BASE + expected_path]


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize("data", (None, True, 17, 3.5, "unexpected", {"unexpected": "value"}))
def test_non_list_data_is_an_empty_catalogue(
    monkeypatch: pytest.MonkeyPatch, kind: str, data: object
):
    assert fetch(monkeypatch, kind, response({"data": data})) == []


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize("payload", ({}, None, [], "unexpected", 17, {"data": []}))
def test_missing_or_empty_catalogue_is_empty(
    monkeypatch: pytest.MonkeyPatch, kind: str, payload: object
):
    assert fetch(monkeypatch, kind, response(payload)) == []


@pytest.mark.parametrize("kind", ("models", "voices"))
def test_valid_catalogue_rows_preserve_metadata_and_order(
    monkeypatch: pytest.MonkeyPatch, kind: str
):
    first = {
        "id": "first",
        "status": {"installed": True, "licence_accepted": False},
        "languages": ["ukr", "eng"],
        "available": True,
    }
    second = {"id": "second", "language": "ukr", "provider_metadata": {"custom": 7}}
    payload = {"data": [None, "ignored", 7, {}, {"id": ""}, {"id": 0}, first, second, first]}
    assert fetch(monkeypatch, kind, response(payload)) == [first, second, first]


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize("failure", (403, 500, "json", "timeout", "connection"))
def test_expected_transport_and_json_failures_are_empty(
    monkeypatch: pytest.MonkeyPatch, kind: str, failure: int | str
):
    result: requests.Response | Exception
    if failure == "timeout":
        result = requests.exceptions.Timeout("fixture timeout")
    elif failure == "connection":
        result = requests.exceptions.ConnectionError("fixture connection failure")
    elif failure == "json":
        result = response(malformed=True)
    else:
        assert isinstance(failure, int)
        result = response(status=failure)
    assert fetch(monkeypatch, kind, result) == []


@pytest.mark.parametrize("kind", ("models", "voices"))
def test_unexpected_transport_error_propagates(monkeypatch: pytest.MonkeyPatch, kind: str):
    with pytest.raises(RuntimeError, match="unexpected fixture error"):
        fetch(monkeypatch, kind, RuntimeError("unexpected fixture error"))


def test_voice_query_normalization_and_unavailable_flag(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    result = response({"data": [{"id": "ukr_igor", "available": False}]})

    def get(url: str, **options: object) -> requests.Response:
        assert not calls, "Unexpected extra catalogue request"
        assert url == BASE + "/v1/audio/voices"
        assert options == {
            "params": {
                "model": "v5_cis_base_nostress",
                "language": "ukr",
                "include_unavailable": "true",
            },
            "timeout": 15,
        }
        calls.append(url)
        return result

    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert tts_handler.get_silero_voice_catalog(
        BASE, model=" v5_cis_base_nostress ", language="UK", include_unavailable=True
    ) == [{"id": "ukr_igor", "available": False}]
    assert calls == [BASE + "/v1/audio/voices"]


@pytest.mark.parametrize("kind", ("models", "voices"))
@pytest.mark.parametrize(
    "error",
    (
        ValueError("invalid base fixture"),
        requests.exceptions.ConnectionError("normalizer connection fixture"),
        RuntimeError("unexpected normalizer fixture"),
    ),
)
def test_base_normalization_retains_request_error_boundary(
    monkeypatch: pytest.MonkeyPatch, kind: str, error: Exception
):
    normalized: list[tuple[str, str]] = []

    def normalize(base: str, fallback: str) -> str:
        normalized.append((base, fallback))
        raise error

    def forbidden_get(*args: object, **kwargs: object) -> requests.Response:
        raise AssertionError("GET must not follow failed base normalization")

    monkeypatch.setattr(tts_handler, "_normalize_base_url", normalize)
    monkeypatch.setattr(tts_handler.requests, "get", forbidden_get)
    call = (
        tts_handler.get_silero_model_catalog
        if kind == "models"
        else tts_handler.get_silero_voice_catalog
    )
    if isinstance(error, RuntimeError):
        with pytest.raises(RuntimeError) as raised:
            call(BASE)
        assert raised.value is error
    else:
        assert call(BASE) == []
    assert normalized == [(BASE, "http://127.0.0.1:8001")]


def test_voice_language_normalization_error_propagates_before_base_resolution(
    monkeypatch: pytest.MonkeyPatch,
):
    error = ValueError("invalid language fixture")
    languages: list[object] = []

    def normalize_language(value: object) -> str:
        languages.append(value)
        raise error

    def forbidden_base(base: str, fallback: str) -> str:
        raise AssertionError("Base resolution must not follow invalid language")

    def forbidden_get(*args: object, **kwargs: object) -> requests.Response:
        raise AssertionError("GET must not follow invalid language")

    monkeypatch.setattr(tts_handler, "normalize_silero_language_code", normalize_language)
    monkeypatch.setattr(tts_handler, "_normalize_base_url", forbidden_base)
    monkeypatch.setattr(tts_handler.requests, "get", forbidden_get)
    with pytest.raises(ValueError) as raised:
        tts_handler.get_silero_voice_catalog(BASE, language="uk")
    assert raised.value is error
    assert languages == ["uk"]


def test_voice_language_policy_can_replace_later_base_policy(monkeypatch: pytest.MonkeyPatch):
    languages: list[object] = []
    bases: list[tuple[str, str]] = []
    gets: list[str] = []
    original_base = tts_handler._normalize_base_url
    late_base = "http://late-provider.invalid"

    def normalize_base(base: str, fallback: str) -> str:
        bases.append((base, fallback))
        return original_base(base, fallback)

    def normalize_language(value: object) -> str:
        languages.append(value)
        monkeypatch.setattr(tts_handler, "SILERO_API_BASE_URL", late_base)
        monkeypatch.setattr(tts_handler, "_normalize_base_url", normalize_base)
        return "late-language"

    def get(url: str, **options: object) -> requests.Response:
        assert not gets
        assert url == late_base + "/v1/audio/voices"
        assert options == {
            "params": {"language": "late-language", "include_unavailable": "false"},
            "timeout": 15,
        }
        gets.append(url)
        return response({"data": [{"id": "late-voice"}]})

    monkeypatch.setattr(tts_handler, "normalize_silero_language_code", normalize_language)
    monkeypatch.setattr(tts_handler.requests, "get", get)
    assert tts_handler.get_silero_voice_catalog("", language="uk") == [{"id": "late-voice"}]
    assert languages == ["uk"]
    assert bases == [("", late_base)]
    assert gets == [late_base + "/v1/audio/voices"]
