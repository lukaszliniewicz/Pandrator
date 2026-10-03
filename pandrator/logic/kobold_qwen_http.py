"""Kobold Qwen HTTP transport with caller-supplied policy callbacks."""

import base64
import json
from collections.abc import Callable, Iterator
from threading import Event
from typing import Protocol

import requests
from pydub import AudioSegment

from .kobold_qwen_contracts import KoboldQwenBatchAudioEvent, KoboldQwenBatchCapabilities
from .tts_openai_http_policy import (
    _normalize_base_url,
    _openai_audio_speech_batch_urls,
    _openai_audio_speech_urls,
    _openai_auth_headers,
    _openai_capabilities_urls,
    _should_try_next_openai_candidate,
)

# Kobold Qwen default URLs
KOBOLD_QWEN_API_BASE_URL = "http://127.0.0.1:8042"


# A first Qwen CustomVoice request may need to download several gigabytes and
# then restart KoboldCpp with the newly selected model.  Keep the request alive
# for that one-time preparation instead of failing at the normal TTS timeout.
KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS = 1800


class AudioDecoder(Protocol):
    def __call__(self, audio: bytes, *, format_hint: str) -> AudioSegment: ...


def _kobold_qwen_is_ready(
    base_url: str, api_key: str = "", *, resolve_api_key: Callable[[], str]
) -> bool:
    """Return child readiness without confusing wrapper liveness with inference."""
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    headers = _openai_auth_headers(api_key or resolve_api_key())
    try:
        response = requests.get(
            f"{normalized_base_url}/readyz",
            headers=headers,
            timeout=2,
        )
        if response.status_code < 400:
            return True
        if response.status_code != 404:
            return False
    except requests.exceptions.RequestException:
        return False

    # Compatibility with wrapper versions predating /readyz.
    try:
        response = requests.get(
            f"{normalized_base_url}/health",
            headers=headers,
            timeout=2,
        )
        if response.status_code >= 400:
            return False
        payload = response.json()
        return bool(
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("kobold_online") is True
        )
    except (requests.exceptions.RequestException, ValueError):
        return False


def _request_kobold_qwen_audio(
    text: str, tts_settings: dict, kobold_qwen_base_url: str,
    *,
    resolve_api_key: Callable[[dict | None], str],
    build_payload: Callable[[str, dict], dict[str, str | float]],
) -> requests.Response:
    normalized_base_url = _normalize_base_url(
        kobold_qwen_base_url, KOBOLD_QWEN_API_BASE_URL
    )
    api_key = resolve_api_key(tts_settings)
    payload = build_payload(text, tts_settings)

    last_response = None
    for speech_url in _openai_audio_speech_urls(normalized_base_url):
        response = requests.post(
            speech_url,
            headers=_openai_auth_headers(api_key),
            json=payload,
            timeout=KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS,
        )

        if _should_try_next_openai_candidate(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(
        f"No Qwen3 TTS speech endpoint could be resolved for '{normalized_base_url}'."
    )


def get_kobold_qwen_batch_capabilities(
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    *,
    api_key: str = "",
    resolve_api_key: Callable[[], str],
) -> KoboldQwenBatchCapabilities:
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    headers = _openai_auth_headers(api_key or resolve_api_key())
    fallback: KoboldQwenBatchCapabilities = {
        "supported": False,
        "streaming": False,
        "default_batch_size": 1,
        "max_batch_size": 1,
    }
    for url in _openai_capabilities_urls(normalized_base_url):
        try:
            response = requests.get(url, headers=headers, timeout=2)
        except requests.RequestException:
            continue
        if _should_try_next_openai_candidate(response.status_code):
            continue
        if response.status_code >= 400:
            return fallback
        try:
            payload = response.json()
        except ValueError:
            return fallback
        batch = payload.get("batch_synthesis") if isinstance(payload, dict) else None
        if not isinstance(batch, dict):
            return fallback
        try:
            default_size = max(1, min(32, int(batch.get("default_batch_size") or 1)))
            maximum_size = max(
                default_size, min(32, int(batch.get("max_batch_size") or default_size))
            )
            parallelism = max(1, int(batch.get("parallelism") or 1))
        except (TypeError, ValueError, OverflowError):
            return fallback
        return {
            "supported": bool(batch.get("supported")),
            "streaming": bool(batch.get("streaming")),
            "endpoint": str(batch.get("endpoint") or "/v1/audio/speech/batch"),
            "protocol": str(batch.get("protocol") or ""),
            "default_batch_size": default_size,
            "max_batch_size": maximum_size,
            "parallelism": parallelism,
        }
    return fallback


def _iter_kobold_qwen_batch_audio_http(
    items: list[dict[str, object]],
    *,
    base_url: str = KOBOLD_QWEN_API_BASE_URL,
    api_key: str = "",
    stop_event: Event | None = None,
    cancel_event: Event | None = None,
    resolve_api_key: Callable[[dict | None], str],
    build_payload: Callable[[str, dict], dict[str, str | float]],
    decode_audio: AudioDecoder,
) -> Iterator[KoboldQwenBatchAudioEvent]:
    if not items:
        return
    normalized_base_url = _normalize_base_url(base_url, KOBOLD_QWEN_API_BASE_URL)
    first_settings = next(
        (
            settings
            for item in items
            if isinstance(settings := item.get("settings"), dict)
        ),
        {},
    )
    resolved_api_key = api_key or resolve_api_key(first_settings)
    request_items = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        settings = item.get("settings")
        if not item_id or not isinstance(settings, dict):
            raise ValueError("Qwen batch items require an ID and TTS settings.")
        request_items.append(
            {
                "id": item_id,
                **build_payload(
                    str(item.get("text") or ""),
                    settings,
                ),
            }
        )

    request_payload = {
        "items": request_items,
        "stream": True,
        "fail_fast": False,
    }
    last_response = None
    for batch_url in _openai_audio_speech_batch_urls(normalized_base_url):
        response = requests.post(
            batch_url,
            headers=_openai_auth_headers(resolved_api_key),
            json=request_payload,
            stream=True,
            timeout=(10, KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS),
        )
        if _should_try_next_openai_candidate(response.status_code):
            response.close()
            last_response = response
            continue
        with response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    return
                line = (
                    raw_line.decode("utf-8")
                    if isinstance(raw_line, bytes)
                    else str(raw_line or "")
                ).strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError as error:
                    raise RuntimeError(
                        "Qwen batch synthesis returned invalid NDJSON."
                    ) from error
                if not isinstance(event, dict) or event.get("type") != "item":
                    continue
                item_id = str(event.get("id") or "").strip()
                if event.get("status") == "completed":
                    try:
                        audio_bytes = base64.b64decode(
                            str(event.get("audio_base64") or ""),
                            validate=True,
                        )
                        audio = decode_audio(
                            audio_bytes,
                            format_hint=str(event.get("response_format") or "wav"),
                        )
                    except (ValueError, TypeError) as error:
                        raise RuntimeError(
                            f"Qwen batch item '{item_id}' returned invalid audio."
                        ) from error
                    yield {
                        "id": item_id,
                        "audio": audio,
                        "error": None,
                    }
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    continue
                error_payload = event.get("error")
                if not isinstance(error_payload, dict):
                    error_payload = {"detail": "Qwen batch item failed."}
                yield {
                    "id": item_id,
                    "audio": None,
                    "error": error_payload,
                }
                if cancel_event is not None and cancel_event.is_set():
                    return
        return

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError(
        f"No Qwen3 TTS batch endpoint could be resolved for '{normalized_base_url}'."
    )
