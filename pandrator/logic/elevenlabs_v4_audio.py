"""One-shot transports for ElevenLabs v4 Text-to-Dialogue audio."""

from __future__ import annotations

import base64
import binascii
import importlib
import json
import math
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests

from . import native_speech_http
from .elevenlabs_speech_contracts import (
    ELEVENLABS_V4_HTTP_MODEL,
    ELEVENLABS_V4_MAX_REQUEST_CHARACTERS,
    ELEVENLABS_V4_TURBO_MODEL,
    _validated_elevenlabs_voice_settings,
)

MAX_WEBSOCKET_FRAME_BYTES = 8 * 1024 * 1024
MAX_WEBSOCKET_AUDIO_BYTES = 32 * 1024 * 1024
_WEBSOCKET_PATH = "/v1/text-to-dialogue/stream-input"
_HTTP_PATH = "/v1/text-to-dialogue"
_OUTPUT_FORMATS = frozenset({
    "alaw_8000", "ulaw_8000", "mp3_22050_32", "mp3_24000_48",
    *(f"mp3_44100_{bitrate}" for bitrate in (32, 64, 96, 128, 192)),
    *(f"opus_48000_{bitrate}" for bitrate in (32, 64, 96, 128, 192)),
    *(f"{codec}_{rate}" for codec in ("pcm", "wav") for rate in (8000, 16000, 22050, 24000, 32000, 44100, 48000)),
})


def _validate_output_format(output_format: str) -> None:
    if output_format not in _OUTPUT_FORMATS:
        raise ValueError("Unsupported ElevenLabs dialogue output format.")


class _SafeWebsocketError(ValueError):
    """A transport failure with a fixed message and no provider data."""


def _validate_text(text: str) -> None:
    if len(text) > ELEVENLABS_V4_MAX_REQUEST_CHARACTERS:
        raise ValueError(
            "ElevenLabs v4 dialogue input exceeds the 2000 character request limit."
        )


def _safe_base_url(base_url: str, *, websocket: bool = False) -> tuple[str, str, str]:
    """Return URL parts after rejecting credentials and ambiguous URL suffixes."""
    raw_url = str(base_url).strip()
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        # Accessing .port also validates malformed port syntax.
        _ = parsed.port
    except (TypeError, ValueError):
        raise ValueError("ElevenLabs base URL is invalid.") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in raw_url
        or "#" in raw_url
    ):
        suffix = " WebSocket" if websocket else ""
        raise ValueError(f"ElevenLabs{suffix} base URL is invalid.")
    scheme = parsed.scheme.lower()
    if websocket:
        scheme = "wss" if scheme == "https" else "ws"
    path = parsed.path.rstrip("/")
    if path.casefold().endswith("/v1"):
        path = path[:-3]
    return scheme, parsed.netloc, path


def _output_content_type(output_format: str) -> str:
    codec, _, sample = output_format.partition("_")
    codec = codec.casefold()
    if codec == "mp3":
        return "audio/mpeg"
    if codec == "opus":
        return "audio/ogg; codecs=opus"
    if codec == "wav":
        return "audio/wav"
    if codec == "pcm":
        sample_rate = sample.split("_", 1)[0]
        return f"audio/L16; rate={sample_rate}" if sample_rate.isdigit() else "audio/L16"
    if codec in {"alaw", "ulaw"}:
        return "audio/basic"
    return "application/octet-stream"


