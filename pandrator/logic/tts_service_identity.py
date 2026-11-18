"""Canonical TTS service identifiers shared by catalogues and queue coordination."""

from __future__ import annotations

from functools import lru_cache

# Spelling aliases that are not display names. Display names come from the
# catalogue, so a stored snapshot carrying "Google Vertex AI" names the same
# service as "vertex_ai" without duplicating the catalogue here.
_ALIASES = {
    "qwen3_tts": "kobold_qwen",
    "qwen3": "kobold_qwen",
    "qwen": "kobold_qwen",
    "kobold_qwen3": "kobold_qwen",
    "audio.cpp": "audio_cpp",
    "audio-cpp": "audio_cpp",
    "audiocpp": "audio_cpp",
    "openai_compatible": "openai_compatible",
}


def _normalize(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


@lru_cache(maxsize=1)
def _catalogue_names() -> dict[str, str]:
    """Map every catalogue display name, normalized, to its service ID."""

    from pandrator.logic.tts_service_catalogue import FIRST_CLASS_SERVICE_NAMES

    names: dict[str, str] = {}
    for service_id, display_name in FIRST_CLASS_SERVICE_NAMES.items():
        names.setdefault(_normalize(display_name), service_id)
        # A catalogue ID spelled out still resolves to itself.
        names.setdefault(_normalize(service_id), service_id)
    return names


def normalize_service_id(value: object) -> str:
    normalized = _normalize(value)
    canonical = _ALIASES.get(normalized)
    if canonical is not None:
        return canonical
    return _catalogue_names().get(normalized, normalized)


def service_display_name(value: object) -> str:
    """Return the catalogue name of a service, or the caller's spelling."""

    from pandrator.logic.tts_service_catalogue import FIRST_CLASS_SERVICE_NAMES

    return FIRST_CLASS_SERVICE_NAMES.get(normalize_service_id(value), str(value or ""))
