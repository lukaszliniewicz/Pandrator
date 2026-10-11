from __future__ import annotations

import base64
import json
import sys
import types
from typing import Any

import pytest
import requests

from pandrator.logic import elevenlabs_v4_audio, tts_handler
from pandrator.logic.elevenlabs_speech_contracts import (
    ELEVENLABS_V4_HTTP_MODEL,
    ELEVENLABS_V4_MAX_REQUEST_CHARACTERS,
    ELEVENLABS_V4_MODELS,
    ELEVENLABS_V4_TURBO_MODEL,
    _validated_elevenlabs_voice_settings,
)
from pandrator.logic.speech_performance import (
    capabilities_for_model,
    compile_performance,
)


def _settings(model: str, **overrides: Any) -> dict[str, Any]:
    return {
        "service": tts_handler.ELEVENLABS_SERVICE,
        "elevenlabs_model": model,
        "speaker": "voice-id",
        "language": "en-US",
        "elevenlabs_output_format": "mp3_44100_128",
        "provider_configs": [
            {
                "id": "elevenlabs",
                "api_key": "test-secret-key",
                "api_base": "https://eleven.example.test",
            }
        ],
        **overrides,
    }


class FakeConnection:
    def __init__(self, frames: list[str | bytes | BaseException]) -> None:
        self.frames = iter(frames)
        self.sent: list[dict[str, Any]] = []
        self.receive_timeouts: list[float] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float) -> str | bytes:
        self.receive_timeouts.append(timeout)
        frame = next(self.frames)
        if isinstance(frame, BaseException):
            raise frame
        return frame

    def close(self) -> None:
        self.closed = True


def _install_fake_connect(monkeypatch: pytest.MonkeyPatch, connect: Any) -> None:
    root = types.ModuleType("websockets")
    root.__path__ = []
    sync = types.ModuleType("websockets.sync")
    sync.__path__ = []
    client = types.ModuleType("websockets.sync.client")
    client.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", root)
    monkeypatch.setitem(sys.modules, "websockets.sync", sync)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client)


def _frame(**values: Any) -> str:
    return json.dumps(values)


def test_v4_http_posts_exact_dialogue_route_and_maps_controls_context(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, *, request_options: Any, **_kwargs: Any):
        calls.append((url, request_options()))
        return requests.Response()

    monkeypatch.setattr(tts_handler._native_speech_http, "post_native_speech", post)
    before = "before " * 30
    after = "after " * 30
    settings = _settings(
        ELEVENLABS_V4_HTTP_MODEL,
        tts_context_mode="both",
        _semantic_context={"before": before, "after": after},
        elevenlabs_voice_settings={"stability": 0.4, "similarity_boost": 0.8},
    )

    result = tts_handler._request_elevenlabs_audio("Hello there.", settings)

    assert result is not None
    assert len(calls) == 1
    url, options = calls[0]
    assert url == "https://eleven.example.test/v1/text-to-dialogue"
    assert options["params"] == {"output_format": "mp3_44100_128"}
    assert options["headers"]["xi-api-key"] == "test-secret-key"
    assert options["headers"]["Content-Type"] == "application/json"
    assert options["timeout"] == 300
    assert options["json"] == {
        "model_id": "eleven_v4",
        "inputs": [{"text": "Hello there.", "voice_id": "voice-id"}],
        "language_code": "en",
        "settings": {"stability": 0.4, "similarity": 0.8},
        "previous_text": before[-100:],
        "future_text": after[:100],
    }
    assert "text-to-speech" not in url


def test_v4_http_context_limit_note_is_only_recorded_when_trimmed():
    settings = _settings(
        ELEVENLABS_V4_HTTP_MODEL,
        tts_context_mode="both",
        _semantic_context={"before": "b" * 130, "after": "a" * 120},
    )
    compiled = compile_performance("spoken", settings)
    assert compiled.request_options == {
        "previous_text": "b" * 100,
        "next_text": "a" * 100,
    }
    assert any(
        item["message"]
        == "Eleven v4 text context is limited to 100 characters before and after."
        for item in compiled.report
    )

    untrimmed = compile_performance(
        "spoken",
        _settings(
            ELEVENLABS_V4_HTTP_MODEL,
            tts_context_mode="both",
            _semantic_context={"before": "b" * 100, "after": "a" * 100},
        ),
    )
    assert not any(
        item["message"]
        == "Eleven v4 text context is limited to 100 characters before and after."
        for item in untrimmed.report
    )


