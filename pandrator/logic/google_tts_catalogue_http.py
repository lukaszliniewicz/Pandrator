"""Fetch and sanitize Google TTS voice catalogue pages."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import requests

_MAX_PAGES = 100
_MAX_VOICE_ENTRIES = 10_000
_REQUEST_ERROR = "Could not fetch the Google voice catalogue."
_RESPONSE_ERROR = "The Google voice catalogue response is invalid."
_PAGINATION_ERROR = "The Google voice catalogue pagination is invalid or incomplete."
_VOICE_DESIGN_MODELS = frozenset(
    {"gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"}
)


class GoogleVoiceCreationStatusError(RuntimeError):
    """Sanitized provider status failure, retaining only its numeric status."""

    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code
        if status_code is None:
            message = "Google voice creation failed (HTTP status unavailable)."
        else:
            message = f"Google voice creation failed (HTTP {status_code})."
        super().__init__(message)


_METADATA_ALIASES: dict[str, tuple[str, ...]] = {
    "display_name": ("display_name", "displayName"),
    "description": ("description",),
    "language_code": ("language_code", "languageCode"),
    "accent": ("accent",),
    "gender": ("gender",),
    "pitch": ("pitch",),
    "persona": ("persona",),
    "context": ("context",),
    "region_code": ("region_code", "regionCode"),
    "type": ("type",),
    "expire_time": ("expire_time", "expireTime"),
}


def _voice_id(item: dict[str, Any]) -> str | None:
    if "id" in item:
        candidate = item["id"]
    elif "voiceId" in item:
        # The Cloud voice-library response uses this camelCase spelling.
        candidate = item["voiceId"]
    else:
        resource_name = item.get("name")
        if not isinstance(resource_name, str):
            return None
        match = re.search(r"(?:^|/)voices/([^/]+)$", resource_name)
        if match is None:
            return None
        candidate = match.group(1)

    if not isinstance(candidate, str) or not candidate.strip():
        return None
    return candidate


def _metadata_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _sanitize_voice(item: dict[str, Any]) -> dict[str, object] | None:
    identifier = _voice_id(item)
    if identifier is None:
        return None

    voice: dict[str, object] = {"id": identifier}
    for output_key, input_keys in _METADATA_ALIASES.items():
        for input_key in input_keys:
            if input_key not in item:
                continue
            value = _metadata_string(item[input_key])
            if value is not None:
                voice[output_key] = value
                break
    return voice


def _request_page(
    endpoint: str,
    *,
    headers: dict[str, str],
    params: dict[str, str | int],
    request_session: requests.Session | None,
) -> object:
    getter = request_session.get if request_session is not None else requests.get
    try:
        response = getter(
            endpoint,
            headers=headers,
            params=params,
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise RuntimeError(_REQUEST_ERROR) from None

    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 300 <= status_code < 400:
        raise RuntimeError("The Google voice catalogue request was redirected.")
    if status_code != 200:
        raise RuntimeError(_REQUEST_ERROR)

    try:
        return response.json()
    except (ValueError, requests.RequestException):
        raise RuntimeError(_RESPONSE_ERROR) from None


def get_google_voice_catalog(
    endpoint: str,
    *,
    headers: dict[str, str],
    vertex: bool = False,
    request_session: requests.Session | None = None,
) -> list[dict[str, object]]:
    """Fetch every page and return only safe, normalized voice metadata."""
    page_size_key = "pageSize" if vertex else "page_size"
    page_token_key = "pageToken" if vertex else "page_token"
    next_token_key = "nextPageToken" if vertex else "next_page_token"
    alternate_next_token_key = "next_page_token" if vertex else "nextPageToken"
    page_params: dict[str, str | int] = {page_size_key: 1000}
    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_ids: set[str] = set()
    voices: list[dict[str, object]] = []
    voice_entry_count = 0

    for page_index in range(_MAX_PAGES):
        params = dict(page_params)
        if page_token is not None:
            params[page_token_key] = page_token

        payload = _request_page(
            endpoint,
            headers=headers,
            params=params,
            request_session=request_session,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(_RESPONSE_ERROR)

        page_voices = payload.get("voices")
        if not isinstance(page_voices, list):
            raise RuntimeError(_RESPONSE_ERROR)
        voice_entry_count += len(page_voices)
        if voice_entry_count > _MAX_VOICE_ENTRIES:
            raise RuntimeError(_PAGINATION_ERROR)

        for item in page_voices:
            if not isinstance(item, dict):
                continue
            voice = _sanitize_voice(item)
            if voice is None:
                continue
            identifier = voice["id"]
            assert isinstance(identifier, str)
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)
            voices.append(voice)

        raw_next_token = payload.get(next_token_key)
        if raw_next_token is None or raw_next_token == "":
            raw_next_token = payload.get(alternate_next_token_key)
        if raw_next_token is None or raw_next_token == "":
            return voices
        if not isinstance(raw_next_token, str) or not raw_next_token.strip():
            raise RuntimeError(_PAGINATION_ERROR)
        if raw_next_token in seen_tokens:
            raise RuntimeError(_PAGINATION_ERROR)
        if page_index == _MAX_PAGES - 1:
            raise RuntimeError(_PAGINATION_ERROR)

        seen_tokens.add(raw_next_token)
        page_token = raw_next_token

    raise RuntimeError(_PAGINATION_ERROR)


def _voice_creation_status_error(status_code: int | None) -> RuntimeError:
    return GoogleVoiceCreationStatusError(status_code)


def _created_voice_id(payload: dict[str, Any]) -> str | None:
    if "id" in payload:
        candidate = payload["id"]
    else:
        candidate = _voice_id({"name": payload.get("name")})
    if (
        not isinstance(candidate, str)
        or not candidate.startswith("voice_")
        or not candidate[len("voice_") :].strip()
        or any(character.isspace() for character in candidate)
        or "/" in candidate
    ):
        return None
    return candidate


def _sample_audio(payload: dict[str, Any]) -> dict[str, str] | None:
    sample = payload.get("sample_audio")
    if not isinstance(sample, dict):
        sample = payload.get("sampleAudio")
    if not isinstance(sample, dict):
        return None

    data = sample.get("data")
    mime_type = sample.get("mime_type")
    if not isinstance(mime_type, str):
        mime_type = sample.get("mimeType")
    if not isinstance(data, str) or not data or not isinstance(mime_type, str) or not mime_type:
        return None
    return {"data": data, "mime_type": mime_type}


def _usage_metadata(payload: dict[str, Any]) -> dict[str, int] | None:
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        return None

    aliases = {
        "total_input_tokens": "totalInputTokens",
        "total_output_tokens": "totalOutputTokens",
        "total_tokens": "totalTokens",
    }
    usage: dict[str, int] = {}
    for output_key, camel_key in aliases.items():
        value = raw_usage.get(output_key)
        if not isinstance(value, int) or isinstance(value, bool):
            value = raw_usage.get(camel_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[output_key] = value
    return usage or None


def create_google_voice(
    endpoint: str,
    *,
    headers: Mapping[str, str],
    vertex: bool = False,
    model: str,
    display_name: str,
    prompt: str,
    language_code: str = "",
    request_session: requests.Session | None = None,
) -> dict[str, object]:
    """Create one stored prompted Google voice and return safe response fields."""
    if model not in _VOICE_DESIGN_MODELS:
        raise RuntimeError("Unsupported Google voice design model.")

    if vertex:
        voice: dict[str, Any] = {
            "type": "VOICE_TYPE_PROMPTED",
            "displayName": display_name,
            "prompted": {"input": prompt},
        }
        if language_code:
            voice["languageCode"] = language_code
    else:
        voice = {
            "type": "prompted",
            "model": model,
            "display_name": display_name,
            "prompted": {"input": prompt},
        }
        if language_code:
            voice["language_code"] = language_code
    body: dict[str, Any] = {"store": True, "voice": voice}

    owns_session = request_session is None
    session = requests.Session() if owns_session else request_session
    assert session is not None
    try:
        try:
            response = session.post(
                endpoint,
                headers=headers,
                json=body,
                timeout=60,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("Could not create the Google voice.") from None

        raw_status = getattr(response, "status_code", None)
        status_code = raw_status if isinstance(raw_status, int) else None
        if status_code is None or not 200 <= status_code < 300:
            raise _voice_creation_status_error(status_code)

        try:
            payload = response.json()
        except (ValueError, requests.RequestException):
            raise _voice_creation_status_error(status_code) from None
        if not isinstance(payload, dict):
            raise _voice_creation_status_error(status_code)

        identifier = _created_voice_id(payload)
        if identifier is None:
            raise _voice_creation_status_error(status_code)

        safe_payload = dict(payload)
        safe_payload["id"] = identifier
        result = _sanitize_voice(safe_payload)
        assert result is not None

        sample_audio = _sample_audio(payload)
        if sample_audio is not None:
            result["sample_audio"] = sample_audio
        usage = _usage_metadata(payload)
        if usage is not None:
            result["usage"] = usage
        return result
    finally:
        if owns_session:
            session.close()