def _audio_response(audio: bytes, output_format: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = audio
    response._content_consumed = True
    response.headers["Content-Type"] = _output_content_type(output_format)
    response.headers["X-Pandrator-Audio-Format"] = output_format
    return response


def post_dialogue_audio(
    *,
    base_url: str,
    api_key: str,
    model_id: str,
    voice_id: str,
    text: str,
    output_format: str,
    language_code: str = "",
    voice_settings: Mapping[str, object] | None = None,
    previous_text: str = "",
    future_text: str = "",
    timeout_seconds: float = 300.0,
) -> requests.Response:
    """POST one single-voice v4 dialogue request and return native audio."""
    if model_id != ELEVENLABS_V4_HTTP_MODEL:
        raise ValueError("The ElevenLabs HTTP dialogue route requires eleven_v4.")
    _validate_text(text)
    _validate_output_format(output_format)
    if not api_key:
        raise ValueError("An ElevenLabs API key is required for speech generation.")
    scheme, netloc, path = _safe_base_url(base_url)
    body: dict[str, Any] = {
        "model_id": model_id,
        "inputs": [{"text": text, "voice_id": voice_id}],
    }
    validated_settings = _validated_elevenlabs_voice_settings(
        dict(voice_settings or {}), model_id=model_id
    )
    mapped_settings: dict[str, Any] = {}
    if "stability" in validated_settings:
        mapped_settings["stability"] = validated_settings["stability"]
    if "similarity_boost" in validated_settings:
        mapped_settings["similarity"] = validated_settings["similarity_boost"]
    if mapped_settings:
        body["settings"] = mapped_settings
    if language_code:
        body["language_code"] = language_code
    if previous_text:
        body["previous_text"] = previous_text
    if future_text:
        body["future_text"] = future_text

    url = urlunsplit((scheme, netloc, f"{path}{_HTTP_PATH}", "", ""))
    response = native_speech_http.post_native_speech(
        url,
        request_label="ElevenLabs v4 dialogue",
        request_options=lambda: {
            "headers": {
                "Accept": _output_content_type(output_format),
                "Content-Type": "application/json",
                "xi-api-key": api_key,
            },
            "params": {"output_format": output_format},
            "json": body,
            "timeout": timeout_seconds,
        },
    )
    response.headers["X-Pandrator-Audio-Format"] = output_format
    return response


def stream_turbo_dialogue_audio(
    *,
    base_url: str,
    api_key: str,
    model_id: str,
    voice_id: str,
    text: str,
    output_format: str,
    language_code: str = "",
    voice_settings: Mapping[str, object] | None = None,
    timeout_seconds: float = 300.0,
) -> requests.Response:
    """Open one v4 Turbo WebSocket session and collect its complete audio."""
    if model_id != ELEVENLABS_V4_TURBO_MODEL:
        raise ValueError("The ElevenLabs realtime dialogue route requires eleven_v4_turbo.")
    _validate_text(text)
    _validate_output_format(output_format)
    if not api_key:
        raise ValueError("An ElevenLabs API key is required for speech generation.")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("ElevenLabs realtime dialogue timeout is invalid.") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ElevenLabs realtime dialogue timeout is invalid.")
    validated_settings = _validated_elevenlabs_voice_settings(
        dict(voice_settings or {}), model_id=model_id
    )
    scheme, netloc, path = _safe_base_url(base_url, websocket=True)
    query: list[tuple[str, str]] = [
        ("model_id", model_id),
        ("output_format", output_format),
    ]
    if language_code:
        query.append(("language_code", language_code))
    uri = urlunsplit((scheme, netloc, f"{path}{_WEBSOCKET_PATH}", urlencode(query), ""))
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise _SafeWebsocketError(
                "ElevenLabs realtime dialogue timed out before audio completed."
            )
        return seconds

    try:
        # Imported only when the v4 Turbo route is actually selected.
        connect = importlib.import_module("websockets.sync.client").connect

        connection = connect(
            uri,
            additional_headers={"xi-api-key": api_key},
            proxy=None,
            open_timeout=remaining(),
            max_size=MAX_WEBSOCKET_FRAME_BYTES,
        )
        try:
            initial: dict[str, object] = {"voices": [voice_id]}
            if "stability" in validated_settings:
                initial["voice_settings"] = {
                    "stability": validated_settings["stability"]
                }
            connection.send(json.dumps(initial, separators=(",", ":")))
            remaining()
            connection.send(
                json.dumps(
                    {"inputs": [{"text": text, "voice_id": voice_id}], "flush": True},
                    separators=(",", ":"),
                )
            )
            remaining()
            connection.send(json.dumps({"close_socket": True}, separators=(",", ":")))

            audio = bytearray()
            final_received = False
            while not final_received:
                frame = connection.recv(timeout=remaining())
                if not isinstance(frame, str):
                    raise _SafeWebsocketError(
                        "ElevenLabs realtime dialogue returned a malformed frame."
                    )
                try:
                    message = json.loads(frame)
                except (TypeError, ValueError):
                    raise _SafeWebsocketError(
                        "ElevenLabs realtime dialogue returned a malformed frame."
                    ) from None
                if not isinstance(message, dict):
                    raise _SafeWebsocketError(
                        "ElevenLabs realtime dialogue returned a malformed frame."
                    )
                if "error" in message:
                    raise _SafeWebsocketError(
                        "ElevenLabs realtime dialogue returned an error frame."
                    )
                if "audio" in message:
                    encoded = message["audio"]
                    if not isinstance(encoded, str):
                        raise _SafeWebsocketError(
                            "ElevenLabs realtime dialogue returned malformed audio."
                        )
                    try:
                        chunk = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        raise _SafeWebsocketError(
                            "ElevenLabs realtime dialogue returned malformed audio."
                        ) from None
                    if len(audio) + len(chunk) > MAX_WEBSOCKET_AUDIO_BYTES:
                        raise _SafeWebsocketError(
                            "ElevenLabs realtime dialogue output exceeds the 32 MiB limit."
                        )
                    audio.extend(chunk)
                final_received = message.get("is_final") is True
            if not audio:
                raise _SafeWebsocketError("ElevenLabs realtime dialogue returned no audio.")
            return _audio_response(bytes(audio), output_format)
        finally:
            connection.close()
    except _SafeWebsocketError:
        raise
    except TimeoutError:
        raise ValueError(
            "ElevenLabs realtime dialogue timed out before audio completed."
        ) from None
    except Exception:
        # Provider errors and websocket exceptions may include credentials,
        # request text, or endpoint details; never expose those values.
        raise ValueError(
            "ElevenLabs realtime dialogue transport failed; no audio was returned."
        ) from None


__all__ = [
    "MAX_WEBSOCKET_AUDIO_BYTES",
    "MAX_WEBSOCKET_FRAME_BYTES",
    "post_dialogue_audio",
    "stream_turbo_dialogue_audio",
]