def test_v4_capabilities_and_inline_delivery_tags_match_v3_contract():
    http = capabilities_for_model(ELEVENLABS_V4_HTTP_MODEL, backend="elevenlabs_native")
    turbo = capabilities_for_model(ELEVENLABS_V4_TURBO_MODEL, backend="elevenlabs_native")
    assert http["dialect"] == turbo["dialect"] == "eleven_v4"
    assert http["instructions"] == turbo["instructions"] == "inline"
    assert http["semantic_context"] == "field"
    assert turbo["semantic_context"] == "none"
    assert "Eleven v4 uses inline delivery tags; scope and timing remain model-interpreted." in http["notes"]
    assert "The dialogue route supports stability, similarity, and up to 100 characters of unspoken text context on either side." in http["notes"]
    assert "The realtime route supports stability; hidden before/after text context is not documented." in turbo["notes"]

    performance = {
        "decision": "steer",
        "delivery": {"instruction": "Measured narration"},
        "spans": [
            {"anchor": {"quote": "world"}, "delivery": {"emphasis": "strong"}}
        ],
        "events": [{"kind": "laugh", "position": "after", "anchor": {"quote": "Hello"}}],
    }
    for model in (ELEVENLABS_V4_HTTP_MODEL, ELEVENLABS_V4_TURBO_MODEL):
        compiled = compile_performance(
            "Hello world.",
            _settings(
                model,
                generation_prompt="Measured narration",
                performance_allow_vocalizations=True,
                _performance=performance,
            ),
        )
        assert compiled.input.startswith("[Measured narration; Measured narration]")
        assert "[strong emphasis]world[Measured narration; Measured narration]" in compiled.input
        assert "[laughs]" in compiled.input
        assert compiled.transcript == "Hello world."
    with pytest.raises(ValueError, match="plain performance directions"):
        compile_performance(
            "spoken",
            _settings(ELEVENLABS_V4_HTTP_MODEL, generation_prompt="[unsafe tag]"),
        )


def test_turbo_context_is_dropped_with_existing_unsupported_report():
    compiled = compile_performance(
        "spoken",
        _settings(
            ELEVENLABS_V4_TURBO_MODEL,
            tts_context_mode="both",
            _semantic_context={"before": "private preceding context", "after": "private following context"},
        ),
    )
    assert compiled.input == "spoken"
    assert compiled.request_options == {}
    unsupported = [
        item for item in compiled.report
        if item["control"] == "semantic_context" and item["status"] == "unsupported"
    ]
    assert unsupported


def test_v4_model_settings_are_route_specific_and_keep_legacy_validator_shape():
    assert ELEVENLABS_V4_MODELS == frozenset({"eleven_v4", "eleven_v4_turbo"})
    assert ELEVENLABS_V4_MAX_REQUEST_CHARACTERS == 2_000
    assert _validated_elevenlabs_voice_settings({"style": 0.2}) == {"style": 0.2}
    assert _validated_elevenlabs_voice_settings(
        {"stability": 0.2, "similarity_boost": 0.7}, model_id="eleven_v4"
    ) == {"stability": 0.2, "similarity_boost": 0.7}
    with pytest.raises(ValueError, match="similarity_boost"):
        _validated_elevenlabs_voice_settings(
            {"similarity_boost": 0.7}, model_id="eleven_v4_turbo"
        )
    for value in (
        {"stability": True},
        {"stability": float("nan")},
        {"similarity_boost": float("inf")},
        {"style": 0.2},
    ):
        with pytest.raises(ValueError, match="ElevenLabs"):
            _validated_elevenlabs_voice_settings(value, model_id="eleven_v4")


