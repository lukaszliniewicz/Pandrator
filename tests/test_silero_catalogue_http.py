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
