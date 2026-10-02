"""Resolve and validate language support for the selected TTS route/model."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from importlib import import_module
from typing import Any

from .language_capabilities import (
    canonical_language_tag,
    language_decision,
    require_supported_language,
)
from .tts_language_support import tts_language_support

_KNOWN_PROVIDER_IDS = frozenset(
    {
        "audio_cpp",
        "azure",
        "chatterbox",
        "fishs2",
        "gemini",
        "kokoro",
        "kobold_qwen",
        "magpie",
        "openai",
        "silero",
        "vertex_ai",
        "voxcpm",
        "voxtral",
        "xtts",
        "elevenlabs",
    }
)
_CUSTOM_ADAPTERS = frozenset({"generic_json", "openai_compatible"})
_ADAPTER_PROVIDER_IDS = {
    "audio_cpp": "audio_cpp",
    "azure_speech": "azure",
    "elevenlabs_native": "elevenlabs",
}


def _selected_endpoint(
    settings: Mapping[str, Any], endpoint: Mapping[str, Any] | None, cache: dict | None
) -> tuple[dict[str, Any], str]:
    if endpoint is not None:
        selected = dict(endpoint)
        return selected, str(selected.get("id") or selected.get("name") or "")

    tts_handler = import_module("pandrator.logic.tts_handler")

    service = str(settings.get("service") or settings.get("tts_service") or "XTTS")
    selected = tts_handler.get_service_config(settings, service, _cache=cache)
    selected_id = str((selected or {}).get("id") or "")

    if selected is None:
        endpoint_name = str(settings.get("openai_audio_endpoint") or "").strip()
        if endpoint_name:
            selected = tts_handler.get_service_config(
                settings, endpoint_name, _cache=cache
            )
            selected_id = str((selected or {}).get("id") or endpoint_name)

    if selected is None:
        # Custom OpenAI-compatible configurations are routed by the selected
        # endpoint, or by the same first configured endpoint as synthesis.
        candidate, _error = tts_handler.resolve_openai_audio_endpoint(
            dict(settings), _cache=cache
        )
        if candidate is not None:
            selected = dict(candidate)
            selected_id = str(
                selected.get("id")
                or selected.get("name")
                or settings.get("openai_audio_endpoint")
                or ""
            )
    return dict(selected or {}), selected_id or service


def _provider_identity(endpoint: Mapping[str, Any], selector: str) -> tuple[str, str]:
    adapter = str(endpoint.get("adapter") or "").strip().casefold().replace("-", "_")
    raw_provider = str(endpoint.get("provider") or "").strip().casefold().replace("-", "_")
    endpoint_id = str(endpoint.get("id") or selector or "").strip().casefold().replace("-", "_")

    if adapter in _CUSTOM_ADAPTERS:
        # A generic transport does not prove which upstream company will
        # receive the request. Do not infer a provider from a URL or name.
        return adapter, adapter
    if adapter in _ADAPTER_PROVIDER_IDS:
        return _ADAPTER_PROVIDER_IDS[adapter], adapter
    if raw_provider in _KNOWN_PROVIDER_IDS:
        return raw_provider, adapter or raw_provider
    if endpoint_id in _KNOWN_PROVIDER_IDS:
        return endpoint_id, adapter or endpoint_id
    provider_id = adapter or endpoint_id or "unknown"
    return provider_id, adapter or provider_id


def _selected_model(
    settings: Mapping[str, Any], endpoint: Mapping[str, Any], provider_id: str
) -> str:
    tts_handler = import_module("pandrator.logic.tts_handler")

    if provider_id == "silero":
        model = (
            settings.get("silero_model")
            or settings.get("xtts_model")
            or settings.get("model")
            or endpoint.get("default_model")
            or tts_handler.SILERO_DEFAULT_MODEL
        )
    elif provider_id == "elevenlabs":
        model = (
            settings.get("elevenlabs_model")
            or settings.get("xtts_model")
            or settings.get("model")
            or endpoint.get("default_model")
            or tts_handler.ELEVENLABS_TTS_DEFAULT_MODEL
        )
    else:
        model = (
            settings.get("xtts_model")
            or settings.get("model")
            or endpoint.get("default_model")
        )
    return str(model or "").strip()


def _exact_catalogue_model(
    endpoint: Mapping[str, Any], model_id: str
) -> dict[str, Any]:
    catalogue = endpoint.get("model_catalog")
    if not isinstance(catalogue, list):
        return {}
    return next(
        (
            dict(item)
            for item in catalogue
            if isinstance(item, Mapping) and item.get("id") == model_id
        ),
        {},
    )


def resolve_tts_language_support(
    settings: Mapping[str, Any],
    endpoint: Mapping[str, Any] | None = None,
    _service_config_cache: dict | None = None,
) -> dict[str, Any] | None:
    """Return portable language evidence for the selected TTS model, if known.

    Endpoint configuration is used only to identify the effective provider,
    model, route and the exact model-catalogue entry. It is never copied into
    the returned record.
    """

    selected_endpoint, selector = _selected_endpoint(
        settings, endpoint, _service_config_cache
    )
    provider_id, native_route = _provider_identity(selected_endpoint, selector)
    model_id = _selected_model(settings, selected_endpoint, provider_id)
    if not model_id:
        return None

    if provider_id == "audio_cpp":
        from .audio_cpp_catalogue import package_metadata

        package = package_metadata(model_id)
        if package:
            by_operation = package.get("language_support_by_operation")
            tts_record = by_operation.get("tts") if isinstance(by_operation, Mapping) else None
            if not isinstance(tts_record, Mapping):
                raise ValueError(
                    f"audio.cpp package '{model_id}' has no text-to-speech request route."
                )
            return deepcopy(dict(tts_record))

    metadata = _exact_catalogue_model(selected_endpoint, model_id)
    discovery = (
        "provider_live"
        if metadata.get("language_discovery") == "provider_live"
        else "static"
    )
    return tts_language_support(
        provider_id,
        model_id,
        adapter=str(selected_endpoint.get("adapter") or ""),
        metadata=metadata,
        operation="tts",
        native_route=native_route,
        discovery=discovery,
    )


def _route_consumes_language(record: Mapping[str, Any]) -> bool:
    route = str(record.get("native_route") or "").casefold()
    return route == "audio_cpp" or route.startswith("audio_cpp:") or route in {
        "silero",
        "silero_tts",
    }


def validate_tts_language(
    settings: Mapping[str, Any],
    endpoint: Mapping[str, Any] | None = None,
    _service_config_cache: dict | None = None,
) -> dict[str, Any]:
    """Validate the canonical language against exact selected-model evidence."""

    raw_language = settings.get("language") or settings.get("target_language")
    language = canonical_language_tag(raw_language, allow_auto=True)
    support = resolve_tts_language_support(
        settings,
        endpoint=endpoint,
        _service_config_cache=_service_config_cache,
    )
    if support is None:
        return {
            "language": language,
            "decision": "unverified",
            "language_support": None,
            "native_language": language,
        }

    decision = language_decision(support, language)
    require_supported_language(support, language)
    native_language = language
    if _route_consumes_language(support):
        aliases = support.get("request_aliases")
        if isinstance(aliases, Mapping):
            candidate = aliases.get(language)
            if isinstance(candidate, str) and candidate.strip():
                native_language = candidate
    return {
        "language": language,
        "decision": decision,
        "language_support": support,
        "native_language": native_language,
    }