def test_turbo_websocket_sends_one_session_and_aggregates_through_is_final(monkeypatch):
    connection = FakeConnection(
        [
            _frame(audio=base64.b64encode(b"first ").decode(), is_final_audio_for_turn=True),
            _frame(audio=base64.b64encode(b"second").decode()),
            _frame(is_final=True),
        ]
    )
    connects: list[tuple[str, dict[str, Any]]] = []

    def connect(uri: str, **options: Any) -> FakeConnection:
        connects.append((uri, options))
        return connection

    _install_fake_connect(monkeypatch, connect)
    response = tts_handler._request_elevenlabs_audio(
        "Hello there.",
        _settings(
            ELEVENLABS_V4_TURBO_MODEL,
            elevenlabs_voice_settings={"stability": 0.35},
        ),
    )

    assert len(connects) == 1
    uri, options = connects[0]
    assert uri == (
        "wss://eleven.example.test/v1/text-to-dialogue/stream-input"
        "?model_id=eleven_v4_turbo&output_format=mp3_44100_128&language_code=en"
    )
    assert options["additional_headers"] == {"xi-api-key": "test-secret-key"}
    assert options["proxy"] is None
    assert options["max_size"] == 8 * 1024 * 1024
    assert options["open_timeout"] <= 300
    assert connection.sent == [
        {"voices": ["voice-id"], "voice_settings": {"stability": 0.35}},
        {"inputs": [{"text": "Hello there.", "voice_id": "voice-id"}], "flush": True},
        {"close_socket": True},
    ]
    assert len(connection.receive_timeouts) == 3
    assert response.content == b"first second"
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "audio/mpeg"
    assert connection.closed


def test_turbo_omits_empty_voice_settings_and_passes_remaining_receive_timeout(monkeypatch):
    connection = FakeConnection([_frame(audio=base64.b64encode(b"ok").decode(), is_final=True)])
    connects: list[dict[str, Any]] = []
    ticks = iter([0.0, 1.0, 2.0, 3.0, 8.0])
    monkeypatch.setattr(elevenlabs_v4_audio.time, "monotonic", lambda: next(ticks))

    def connect(_uri: str, **options: Any) -> FakeConnection:
        connects.append(options)
        return connection

    _install_fake_connect(monkeypatch, connect)
    response = elevenlabs_v4_audio.stream_turbo_dialogue_audio(
        base_url="https://eleven.example.test",
        api_key="secret",
        model_id=ELEVENLABS_V4_TURBO_MODEL,
        voice_id="voice-id",
        text="hello",
        output_format="mp3_44100_128",
        timeout_seconds=20,
    )
    assert response.content == b"ok"
    assert connection.sent[0] == {"voices": ["voice-id"]}
    assert connects[0]["open_timeout"] == 19
    assert connection.receive_timeouts[0] == 12


def test_turbo_transport_failure_does_not_fallback_to_http(monkeypatch):
    connects: list[object] = []

    def connect(*_args: Any, **_kwargs: Any) -> FakeConnection:
        connects.append(object())
        raise RuntimeError("secret-key and source text")

    _install_fake_connect(monkeypatch, connect)
    monkeypatch.setattr(
        tts_handler._native_speech_http,
        "post_native_speech",
        lambda *_args, **_kwargs: pytest.fail("Turbo must not fall back to HTTP"),
    )
    with pytest.raises(ValueError, match="transport failed") as raised:
        tts_handler._request_elevenlabs_audio(
            "private source text", _settings(ELEVENLABS_V4_TURBO_MODEL)
        )
    assert len(connects) == 1
    assert "secret-key" not in str(raised.value)
    assert "private source text" not in str(raised.value)


@pytest.mark.parametrize(
    "output_format,expected_content_type",
    [
        ("mp3_44100_128", "audio/mpeg"),
        ("opus_48000_128", "audio/ogg; codecs=opus"),
        ("wav_44100", "audio/wav"),
        ("pcm_16000", "audio/L16; rate=16000"),
        ("alaw_8000", "audio/basic"),
        ("ulaw_8000", "audio/basic"),
        ("future_codec", "application/octet-stream"),
    ],
)
def test_synthetic_response_content_type_matches_requested_output_format(
    output_format: str,
    expected_content_type: str,
):
    response = elevenlabs_v4_audio._audio_response(b"fake-audio", output_format)
    assert response.status_code == 200
    assert response.content == b"fake-audio"
    assert response.headers["Content-Type"] == expected_content_type


