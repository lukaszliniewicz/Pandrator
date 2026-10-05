"""Stateless HTTP operations for discovering XTTS voices and models."""

import logging
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from typing import Protocol

import requests


class FileIdsExtractor(Protocol):
    def __call__(self, payload: object, *, allowed_purposes: AbstractSet[str]) -> list[str]: ...


def get_xtts_speakers(
    normalized_base_url: str,
    *,
    _openai_voice_catalog_urls: Callable[[str], list[str]],
    _openai_auth_headers: Callable[[], dict[str, str]],
    _should_try_next_openai_candidate: Callable[[int], bool],
    _extract_voices_from_openai_payload: Callable[[object], list[str]],
    _openai_files_urls: Callable[[str], list[str]],
    _extract_file_ids_from_openai_payload: FileIdsExtractor,
    discoverable_file_purposes: Callable[[], tuple[str, ...]],
    _dedupe_ordered: Callable[[Iterable[object]], list[str]],
) -> list[str]:
    """Fetches discoverable XTTS voice identifiers from server."""
    # Preferred path: voice catalog endpoints (/v1/audio/voices, /v1/voices).
    discovered_voice_ids: list[str] = []
    for voices_url in _openai_voice_catalog_urls(normalized_base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_voice_ids = _extract_voices_from_openai_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not fetch voices from %s: %s", voices_url, e)
            continue

    discovered_file_ids: list[str] = []
    discoverable_purposes = set(discoverable_file_purposes())

    # Legacy path: OpenAI-compatible files endpoint (/v1/files).
    for purpose in discoverable_file_purposes():
        for files_url in _openai_files_urls(normalized_base_url):
            try:
                response = requests.get(
                    files_url,
                    headers=_openai_auth_headers(),
                    params={"purpose": purpose, "limit": 10000},
                    timeout=8,
                )
                if _should_try_next_openai_candidate(response.status_code):
                    continue

                response.raise_for_status()
                discovered_file_ids.extend(
                    _extract_file_ids_from_openai_payload(
                        response.json(),
                        allowed_purposes=discoverable_purposes,
                    )
                )
                break
            except (requests.exceptions.RequestException, ValueError) as e:
                logging.debug("Could not fetch files from %s: %s", files_url, e)
                continue

    return _dedupe_ordered(discovered_voice_ids + discovered_file_ids)


def get_xtts_models(
    normalized_base_url: str,
    *,
    _openai_models_urls: Callable[[str], list[str]],
    _openai_auth_headers: Callable[[], dict[str, str]],
    _should_try_next_openai_candidate: Callable[[int], bool],
    _extract_models_from_openai_payload: Callable[[object], list[str]],
    default_model: Callable[[], str],
    _merge_catalog_with_discovered: Callable[[list[str], list[str]], list[str]],
) -> list[str]:
    """Fetches available XTTS models from server."""
    discovered_models: list[str] = []

    for models_url in _openai_models_urls(normalized_base_url):
        try:
            response = requests.get(
                models_url,
                headers=_openai_auth_headers(),
                timeout=8,
            )
            if _should_try_next_openai_candidate(response.status_code):
                continue

            response.raise_for_status()
            discovered_models = _extract_models_from_openai_payload(response.json())
            break
        except (requests.exceptions.RequestException, ValueError) as e:
            logging.debug("Could not fetch models from %s: %s", models_url, e)
            continue

    return _merge_catalog_with_discovered([default_model()], discovered_models)
