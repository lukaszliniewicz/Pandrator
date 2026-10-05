"""Stateless HTTP catalogue operations for the Silero TTS provider."""

import logging
from collections.abc import Callable

import requests


def get_silero_model_catalog(
    base_url: str,
    *,
    _normalize_base_url: Callable[[str, str], str],
    default_base_url: Callable[[], str],
) -> list[dict[str, object]]:
    """Return model metadata, including installation and licence state."""
    try:
        response = requests.get(
            f"{_normalize_base_url(base_url, default_base_url())}/v1/models",
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            return []
        return [dict(item) for item in data if isinstance(item, dict) and item.get("id")]
    except (requests.exceptions.RequestException, ValueError) as exc:
        logging.error("Failed to fetch Silero models: %s", exc)
        return []


def get_silero_voice_catalog(
    base_url: str,
    *,
    model: str = "",
    language: str = "",
    include_unavailable: bool = False,
    _normalize_base_url: Callable[[str, str], str],
    default_base_url: Callable[[], str],
    normalize_silero_language_code: Callable[[object], str],
) -> list[dict[str, object]]:
    params = {
        "model": str(model or "").strip(),
        "language": normalize_silero_language_code(language),
        "include_unavailable": str(bool(include_unavailable)).lower(),
    }
    params = {key: value for key, value in params.items() if value != ""}
    try:
        response = requests.get(
            f"{_normalize_base_url(base_url, default_base_url())}/v1/audio/voices",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            return []
        return [dict(item) for item in data if isinstance(item, dict) and item.get("id")]
    except (requests.exceptions.RequestException, ValueError) as exc:
        logging.error("Failed to fetch Silero voices: %s", exc)
        return []