def test_turbo_receive_timeout_is_sanitized_and_does_not_retry(monkeypatch):
    connection = FakeConnection([TimeoutError("secret-key and source text")])
    connects: list[object] = []

    def connect(*_args: Any, **_kwargs: Any) -> FakeConnection:
        connects.append(object())
        return connection

    _install_fake_connect(monkeypatch, connect)
    with pytest.raises(ValueError, match="timed out") as raised:
        elevenlabs_v4_audio.stream_turbo_dialogue_audio(
            base_url="https://eleven.example.test",
            api_key="secret-key",
            model_id=ELEVENLABS_V4_TURBO_MODEL,
            voice_id="voice-id",
            text="private source text",
            output_format="mp3_44100_128",
        )
    assert len(connects) == 1
    assert "secret-key" not in str(raised.value)
    assert "private source text" not in str(raised.value)


@pytest.mark.parametrize(
    "frames",
    [
        ["not-json"],
        [_frame(error="secret provider error", message="sensitive")],
        [b"binary frame"],
        [_frame(audio="***", is_final=True)],
        [_frame(is_final=True)],
        [_frame(audio=base64.b64encode(b"partial").decode()), RuntimeError("secret-key: source text")],
    ],
)
def test_turbo_rejects_malformed_or_incomplete_responses_without_leaking_data(
    monkeypatch: pytest.MonkeyPatch,
    frames: list[str | bytes | BaseException],
):
    connection = FakeConnection(frames)
    connects: list[object] = []

    def connect(*_args: Any, **_kwargs: Any) -> FakeConnection:
        connects.append(object())
        return connection

    _install_fake_connect(monkeypatch, connect)
    with pytest.raises(ValueError) as raised:
        elevenlabs_v4_audio.stream_turbo_dialogue_audio(
            base_url="https://eleven.example.test",
            api_key="secret-key",
            model_id=ELEVENLABS_V4_TURBO_MODEL,
            voice_id="voice-id",
            text="private source sentence",
            output_format="mp3_44100_128",
        )
    assert "secret-key" not in str(raised.value)
    assert "private source sentence" not in str(raised.value)
    assert "secret provider error" not in str(raised.value)
    assert connection.closed
    assert len(connects) == 1


def test_turbo_rejects_oversize_output_and_bad_base_urls(monkeypatch: pytest.MonkeyPatch):
    connection = FakeConnection(
        [_frame(audio=base64.b64encode(b"12345").decode(), is_final=True)]
    )
    def connect(*_args: Any, **_kwargs: Any) -> FakeConnection:
        return connection

    _install_fake_connect(monkeypatch, connect)
    monkeypatch.setattr(elevenlabs_v4_audio, "MAX_WEBSOCKET_AUDIO_BYTES", 4)
    with pytest.raises(ValueError, match="32 MiB"):
        elevenlabs_v4_audio.stream_turbo_dialogue_audio(
            base_url="https://eleven.example.test",
            api_key="secret-key",
            model_id=ELEVENLABS_V4_TURBO_MODEL,
            voice_id="voice-id",
            text="hello",
            output_format="mp3_44100_128",
        )

    for base_url in (
        "https://user:pass@eleven.example.test",
        "https://eleven.example.test?",
        "https://eleven.example.test#fragment",
    ):
        with pytest.raises(ValueError, match="base URL is invalid"):
            elevenlabs_v4_audio.stream_turbo_dialogue_audio(
                base_url=base_url,
                api_key="secret-key",
                model_id=ELEVENLABS_V4_TURBO_MODEL,
                voice_id="voice-id",
                text="hello",
                output_format="mp3_44100_128",
            )


