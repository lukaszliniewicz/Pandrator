"""Qwen HTTP ownership retains lazy, late-bound facade policy callbacks."""

import base64
import inspect
import io
import json
import subprocess
import sys

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import tts_handler

BASE_URL = "http://qwen.invalid:8042"


class ClosingResponse(requests.Response):
    def __init__(self):
        super().__init__()
        self.close_count = 0

    def close(self):
        self.close_count += 1
        super().close()


def _response(status=200, payload=None, *, stream=None):
    response = ClosingResponse()
    response.status_code = status
    response.encoding = "utf-8"
    if stream is None:
        response._content = json.dumps(payload).encode()
    else:
        response.raw = io.BytesIO(stream.encode())
    return response


@pytest.mark.parametrize("method", ["_kobold_qwen_is_ready", "get_kobold_qwen_batch_capabilities"])
@pytest.mark.parametrize("explicit_key", ["", "explicit-fixture-key"])
def test_discovery_key_resolver_retains_zero_argument_call(monkeypatch, method, explicit_key):
    resolver_calls = []
    request_calls = []

    def resolve(*args, **kwargs):
        resolver_calls.append((args, kwargs))
        return "resolved-fixture-key"

    def request(url, **options):
        request_calls.append((url, options))
        return _response(payload={"batch_synthesis": {"supported": True, "streaming": True}})

    monkeypatch.setattr(tts_handler, "_resolve_kobold_qwen_api_key", resolve)
    monkeypatch.setattr(tts_handler.requests, "get", request)
    result = getattr(tts_handler, method)(BASE_URL, api_key=explicit_key)
    assert result is True if method == "_kobold_qwen_is_ready" else result["supported"] is True
    assert resolver_calls == ([] if explicit_key else [((), {})])
    assert len(request_calls) == 1
    assert request_calls[0][1] == {
        "headers": {"Authorization": f"Bearer {explicit_key or 'resolved-fixture-key'}"},
        "timeout": 2,
    }


def test_single_request_retains_policy_argument_and_return_identity(monkeypatch):
    settings = {"service": "kobold_qwen", "voice": "Ryan", "model": "Prebuilt Voices"}
    payload = {"model": "Prebuilt Voices", "input": "Test.", "voice": "Ryan", "speed": 1.0}
    calls = []
    response = _response()

    def resolve(*args, **kwargs):
        calls.append(("key", args, kwargs))
        return "single-fixture-key"

    def build(*args, **kwargs):
        calls.append(("payload", args, kwargs))
        return payload

    def post(url, **options):
        calls.append(("http", url, options))
        return response

    monkeypatch.setattr(tts_handler, "_resolve_kobold_qwen_api_key", resolve)
    monkeypatch.setattr(tts_handler, "_build_kobold_qwen_payload", build)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    assert tts_handler._request_kobold_qwen_audio("Test.", settings, BASE_URL) is response
    assert calls[:2] == [("key", (settings,), {}), ("payload", ("Test.", settings), {})]
    assert calls[0][1][0] is settings and calls[1][1][1] is settings
    assert calls[2] == (
        "http",
        f"{BASE_URL}/v1/audio/speech",
        {
            "headers": {"Authorization": "Bearer single-fixture-key"},
            "json": payload,
            "timeout": 1800,
        },
    )
    assert calls[2][2]["json"] is payload


