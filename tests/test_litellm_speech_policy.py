"""OpenAI SDK timeout/retry policy through root seams and isolated offline wire calls."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import wave
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
import requests
from openai import HttpxBinaryResponseContent

from pandrator.logic import tts_handler

TEXT = "Hello 世界"
BASE = "https://fixture.invalid/v1"
URL = "https://fixture.invalid/v1/audio/speech"
PCM = b"\x00\x00\x40\x00\xc0\xff\x00\x00"


def wav_bytes() -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(PCM)
        return buffer.getvalue()


@pytest.fixture(autouse=True)
def synthetic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDK_PACKET_KEY", " fixture-key ")


def endpoint(provider: str = "openai") -> dict[str, object]:
    return {
        "name": "fixture",
        "provider": provider,
        "base_url": BASE,
        "api_key_env": "SDK_PACKET_KEY",
        "api_key": "explicit-fixture-key",
    }


def sdk_response(status: int = 200) -> HttpxBinaryResponseContent:
    return HttpxBinaryResponseContent(
        httpx.Response(
            status,
            headers={"Content-Type": "audio/wav", "X-Fixture": "native"},
            content=wav_bytes(),
            request=httpx.Request("POST", URL),
        )
    )


def assert_converted_response(result: requests.Response, status: int = 200) -> None:
    assert isinstance(result, requests.Response)
    assert result.status_code == status
    assert result.content == wav_bytes()
    assert result.headers == {
        "Content-Type": "audio/wav",
        "X-Fixture": "native",
        "Content-Length": "52",
    }
    assert result.url == URL
    assert result.request is not None
    assert result.request.method == "POST" and result.request.url == URL
    assert bool(result) is (status < 400)


@pytest.mark.parametrize("custom", (False, True))
def test_openai_helper_supplies_complete_literal_sdk_policy_kwargs(
    monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    payload: dict[str, object] = {"input": TEXT}
    if custom:
        payload.update(
            {
                "model": "openai/tts-1-hd",
                "voice": " ALLOY ",
                "speed": 0,
                "instructions": "  fixture direction  ",
                "response_format": "mp3",
            }
        )
    selected_endpoint = endpoint()
    original, original_endpoint = deepcopy(payload), deepcopy(selected_endpoint)
    calls: list[dict[str, object]] = []
    binary = sdk_response()

    def speech(**options: object) -> HttpxBinaryResponseContent:
        assert not calls
        calls.append(deepcopy(options))
        return binary

    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client", lambda: speech)
    result = tts_handler._request_litellm_audio(payload, selected_endpoint)
    assert_converted_response(result)
    expected: dict[str, object] = {
        "model": "openai/tts-1-hd" if custom else "openai/gpt-4o-mini-tts",
        "input": TEXT,
        "voice": "alloy",
        "api_key": "fixture-key",
        "api_base": BASE,
        "response_format": "mp3" if custom else "wav",
        "timeout": 300,
        "max_retries": 0,
    }
    if custom:
        expected.update({"speed": 0, "instructions": "fixture direction"})
    assert calls == [expected]
    assert payload == original and selected_endpoint == original_endpoint


def test_openai_timeout_is_resolved_from_root_on_each_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {"input": TEXT, "speed": 0}
    selected_endpoint = endpoint()
    original, original_endpoint = deepcopy(payload), deepcopy(selected_endpoint)
    calls: list[dict[str, object]] = []
    binary = sdk_response()

    def speech(**options: object) -> HttpxBinaryResponseContent:
        assert len(calls) < 2
        calls.append(deepcopy(options))
        return binary

    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client", lambda: speech)
    assert_converted_response(tts_handler._request_litellm_audio(payload, selected_endpoint))
    monkeypatch.setattr(tts_handler, "TTS_GENERATION_TIMEOUT_SECONDS", 111)
    assert_converted_response(tts_handler._request_litellm_audio(payload, selected_endpoint))
    assert calls == [
        {
            "model": "openai/gpt-4o-mini-tts",
            "input": TEXT,
            "voice": "alloy",
            "api_key": "fixture-key",
            "api_base": BASE,
            "speed": 0,
            "response_format": "wav",
            "timeout": timeout,
            "max_retries": 0,
        }
        for timeout in (300, 111)
    ]
    assert payload == original and selected_endpoint == original_endpoint


@pytest.mark.parametrize("provider", ("gemini", "azure", "elevenlabs"))
def test_other_provider_full_kwargs_and_native_conversion_remain_unchanged(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    payload: dict[str, object] = {
        "input": TEXT,
        "speed": 0,
        "instructions": "  fixture direction  ",
        "response_format": "mp3",
    }
    expected: dict[str, object] = {
        "input": TEXT,
        "api_key": "fixture-key",
        "api_base": BASE,
        "speed": 0,
        "instructions": "fixture direction",
    }
    if provider == "gemini":
        payload.update({"model": "gemini-2.5-flash-tts", "voice": "kore"})
        expected.update(
            {"model": "gemini/gemini-2.5-flash-preview-tts", "voice": "Kore", "api_base": None}
        )
    elif provider == "azure":
        payload.update({"model": "deployment-fixture", "voice": "ALLOY"})
        expected.update({"model": "azure/deployment-fixture", "voice": "alloy"})
    else:
        payload.update({"model": "eleven_multilingual_v2", "voice": " voice/fixture "})
        expected.update({"model": "elevenlabs/eleven_multilingual_v2", "voice": "voice/fixture"})
    selected_endpoint = endpoint(provider)
    original, original_endpoint = deepcopy(payload), deepcopy(selected_endpoint)
    calls: list[dict[str, object]] = []
    binary = sdk_response(401)

    def speech(**options: object) -> HttpxBinaryResponseContent:
        assert not calls
        calls.append(deepcopy(options))
        return binary

    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client", lambda: speech)
    assert_converted_response(tts_handler._request_litellm_audio(payload, selected_endpoint), 401)
    assert calls == [expected]
    assert payload == original and selected_endpoint == original_endpoint


def test_missing_sdk_preserves_exact_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, object] = {"input": TEXT}
    selected_endpoint = endpoint()
    original, original_endpoint = deepcopy(payload), deepcopy(selected_endpoint)
    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client", lambda: None)
    monkeypatch.setattr(
        tts_handler, "_litellm_speech_import_error", ImportError("fixture dependency unavailable")
    )
    with pytest.raises(RuntimeError) as raised:
        tts_handler._request_litellm_audio(payload, selected_endpoint)
    assert str(raised.value) == (
        "LiteLLM speech support could not be loaded (ImportError: fixture dependency unavailable). "
        "Verify that the 'litellm' package and its dependencies are installed."
    )
    assert payload == original and selected_endpoint == original_endpoint


def test_unsupported_provider_is_rejected_before_sdk_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {"input": TEXT}
    selected_endpoint = endpoint()
    original, original_endpoint = deepcopy(payload), deepcopy(selected_endpoint)
    calls: list[dict[str, object]] = []

    def speech(**options: object) -> HttpxBinaryResponseContent:
        calls.append(deepcopy(options))
        raise AssertionError("Unsupported provider must not invoke SDK")

    def infer(**options: object) -> str:
        assert options == {"name": "fixture", "base_url": BASE, "raw_provider": "openai"}
        return "unsupported-fixture"

    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client", lambda: speech)
    monkeypatch.setattr(tts_handler, "_infer_audio_provider", infer)
    with pytest.raises(RuntimeError) as raised:
        tts_handler._request_litellm_audio(payload, selected_endpoint)
    assert (
        str(raised.value)
        == "Provider 'unsupported-fixture' is not supported for LiteLLM speech routing."
    )
    assert calls == []
    assert payload == original and selected_endpoint == original_endpoint


# This child owns SDK globals/caches. Before import, all real network entry points
# fail; only our finite MockTransport may send. No ambient environment is copied.
WIRE_SCRIPT = r"""
import base64
import io
import json
import socket
import sys
import time
import wave
from copy import deepcopy
from importlib.metadata import version

