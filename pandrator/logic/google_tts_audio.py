"""Prepare structured Google speech requests and decode unary WAV responses."""

import base64
import binascii
import io
import wave
from typing import Any

import requests

STRUCTURED_TTS_MODELS = frozenset({"gemini-3.8-flash-tts"})


def is_structured_tts_model(model: str) -> bool:
    return model.strip().casefold() in STRUCTURED_TTS_MODELS


def build_gemini_speech_request(
    model: str, text: str, voice: str, *, style: str = ""
) -> dict[str, Any]:
    content: dict[str, Any] = {"type": "text", "text": text}
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style}]
    return {
        "model": model,
        "input": [{"type": "user_input", "content": [content]}],
        "response_format": {"type": "audio", "mime_type": "audio/wav"},
        "generation_config": {"speech_config": [{"voice": voice}]},
        "store": False,
    }


def build_vertex_speech_request(text: str, voice: str, *, style: str = "") -> dict[str, Any]:
    part: dict[str, Any] = {"text": text}
    if style:
        part["speechMetadata"] = {"style": style}
    return {
        "contents": [{"role": "user", "parts": [part]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": voice}},
        },
    }


def _validate_wav(data: bytes) -> None:
    with wave.open(io.BytesIO(data), "rb") as audio:
        frames = audio.getnframes()
        if (
            audio.getframerate() <= 0
            or audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or frames <= 0
            or audio.getcomptype() != "NONE"
        ):
            raise ValueError("Unsupported WAV audio format")
        if len(audio.readframes(frames)) != frames * 2:
            raise ValueError("Truncated WAV audio frames")


def decode_google_wav_response(
    response: requests.Response,
    endpoint: str,
    provider_name: str,
    *,
    interactions: bool,
) -> requests.Response:
    if not response.ok:
        return response
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        selected = None
        if interactions:
            if "status" in payload and payload["status"] != "completed":
                raise ValueError("Interaction has not completed")
            steps = payload.get("steps")
            if not isinstance(steps, list):
                raise ValueError("Expected interaction steps")
            for step in steps:
                if not isinstance(step, dict) or step.get("type") != "model_output":
                    continue
                content = step.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "audio":
                        selected = block
        else:
            candidates = payload.get("candidates")
            if not isinstance(candidates, list):
                raise ValueError("Expected response candidates")
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content")
                if not isinstance(content, dict):
                    continue
                parts = content.get("parts")
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    inline = part.get("inlineData") or part.get("inline_data")
                    if not isinstance(inline, dict):
                        continue
                    mime = inline.get("mimeType", inline.get("mime_type", "audio/wav"))
                    if mime in {"audio/wav", "audio/x-wav"}:
                        selected = inline
        if selected is None:
            raise ValueError("Missing WAV audio")
        mime = selected.get("mime_type", selected.get("mimeType", "audio/wav"))
        if mime not in {"audio/wav", "audio/x-wav"}:
            raise ValueError("Unsupported audio MIME type")
        encoded = selected.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Missing base64 WAV audio")
        data = base64.b64decode(encoded, validate=True)
        _validate_wav(data)
    except (KeyError, TypeError, ValueError, binascii.Error, wave.Error, EOFError) as error:
        raise RuntimeError(f"{provider_name} returned no decodable WAV audio payload.") from error
    result = requests.Response()
    result.status_code = 200
    result._content = data
    result.headers["Content-Type"] = "audio/wav"
    result.url = endpoint
    return result
