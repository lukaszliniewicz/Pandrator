"""Canonical queue resource identities and TTS execution declarations."""

from __future__ import annotations

from typing import Any

from pandrator.logic.tts_service_identity import normalize_service_id

_GPU_DEFAULT_ALIASES = frozenset(
    {
        "gpu:default",
        "gpu:0",
        "gpu:auto",
        "gpu:best",
        "gpu:cuda",
        "gpu:vulkan",
        "gpu:metal",
        "gpu:gpu",
        "gpu:mps",
        "gpu:rocm",
        "gpu:hip",
        "gpu:dml",
    }
)
_GPU_DEFAULT = "gpu:default"
_TTS_RESOURCE_PREFIX = "service:tts:"


def canonical_resource_key(key: str) -> str:
    """Normalize known aliases while preserving unrelated resource identity."""
    value = key.strip()
    if value in _GPU_DEFAULT_ALIASES:
        return _GPU_DEFAULT
    if value.startswith(_TTS_RESOURCE_PREFIX):
        service_id = normalize_service_id(value[len(_TTS_RESOURCE_PREFIX) :])
        return f"{_TTS_RESOURCE_PREFIX}{service_id}"
    return value


def tts_resource_keys(settings: dict[str, Any]) -> list[str]:
    """Declare TTS service serialization and any local accelerator use.

    The catalogue lookup is a sparse metadata projection. It does not discover
    models or contact the selected service.
    """
    from pandrator.logic.tts_service_catalogue import get_service_config

    selected_service = normalize_service_id(settings.get("service") or "tts")
    service = get_service_config(
        settings,
        selected_service,
        _fields=frozenset({"id", "kind"}),
    )
    service_id = normalize_service_id(
        service.get("id") if service is not None else selected_service
    )
    keys = [f"{_TTS_RESOURCE_PREFIX}{service_id}"]

    compute = str(settings.get("compute_backend") or settings.get("device") or "auto")
    if service is not None and str(service.get("kind") or "").strip().lower() == "local":
        if compute.strip().lower() != "cpu":
            keys.append(_GPU_DEFAULT)
    return keys