blocked = []
def forbid_network(*args, **kwargs):
    blocked.append("real network")
    raise AssertionError("Real network is forbidden in the SDK witness")
socket.socket.connect = forbid_network
socket.socket.connect_ex = forbid_network
socket.create_connection = forbid_network
socket.getaddrinfo = forbid_network

import dotenv
# LiteLLM loads .env at import; block file credential discovery in this child.
dotenv.load_dotenv = lambda *args, **kwargs: False
dotenv.find_dotenv = lambda *args, **kwargs: ""
import requests
requests.Session.send = forbid_network
import httpx
original_send = httpx.Client.send
def guarded_send(client, request, *args, **kwargs):
    if not isinstance(client._transport, httpx.MockTransport):
        return forbid_network()
    return original_send(client, request, *args, **kwargs)
httpx.Client.send = guarded_send
async def forbid_async(*args, **kwargs):
    return forbid_network()
httpx.AsyncClient.send = forbid_async

import litellm
import openai
from litellm.llms.openai.openai import OpenAIChatCompletion
from pandrator.logic import tts_handler
litellm.telemetry = False
litellm.callbacks = []
time.sleep = lambda seconds: None
mode = sys.argv[1]
pcm = b"\x00\x00\x40\x00\xc0\xff\x00\x00"
buffer = io.BytesIO()
with wave.open(buffer, "wb") as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(16000)
    audio.writeframes(pcm)
wav = buffer.getvalue()
calls = []
def wire(request):
    assert len(calls) < 3, "SDK witness exceeded the finite baseline retry cap"
    assert str(request.url) == "https://fixture.invalid/v1/audio/speech"
    calls.append({"url": str(request.url), "method": request.method,
                  "json": json.loads(request.content),
                  "timeout": dict(request.extensions["timeout"])})
    if mode == "timeout":
        raise httpx.ReadTimeout("fixture read timeout", request=request)
    status = {"success": 200, "503": 503, "401": 401}[mode]
    if status == 200:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav, request=request)
    return httpx.Response(status, json={"error": {"message": "fixture wire failure", "type": "fixture", "code": "fixture"}}, request=request)

