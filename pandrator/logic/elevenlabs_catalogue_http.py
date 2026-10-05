"""HTTP catalogue fetching for ElevenLabs models and voices."""

import logging
from collections.abc import Callable
from typing import Protocol

import requests


class CatalogErrorFactory(Protocol):
    def __call__(
        self,
        operation: str,
        status_code: int = 0,
        *,
        incomplete: bool = False,
    ) -> RuntimeError: ...


def get_elevenlabs_model_catalog(
    base_url: str,
    *,
    api_key: str = "",
    strict: bool = False,
    normalize_base_url: Callable[[str], str],
    auth_headers: Callable[[str], dict[str, str]],
    catalog_status: Callable[[BaseException], int],
    error_factory: Callable[[], CatalogErrorFactory],
) -> list[dict[str, object]]:
    """Fetch the currently available TTS models and authoritative languages."""
    url = f"{normalize_base_url(base_url)}/v1/models"
    try:
        response = requests.get(
            url,
            headers=auth_headers(api_key),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError) as error:
        if strict:
            raise error_factory()("models", catalog_status(error)) from error
        logging.warning("Could not list ElevenLabs models: %s", error)
        return []

    if not isinstance(payload, list):
        if strict:
            raise error_factory()("models")
        return []
    models: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict) or item.get("can_do_text_to_speech") is False:
            continue
        model_id = str(item.get("model_id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        entry: dict[str, object] = {
            "id": model_id,
            "model_id": model_id,
            "name": str(item.get("name") or model_id).strip(),
        }
        languages = item.get("languages")
        if isinstance(languages, list):
            authoritative_languages = []
            for language in languages:
                if not isinstance(language, dict):
                    continue
                language_id = str(language.get("language_id") or "").strip()
                name = str(language.get("name") or "").strip()
                if language_id:
                    authoritative_languages.append(
                        {"language_id": language_id, **({"name": name} if name else {})}
                    )
            if authoritative_languages:
                entry["languages"] = authoritative_languages
        description = str(item.get("description") or "").strip()
        if description:
            entry["description"] = description
        models.append(entry)
    return models


def get_elevenlabs_voice_catalog(
    base_url: str,
    *,
    api_key: str = "",
    strict: bool = False,
    normalize_base_url: Callable[[str], str],
    auth_headers: Callable[[str], dict[str, str]],
    catalog_status: Callable[[BaseException], int],
    error_factory: Callable[[], CatalogErrorFactory],
) -> list[dict[str, object]]:
    """Fetch voice IDs and metadata from ElevenLabs' current v2 voices API."""
    url = f"{normalize_base_url(base_url)}/v2/voices"
    params: dict[str, str | int] = {"show_legacy": "true", "page_size": 100}
    voices: list[dict[str, object]] = []
    seen: set[str] = set()
    seen_page_tokens: set[str] = set()
    incomplete = False
    try:
        for _page in range(20):
            response = requests.get(
                url,
                headers=auth_headers(api_key),
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                if strict:
                    raise error_factory()("voices")
                break
            data = payload.get("voices")
            if not isinstance(data, list):
                if strict:
                    raise error_factory()("voices")
                break
            for item in data:
                if not isinstance(item, dict):
                    continue
                voice_id = str(item.get("voice_id") or "").strip()
                if not voice_id or voice_id in seen:
                    continue
                seen.add(voice_id)
                voices.append(dict(item))
            if payload.get("has_more") is False:
                break
            next_page = str(payload.get("next_page_token") or "").strip()
            if not next_page:
                incomplete = payload.get("has_more") is True
                break
            if next_page in seen_page_tokens:
                incomplete = True
                break
            seen_page_tokens.add(next_page)
            params["next_page_token"] = next_page
        else:
            incomplete = True
    except (requests.exceptions.RequestException, ValueError) as error:
        if strict:
            raise error_factory()("voices", catalog_status(error)) from error
        logging.warning("Could not list ElevenLabs voices: %s", error)
    if incomplete:
        if strict:
            raise error_factory()("voices", incomplete=True)
        logging.warning("Incomplete ElevenLabs voice catalogue: pagination did not finish.")
    return voices
