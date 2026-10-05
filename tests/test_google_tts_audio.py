"""Pure structured Google request and native unary WAV response contracts."""

import base64
import io
import json
import socket
import wave
from copy import deepcopy
from typing import Any

import pytest
import requests

from pandrator.logic.google_tts_audio import (
    STRUCTURED_TTS_MODELS,
    build_gemini_speech_request,
    build_vertex_speech_request,
    decode_google_wav_response,
    is_structured_tts_model,
)

TEXT = "  [whisper] Hello 世界 {control}\n "
PCM = b"\x00\x00\x40\x00\xc0\xff\x00\x00"
ENDPOINT = "https://fixture.invalid/speech"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Network activity is forbidden in pure WAV tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def wav_bytes(*, channels: int = 1, width: int = 2, pcm: bytes = PCM) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return output.getvalue()


def response(payload: object, *, status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = "https://original.invalid/json"
    result.headers["Content-Type"] = "application/json"
    result.headers["X-Original"] = "fixture"
    result._content = json.dumps(payload).encode("utf-8")
    return result


def audio_payload(interactions: bool, data: object, **fields: object) -> dict[str, Any]:
    if interactions:
        return {
            "status": "completed",
            "steps": [
                {"type": "model_output", "content": [{"type": "audio", "data": data, **fields}]}
            ],
        }
    return {"candidates": [{"content": {"parts": [{"inlineData": {"data": data, **fields}}]}}]}


@pytest.mark.parametrize("style", ("", "  読んでください; calmly [soft]  "))
def test_complete_request_golden_preserves_transcript_voice_and_style(style: str) -> None:
    gemini_content: dict[str, Any] = {"type": "text", "text": TEXT}
    vertex_part: dict[str, Any] = {"text": TEXT}
    if style:
        gemini_content["annotations"] = [{"type": "speech_metadata", "style": style}]
        vertex_part["speechMetadata"] = {"style": style}
    assert build_gemini_speech_request(" GEMINI-3.8-FLASH-TTS ", TEXT, " Kore ", style=style) == {
        "model": " GEMINI-3.8-FLASH-TTS ",
        "input": [{"type": "user_input", "content": [gemini_content]}],
        "response_format": {"type": "audio", "mime_type": "audio/wav"},
        "generation_config": {"speech_config": [{"voice": " Kore "}]},
        "store": False,
    }
    assert build_vertex_speech_request(TEXT, " Kore ", style=style) == {
        "contents": [{"role": "user", "parts": [vertex_part]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": " Kore "}},
        },
    }


@pytest.mark.parametrize(
    ("model", "expected"),
    (
        ("gemini-3.8-flash-tts", True),
        ("  GEMINI-3.8-FLASH-TTS  ", True),
        ("gemini-3.1-flash-tts-preview", False),
        ("gemini-2.5-flash-tts", False),
        ("gemini/gemini-3.8-flash-tts", False),
        ("models/gemini-3.8-flash-tts", False),
        ("gemini-3.9-flash-tts", False),
        ("", False),
    ),
)
def test_structured_ids_use_only_exact_normalized_membership(model: str, expected: bool) -> None:
    assert STRUCTURED_TTS_MODELS == frozenset({"gemini-3.8-flash-tts"})
    assert is_structured_tts_model(model) is expected


@pytest.mark.parametrize("interactions", (True, False))
@pytest.mark.parametrize("mime", (None, "audio/wav", "audio/x-wav"))
def test_native_wav_round_trip_preserves_exact_bytes_and_response_contract(
    interactions: bool, mime: str | None
) -> None:
    original_wav = wav_bytes()
    fields = {} if mime is None else {"mime_type": mime}
    payload = audio_payload(interactions, base64.b64encode(original_wav).decode("ascii"), **fields)
    original_payload = deepcopy(payload)
    source = response(payload)
    converted = decode_google_wav_response(source, ENDPOINT, "Fixture", interactions=interactions)
    assert converted is not source
    assert converted.status_code == 200
    assert converted.content == original_wav
    assert dict(converted.headers) == {"Content-Type": "audio/wav"}
    assert converted.url == ENDPOINT
    assert converted.request is None
    assert source.json() == original_payload
    assert source.headers["X-Original"] == "fixture"
    with wave.open(io.BytesIO(converted.content), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.readframes(audio.getnframes()) == PCM


@pytest.mark.parametrize("interactions", (True, False))
def test_metadata_shapes_and_non_audio_blocks_are_skipped_and_last_audio_wins(
    interactions: bool,
) -> None:
    first = base64.b64encode(wav_bytes()).decode("ascii")
    last_wav = wav_bytes(pcm=b"\x02\x00\x03\x00")
    last = base64.b64encode(last_wav).decode("ascii")
    if interactions:
        payload: dict[str, Any] = {
            "steps": [
                None,
                {"type": "user_input", "content": [{"type": "audio", "data": "invalid"}]},
                {"type": "model_output", "content": {}},
                {
                    "type": "model_output",
                    "content": [
                        None,
                        {"type": "text", "text": "metadata"},
                        {"type": "audio", "data": first},
                    ],
                },
                {
                    "type": "model_output",
                    "content": [{"type": "audio", "data": last, "mimeType": "audio/wav"}],
                },
            ]
        }
    else:
        payload = {
            "candidates": [
                None,
                {},
                {"content": {"parts": {}}},
                {
                    "content": {
                        "parts": [None, {"text": "metadata"}, {"inlineData": {"data": first}}]
                    }
                },
                {
                    "content": {
                        "parts": [
                            {"inline_data": {"data": last, "mime_type": "audio/x-wav"}},
                            {"inlineData": {"data": "invalid", "mimeType": "audio/pcm"}},
                        ]
                    }
                },
            ]
        }
    assert (
        decode_google_wav_response(
            response(payload), ENDPOINT, "Fixture", interactions=interactions
        ).content
        == last_wav
    )


@pytest.mark.parametrize("interactions", (True, False))
def test_invalid_last_selected_audio_does_not_fall_back(interactions: bool) -> None:
    encoded = base64.b64encode(wav_bytes()).decode("ascii")
    payload = audio_payload(interactions, encoded)
    if interactions:
        payload["steps"][0]["content"].append({"type": "audio", "data": "?"})
    else:
        payload["candidates"][0]["content"]["parts"].append({"inlineData": {"data": "?"}})
    with pytest.raises(
        RuntimeError, match=r"^Fixture returned no decodable WAV audio payload\.$"
    ) as raised:
        decode_google_wav_response(
            response(payload), ENDPOINT, "Fixture", interactions=interactions
        )
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.parametrize("interactions", (True, False))
@pytest.mark.parametrize("status", (401, 503))
def test_non_success_identity_without_json_access(
    monkeypatch: pytest.MonkeyPatch, interactions: bool, status: int
) -> None:
    source = response({}, status=status)

    def forbidden_json(**kwargs: Any) -> None:
        raise AssertionError("Non-success JSON must not be accessed")

    monkeypatch.setattr(source, "json", forbidden_json)
    assert not source
    assert (
        decode_google_wav_response(source, ENDPOINT, "Fixture", interactions=interactions) is source
    )


@pytest.mark.parametrize("interactions", (True, False))
@pytest.mark.parametrize("data", (None, 1, "", "?", "Zg===", "é"))
def test_invalid_base64_rejects_with_provider_message_and_cause(
    interactions: bool, data: object
) -> None:
    with pytest.raises(
        RuntimeError, match=r"^Fixture returned no decodable WAV audio payload\.$"
    ) as raised:
        decode_google_wav_response(
            response(audio_payload(interactions, data)),
            ENDPOINT,
            "Fixture",
            interactions=interactions,
        )
    assert raised.value.__cause__ is not None


@pytest.mark.parametrize("interactions", (True, False))
@pytest.mark.parametrize(
    "kind",
    ("raw_pcm", "empty", "empty_wav", "truncated", "stereo", "8bit", "zero_rate", "compressed"),
)
def test_invalid_wav_formats_reject_with_chained_provider_error(
    interactions: bool, kind: str
) -> None:
    data = wav_bytes()
    if kind == "raw_pcm":
        data = PCM
    elif kind == "empty":
        data = b""
    elif kind == "empty_wav":
        data = wav_bytes(pcm=b"")
    elif kind == "truncated":
        data = data[:-2]
    elif kind == "stereo":
        data = wav_bytes(channels=2)
    elif kind == "8bit":
        data = wav_bytes(width=1)
    elif kind == "zero_rate":
        data = data[:24] + b"\x00\x00\x00\x00" + data[28:]
    elif kind == "compressed":
        data = data[:20] + b"\x02\x00" + data[22:]
    else:
        raise AssertionError(kind)
    encoded = base64.b64encode(data).decode("ascii")
    with pytest.raises(
        RuntimeError, match=r"^Fixture returned no decodable WAV audio payload\.$"
    ) as raised:
        decode_google_wav_response(
            response(audio_payload(interactions, encoded)),
            ENDPOINT,
            "Fixture",
            interactions=interactions,
        )
    assert raised.value.__cause__ is not None


@pytest.mark.parametrize("interactions", (True, False))
@pytest.mark.parametrize(
    "payload", (None, [], {}, {"steps": {}}, {"candidates": {}}, {"steps": [], "candidates": []})
)
def test_invalid_shapes_or_missing_audio_fail(interactions: bool, payload: object) -> None:
    with pytest.raises(
        RuntimeError, match=r"^Fixture returned no decodable WAV audio payload\.$"
    ) as raised:
        decode_google_wav_response(
            response(payload), ENDPOINT, "Fixture", interactions=interactions
        )
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.parametrize("status", (None, "in_progress", "failed"))
def test_non_completed_interaction_rejects_valid_audio(status: str | None) -> None:
    payload = audio_payload(True, base64.b64encode(wav_bytes()).decode("ascii"))
    payload["status"] = status
    with pytest.raises(RuntimeError, match="Fixture returned no decodable WAV audio payload"):
        decode_google_wav_response(response(payload), ENDPOINT, "Fixture", interactions=True)


@pytest.mark.parametrize("interactions", (True, False))
def test_wrong_mime_cannot_be_decoded_as_wav(interactions: bool) -> None:
    payload = audio_payload(
        interactions, base64.b64encode(wav_bytes()).decode("ascii"), mime_type="audio/pcm"
    )
    with pytest.raises(RuntimeError, match="Fixture returned no decodable WAV audio payload"):
        decode_google_wav_response(
            response(payload), ENDPOINT, "Fixture", interactions=interactions
        )


@pytest.mark.parametrize("interactions", (True, False))
def test_malformed_json_preserves_decode_exception_cause(interactions: bool) -> None:
    source = response({})
    source._content = b"not JSON"
    with pytest.raises(
        RuntimeError, match="Fixture returned no decodable WAV audio payload"
    ) as raised:
        decode_google_wav_response(source, ENDPOINT, "Fixture", interactions=interactions)
    assert isinstance(raised.value.__cause__, requests.exceptions.JSONDecodeError)