@pytest.mark.parametrize("model", [ELEVENLABS_V4_HTTP_MODEL, ELEVENLABS_V4_TURBO_MODEL])
def test_v4_request_character_cap_and_unsupported_controls_fail_before_network(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
):
    http_calls: list[object] = []
    websocket_calls: list[object] = []
    monkeypatch.setattr(
        tts_handler._native_speech_http,
        "post_native_speech",
        lambda *args, **kwargs: http_calls.append((args, kwargs)),
    )
    _install_fake_connect(
        monkeypatch,
        lambda *args, **kwargs: websocket_calls.append((args, kwargs)),
    )
    with pytest.raises(ValueError, match="ElevenLabs"):
        tts_handler._request_elevenlabs_audio(
            "spoken",
            _settings(
                model,
                elevenlabs_voice_settings=(
                    {"style": 0.5}
                    if model == ELEVENLABS_V4_HTTP_MODEL
                    else {"similarity_boost": 0.5}
                ),
            ),
        )
    assert not http_calls
    assert not websocket_calls

    with pytest.raises(ValueError, match="2000 character"):
        tts_handler._request_elevenlabs_audio("x" * (ELEVENLABS_V4_MAX_REQUEST_CHARACTERS + 1), _settings(model))
    assert not http_calls
    assert not websocket_calls


def test_v4_timing_sink_does_not_claim_native_alignment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        tts_handler._native_speech_http,
        "post_native_speech",
        lambda *_args, **_kwargs: requests.Response(),
    )
    sink: dict[str, Any] = {}
    tts_handler._request_elevenlabs_audio("spoken", _settings(ELEVENLABS_V4_HTTP_MODEL), _timing_sink=sink)
    assert sink == {}


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 24000, 32000, 44100, 48000])
def test_raw_pcm_decodes_without_a_container_or_sample_changes(rate):
    samples = b"\x00\x00\xff\x7f\x00\x80" * 100
    response = elevenlabs_v4_audio._audio_response(samples, f"pcm_{rate}")
    audio = tts_handler._decode_audio_response(response)
    assert audio.raw_data == samples
    assert audio.frame_rate == rate
    assert audio.channels == 1 and audio.sample_width == 2


def test_raw_pcm_rejects_an_incomplete_sample():
    with pytest.raises(ValueError, match="incomplete PCM sample"):
        tts_handler._decode_audio_response(elevenlabs_v4_audio._audio_response(b"a", "pcm_16000"))


@pytest.mark.parametrize("codec,zero", [("ulaw", b"\xff"), ("alaw", b"\xd5")])
def test_telephony_audio_decodes_to_mono_pcm(codec, zero):
    audio = tts_handler._decode_audio_response(elevenlabs_v4_audio._audio_response(zero * 8000, f"{codec}_8000"))
    assert len(audio) == 1000
    assert audio.frame_rate == 8000 and audio.channels == 1 and audio.sample_width == 2
    assert max(abs(value) for value in audio.get_array_of_samples()) <= 8


def test_invalid_output_format_is_rejected_before_opening_a_socket(monkeypatch):
    monkeypatch.setattr(elevenlabs_v4_audio.importlib, "import_module", lambda *_args: pytest.fail("must validate first"))
    with pytest.raises(ValueError, match="output format"):
        tts_handler._request_elevenlabs_audio("hello", _settings("eleven_v4_turbo", elevenlabs_output_format="pcm_bad"))


def test_v3_keeps_existing_timestamp_route_and_text_contract(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, *, request_options: Any, **_kwargs: Any):
        calls.append((url, request_options()))
        return object()

    monkeypatch.setattr(tts_handler._native_speech_http, "post_native_speech", post)
    monkeypatch.setattr(
        elevenlabs_v4_audio,
        "post_dialogue_audio",
        lambda **_kwargs: pytest.fail("v3 must keep the legacy REST route"),
    )
    sink: dict[str, Any] = {}
    tts_handler._request_elevenlabs_audio(
        "Hello", _settings("eleven_v3"), _timing_sink=sink
    )
    assert calls[0][0].endswith("/v1/text-to-speech/voice-id/with-timestamps")
    assert calls[0][1]["json"] == {
        "text": "Hello",
        "model_id": "eleven_v3",
        "language_code": "en",
    }
    assert sink["provider_text"] == "Hello"