def test_http_batch_resolves_each_callback_when_it_is_used(monkeypatch):
    settings = {"service": "kobold_qwen", "voice": "Ryan", "model": "Prebuilt Voices"}
    items = [{"id": str(i), "text": f"Text {i}.", "settings": settings} for i in range(2)]
    calls = []

    def too_early(*_args, **_kwargs):
        pytest.fail("Creating a batch iterator must not resolve or capture its policy")

    monkeypatch.setattr(tts_handler, "_resolve_kobold_qwen_api_key", too_early)
    monkeypatch.setattr(tts_handler, "_build_kobold_qwen_payload", too_early)
    monkeypatch.setattr(tts_handler, "_decode_audio_bytes", too_early)
    stream = tts_handler._iter_kobold_qwen_batch_audio_http(items, base_url=BASE_URL)
    assert inspect.isgenerator(stream)
    assert calls == []

    first_audio = AudioSegment.silent(duration=20)
    second_audio = AudioSegment.silent(duration=30)
    encoded = base64.b64encode(b"controlled encoded audio").decode()
    response = _response(
        stream="\n".join(
            json.dumps(
                {
                    "type": "item",
                    "id": str(i),
                    "status": "completed",
                    "audio_base64": encoded,
                    "response_format": "wav",
                }
            )
            for i in range(2)
        )
        + "\n"
    )

    def resolve(*args, **kwargs):
        calls.append(("key", args, kwargs))
        return "batch-fixture-key"

    def replacement_builder(*args, **kwargs):
        calls.append(("second-payload", args, kwargs))
        return {"input": "Second builder.", "voice": "Ryan"}

    def first_builder(*args, **kwargs):
        calls.append(("first-payload", args, kwargs))
        monkeypatch.setattr(tts_handler, "_build_kobold_qwen_payload", replacement_builder)
        return {"input": "First builder.", "voice": "Ryan"}

    def first_decoder(*args, **kwargs):
        calls.append(("first-decode", args, kwargs))
        return first_audio

    def second_decoder(*args, **kwargs):
        calls.append(("second-decode", args, kwargs))
        return second_audio

    def post(url, **options):
        calls.append(("http", url, options))
        monkeypatch.setattr(tts_handler, "_decode_audio_bytes", first_decoder)
        return response

    monkeypatch.setattr(tts_handler, "_resolve_kobold_qwen_api_key", resolve)
    monkeypatch.setattr(tts_handler, "_build_kobold_qwen_payload", first_builder)
    monkeypatch.setattr(tts_handler.requests, "post", post)
    try:
        assert next(stream)["audio"] is first_audio
        monkeypatch.setattr(tts_handler, "_decode_audio_bytes", second_decoder)
        assert next(stream)["audio"] is second_audio
        assert list(stream) == []
    finally:
        stream.close()
    assert calls[:3] == [
        ("key", (settings,), {}),
        ("first-payload", ("Text 0.", settings), {}),
        ("second-payload", ("Text 1.", settings), {}),
    ]
    assert calls[0][1][0] is settings
    assert calls[3] == (
        "http",
        f"{BASE_URL}/v1/audio/speech/batch",
        {
            "headers": {"Authorization": "Bearer batch-fixture-key"},
            "json": {
                "items": [
                    {"id": "0", "input": "First builder.", "voice": "Ryan"},
                    {"id": "1", "input": "Second builder.", "voice": "Ryan"},
                ],
                "stream": True,
                "fail_fast": False,
            },
            "stream": True,
            "timeout": (10, 1800),
        },
    )
    assert calls[4:] == [
        ("first-decode", (b"controlled encoded audio",), {"format_hint": "wav"}),
        ("second-decode", (b"controlled encoded audio",), {"format_hint": "wav"}),
    ]
    assert response.close_count == 1


def test_empty_http_batch_skips_policy_and_http(monkeypatch):
    def unused(*_args, **_options):
        pytest.fail("An empty HTTP batch must not resolve policy or request audio")

    for name in (
        "_resolve_kobold_qwen_api_key",
        "_build_kobold_qwen_payload",
        "_decode_audio_bytes",
    ):
        monkeypatch.setattr(tts_handler, name, unused)
    monkeypatch.setattr(tts_handler.requests, "post", unused)
    assert list(tts_handler._iter_kobold_qwen_batch_audio_http([], base_url=BASE_URL)) == []


def test_lower_http_owner_imports_without_the_facade():
    code = (
        "import sys\n"
        "from pandrator.logic import kobold_qwen_http\n"
        "assert 'pandrator.logic.tts_handler' not in sys.modules\n"
        "assert not any(name.startswith('pandrator.web') for name in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
