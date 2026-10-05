"""Stateless HTTP operations for deleting OpenAI-compatible provider voices."""

from collections.abc import Callable
from typing import Protocol
from urllib.parse import quote

import requests


class _VoiceExistenceCheck(Protocol):
    def __call__(self, voice_id: str, *, base_url: str, api_key: str = "") -> bool | None: ...


def _voice_catalog_can_confirm_absence(payload: object) -> bool:
    """Only an interpretable catalogue can establish that a voice is absent."""

    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        if payload.get("error"):
            return False
        collections = [payload[key] for key in ("data", "voices") if key in payload]
        if not collections or any(not isinstance(items, list) for items in collections):
            return False
        candidates = [item for items in collections for item in items]
    else:
        return False

    for candidate in candidates:
        identifier = (
            candidate.get("voice_id") or candidate.get("id") or candidate.get("name")
            if isinstance(candidate, dict)
            else candidate
        )
        if not isinstance(identifier, (str, int, float)) or not str(identifier or "").strip():
            return False
    return True


def _remote_voice_exists(
    voice_id: str,
    *,
    base_url: str,
    api_key: str = "",
    _openai_voice_catalog_urls: Callable[[str], list[str]],
    _openai_auth_headers: Callable[[str], dict[str, str]],
    _should_try_next_openai_candidate: Callable[[int], bool],
    _extract_voices_from_openai_payload: Callable[[object], list[str]],
    _voice_catalog_can_confirm_absence: Callable[[object], bool],
) -> bool | None:
    """Verify a remote voice after an idempotent or unsupported DELETE."""

    expected = str(voice_id or "").strip().casefold()
    for voices_url in _openai_voice_catalog_urls(base_url):
        try:
            response = requests.get(
                voices_url,
                headers=_openai_auth_headers(api_key),
                timeout=8,
            )
        except requests.exceptions.RequestException:
            continue
        if _should_try_next_openai_candidate(response.status_code):
            continue
        if response.status_code >= 400:
            continue
        try:
            payload = response.json()
            discovered = _extract_voices_from_openai_payload(payload)
        except ValueError:
            continue
        if any(str(item).strip().casefold() == expected for item in discovered):
            return True
        if _voice_catalog_can_confirm_absence(payload):
            return False
    return None


def _delete_speaker_voice_openai_compatible(
    voice_id: str,
    *,
    base_url: str,
    fallback_base_url: str,
    service_name: str,
    api_key: str = "",
    _normalize_base_url: Callable[[str, str], str],
    _openai_voice_catalog_urls: Callable[[str], list[str]],
    _openai_auth_headers: Callable[[str], dict[str, str]],
    _remote_voice_exists: _VoiceExistenceCheck,
) -> bool:
    """Delete an uploaded voice without confusing an absent route with an absent voice."""

    normalized_voice_id = str(voice_id or "").strip()
    if not normalized_voice_id:
        raise ValueError("Provider voice deletion requires a voice ID.")
    if (
        len(normalized_voice_id) > 255
        or normalized_voice_id in {".", ".."}
        or any(character in normalized_voice_id for character in ("/", "\\", "\x00"))
        or any(ord(character) < 32 for character in normalized_voice_id)
    ):
        raise ValueError("Provider voice ID is not safe to delete.")
    normalized_base_url = _normalize_base_url(base_url, fallback_base_url)
    encoded_voice_id = quote(normalized_voice_id, safe="")
    unsupported_responses = 0
    missing_responses = 0

    try:
        for collection_url in _openai_voice_catalog_urls(normalized_base_url):
            response = requests.delete(
                f"{collection_url.rstrip('/')}/{encoded_voice_id}",
                headers=_openai_auth_headers(api_key),
                timeout=30,
            )
            if 200 <= response.status_code < 300:
                return True
            if response.status_code in {404, 410}:
                missing_responses += 1
                continue
            if response.status_code in {405, 501}:
                unsupported_responses += 1
                continue
            raise RuntimeError(
                f"{service_name} voice deletion failed ({response.status_code}): {response.text}"
            )
    except requests.exceptions.RequestException as error:
        raise RuntimeError(
            f"Failed deleting voice from {service_name} server {normalized_base_url}: {error}"
        ) from error

    remote_exists = _remote_voice_exists(
        normalized_voice_id,
        base_url=normalized_base_url,
        api_key=api_key,
    )
    if remote_exists is False:
        # A prior deletion or manual cleanup is success from the caller's point
        # of view. The follow-up catalogue check keeps this from silently
        # accepting a 404 caused by an unimplemented route.
        return False
    if remote_exists is True:
        raise RuntimeError(
            f"{service_name} still lists voice '{normalized_voice_id}' and does "
            "not appear to support provider-side deletion."
        )
    detail = (
        "did not expose a supported voice deletion endpoint"
        if unsupported_responses
        else "returned a missing response that could not be verified"
    )
    raise RuntimeError(
        f"{service_name} {detail} for voice '{normalized_voice_id}' "
        f"({missing_responses} missing response(s))."
    )
