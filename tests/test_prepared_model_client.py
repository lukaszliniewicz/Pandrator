"""Ordinary inference never falls back to an acquisition-capable endpoint."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import Mock

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import kobold_qwen_http, native_speech_http, tts_handler
from pandrator.logic.model_acquisition import ModelAcquisitionDenied, model_acquisition_scope
from pandrator.logic.tts_io_policy import TtsIoDenied, tts_io_scope
from pandrator.logic.tts_retry import TtsRetryGate

PAYLOAD = {"model": "synthetic-model", "input": "Hello", "voice": "reference"}


def response(status: int = 200, payload: object = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    result.url = "https://provider.invalid/v1/audio/speech/prepared"
    return result


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    replies = [response()]

    def post(url: str, **kwargs: Any) -> requests.Response:
        calls.append((url, kwargs))
        return replies.pop(0) if len(replies) > 1 else replies[0]

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("Inference probed a catalogue"))
    monkeypatch.setattr(tts_handler, "_build_xtts_openai_payload", lambda *_: dict(PAYLOAD))
    return calls, replies


def invoke(kind: str):
    if kind == "xtts":
        return tts_handler._request_xtts_audio(
            "Hello", {"allow_download": True}, "https://provider.invalid"
        )
    if kind == "qwen":
        return kobold_qwen_http._request_kobold_qwen_audio(
            "Hello", {"allow_download": True}, "https://provider.invalid",
            resolve_api_key=lambda _: "synthetic-workspace-key",
            build_payload=lambda *_: dict(PAYLOAD),
        )
    return list(kobold_qwen_http._iter_kobold_qwen_batch_audio_http(
        [{"id": "segment", "text": "Hello", "settings": {"allow_download": True}}],
        base_url="https://provider.invalid",
        resolve_api_key=lambda _: "synthetic-workspace-key",
        build_payload=lambda *_: dict(PAYLOAD),
        decode_audio=lambda audio, *, format_hint: AudioSegment.silent(1),
    ))


def batch_response() -> requests.Response:
    result = response()
    result._content = b"\n".join(json.dumps(event).encode() for event in [
        {"type": "item", "id": "segment", "index": 0, "status": "completed",
         "audio_base64": base64.b64encode(b"synthetic").decode(), "response_format": "wav"},
        {"type": "batch", "status": "completed", "count": 1, "completed": 1, "failed": 0},
    ])
    return result


@pytest.mark.parametrize("kind", ["xtts", "qwen", "batch"])
def test_prepared_inference_uses_only_new_endpoint_and_preserves_body(transport, kind):
    calls, replies = transport
    replies[:] = [batch_response() if kind == "batch" else response()]
    acquire = Mock(side_effect=AssertionError("Prepared inference acquired a model"))
    decision = Mock(return_value=False)
    with model_acquisition_scope(acquire, inference_authorizer=decision):
        invoke(kind)
    assert len(calls) == 1 and decision.call_count == 2
    url, options = calls[0]
    assert url == "https://provider.invalid/v1/audio/speech" + ("/batch" if kind == "batch" else "") + "/prepared"
    assert options["allow_redirects"] is False
    value = options["json"]
    assert value["items"] == [{"id": "segment", **PAYLOAD}] if kind == "batch" else value == PAYLOAD
    acquire.assert_not_called()


@pytest.mark.parametrize("kind", ["xtts", "qwen", "batch"])
def test_old_provider_never_falls_back_to_downloading_endpoint(transport, kind):
    calls, replies = transport
    replies[:] = [response(404)]
    with model_acquisition_scope(None, inference_authorizer=lambda: False):
        if kind == "batch":
            with pytest.raises(requests.HTTPError):
                invoke(kind)
        else:
            result = invoke(kind)
            assert isinstance(result, requests.Response) and result.status_code == 404
    assert len(calls) == 2
    assert all(url.endswith("/prepared") and options["allow_redirects"] is False for url, options in calls)


@pytest.mark.parametrize("kind", ["xtts", "qwen", "batch"])
@pytest.mark.parametrize("moment", ["before_first", "before_fallback"])
def test_revoked_inference_stops_before_next_actual_candidate(transport, kind, moment):
    calls, replies = transport
    replies[:] = [response(404)]
    decisions = [False, PermissionError("revoked")] if moment == "before_first" else [False, False, PermissionError("revoked")]
    with model_acquisition_scope(None, inference_authorizer=Mock(side_effect=decisions)), pytest.raises(ModelAcquisitionDenied):
        invoke(kind)
    assert len(calls) == (0 if moment == "before_first" else 1)


@pytest.mark.parametrize("kind", ["xtts", "qwen", "batch"])
def test_prepared_inference_keeps_current_tts_transport_fence(transport, kind):
    calls, _replies = transport
    def deny() -> None:
        raise PermissionError("workspace changed")
    with model_acquisition_scope(None, inference_authorizer=lambda: False), tts_io_scope(deny), pytest.raises(TtsIoDenied):
        invoke(kind)
    assert not calls


@pytest.mark.parametrize("kind", ["xtts", "qwen", "batch"])
@pytest.mark.parametrize("envelope", ["error", "detail"])
def test_missing_model_is_a_permanent_preparation_requirement(transport, kind, envelope):
    calls, replies = transport
    replies[:] = [response(409, {envelope: {"code": "model_acquisition_required", "message": "prepare"}})]
    with model_acquisition_scope(None, inference_authorizer=lambda: False), pytest.raises(ModelAcquisitionDenied):
        invoke(kind)
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [None, {}, {"detail": "busy"}, {"detail": {"code": "other_conflict"}}])
def test_unrelated_conflict_retains_its_existing_response(payload):
    native_speech_http.require_prepared_model_response(response(409, payload))


@pytest.mark.parametrize("kind", ["xtts", "qwen", "batch"])
def test_admin_decision_preserves_existing_acquisition_route(transport, kind):
    calls, replies = transport
    replies[:] = [batch_response() if kind == "batch" else response()]
    acquire = Mock(return_value=None)
    with model_acquisition_scope(acquire, inference_authorizer=lambda: True):
        invoke(kind)
    assert len(calls) == 1 and not calls[0][0].endswith("/prepared")
    assert "allow_redirects" not in calls[0][1]
    acquire.assert_called_once_with()


@pytest.mark.parametrize("provider", ["XTTS", "Qwen3 TTS"])
def test_missing_model_stops_the_outer_synthesis_retry_loop(transport, monkeypatch, provider):
    calls, replies = transport
    replies[:] = [response(409, {"detail": {"code": "model_acquisition_required"}})]
    monkeypatch.setattr(tts_handler, "_TTS_RETRY_GATE", TtsRetryGate())
    monkeypatch.setattr(tts_handler, "wait_for_retry", lambda *_a, **_kw: pytest.fail("Retried a known preparation requirement"))
    settings = {"service": provider, "xtts_model": "xtts_v2" if provider == "XTTS" else "Voice Cloning",
        "speaker": "reference", "language": "en"}
    with model_acquisition_scope(None, inference_authorizer=lambda: False), pytest.raises(ModelAcquisitionDenied):
        tts_handler.text_to_audio("Hello.", settings, max_attempts=4)
    assert len(calls) == 1 and calls[0][0].endswith("/prepared")