wire_client = httpx.Client(transport=httpx.MockTransport(wire))
OpenAIChatCompletion._get_sync_http_client = staticmethod(lambda: wire_client)
original_get_client = OpenAIChatCompletion._get_openai_client
sdk_clients = []
def get_client(self, *args, **kwargs):
    client = original_get_client(self, *args, **kwargs)
    assert isinstance(client, openai.OpenAI)
    sdk_clients.append(client)
    return client
OpenAIChatCompletion._get_openai_client = get_client
tts_handler._get_litellm_speech_client = lambda: litellm.speech
payload = {"input": "Hello 世界", "speed": 1.0}
endpoint = {"name": "fixture", "provider": "openai", "base_url": "https://fixture.invalid/v1", "api_key_env": "SDK_PACKET_KEY", "api_key": "fixture-key"}
original_payload, original_endpoint = deepcopy(payload), deepcopy(endpoint)
outcome = {}
try:
    try:
        result = tts_handler._request_litellm_audio(payload, endpoint)
        outcome = {"success": True, "status": result.status_code,
                   "content": base64.b64encode(result.content).decode("ascii"),
                   "content_type": result.headers.get("Content-Type"),
                   "url": result.url, "request_method": result.request.method,
                   "request_url": result.request.url}
    except Exception as error:
        native_types = {
            "timeout": (openai.APITimeoutError, litellm.Timeout),
            "503": (openai.InternalServerError, litellm.ServiceUnavailableError),
            "401": (openai.AuthenticationError, litellm.AuthenticationError),
        }
        outcome = {"success": False, "error_type": type(error).__name__,
                   "error_module": type(error).__module__,
                   "native_error": mode in native_types and isinstance(error, native_types[mode]),
                   "status": getattr(error, "status_code", None)}
    assert payload == original_payload and endpoint == original_endpoint
finally:
    for client in sdk_clients:
        client.close()
    wire_client.close()
assert blocked == [], "An import or callback attempted real network"
print("WITNESS_JSON:" + json.dumps({"calls": calls, "outcome": outcome,
      "litellm_version": version("litellm"), "openai_version": version("openai"),
      "blocked": blocked, "all_clients_closed": all(client.is_closed() for client in sdk_clients) and wire_client.is_closed}))
"""


@pytest.mark.parametrize("mode", ("success", "timeout", "503", "401"))
def test_pinned_openai_wire_has_one_attempt_and_300_second_timeout(mode: str) -> None:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="pandrator-sdk-wire-") as temporary:
        environment = {
            "PATH": os.defpath,
            "HOME": temporary,
            "TMPDIR": temporary,
            "TEMP": temporary,
            "TMP": temporary,
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_LOG": "ERROR",
            "OPENAI_API_KEY": "fixture-key",
            "SDK_PACKET_KEY": "",
        }
        for name in ("SystemRoot", "WINDIR", "COMSPEC"):
            if name in os.environ:
                environment[name] = os.environ[name]
        child = subprocess.run(
            [sys.executable, "-c", WIRE_SCRIPT, mode],
            cwd=temporary,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    assert child.returncode == 0, (
        f"Offline SDK child protocol failed:\n{child.stdout}\n{child.stderr}"
    )
    lines = [
        line.removeprefix("WITNESS_JSON:")
        for line in child.stdout.splitlines()
        if line.startswith("WITNESS_JSON:")
    ]
    assert len(lines) == 1, f"Missing SDK witness receipt:\n{child.stdout}\n{child.stderr}"
    receipt = json.loads(lines[0])
    print("SDK_WIRE_RECEIPT:" + json.dumps(receipt, sort_keys=True))
    assert receipt["blocked"] == [] and receipt["all_clients_closed"] is True
    outcome = receipt["outcome"]
    if mode == "success":
        assert outcome == {
            "success": True,
            "status": 200,
            "content": base64.b64encode(wav_bytes()).decode("ascii"),
            "content_type": "audio/wav",
            "url": URL,
            "request_method": "POST",
            "request_url": URL,
        }
    else:
        assert outcome["success"] is False and outcome["native_error"] is True
        if mode in {"401", "503"}:
            assert outcome["status"] == int(mode)
    # Equality displays baseline6000 and repeated wire calls without changing
    # the desired policy expectation after the before-production experiment.
    assert receipt["calls"] == [
        {
            "url": URL,
            "method": "POST",
            "json": {
                "model": "gpt-4o-mini-tts",
                "input": TEXT,
                "voice": "alloy",
                "response_format": "wav",
                "speed": 1.0,
            },
            "timeout": {"connect": 300.0, "read": 300.0, "write": 300.0, "pool": 300.0},
        }
    ]
