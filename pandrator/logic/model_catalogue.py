"""Static, provider-neutral catalogue of TTS models known to Pandrator.

The catalogue describes models registered in Pandrator's defaults and provider
profiles. It never probes a provider, reports installation state, or includes
connection settings or credentials.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from ..constants import (
    FISHS2_LANGUAGES,
    KOKORO_LANGUAGES,
    MAGPIE_LANGUAGES,
    QWEN_LANGUAGES,
    VOXTRAL_LANGUAGES,
    XTTS_LANGUAGES,
)
from .audio_cpp_catalogue import inventory as audio_cpp_inventory
from .audio_cpp_catalogue import package_metadata

_OPENAI_TTS_SOURCE = "https://developers.openai.com/api/docs/guides/text-to-speech"
_GEMINI_TTS_SOURCE = "https://ai.google.dev/gemini-api/docs/speech-generation"
_VERTEX_TTS_SOURCE = "https://docs.cloud.google.com/text-to-speech/docs/gemini-tts"
_AZURE_TTS_SOURCE = "https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-voices"

_OFFICIAL_PROVIDER_SOURCES = {
    "openai": [_OPENAI_TTS_SOURCE],
    "gemini": [_GEMINI_TTS_SOURCE],
    "vertex_ai": [_VERTEX_TTS_SOURCE],
}
_KNOWN_GEMINI_MODELS = {
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-tts",
    "gemini-2.5-pro-tts",
}
_AZURE_SPEECH_ADAPTER = "azure_speech"
_AUDIO_CPP_ADAPTER = "audio_cpp"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _model_metadata(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    catalog = profile.get("model_catalog")
    if not isinstance(catalog, list):
        return {}
    return {
        str(item["id"]): copy.deepcopy(item)
        for item in catalog
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _profile_identity(
    profile: Mapping[str, Any], default_provider_ids: set[str]
) -> tuple[str, str, str]:
    profile_id = str(profile.get("id") or "").strip()
    adapter = str(profile.get("adapter") or "").strip()
    if adapter == _AZURE_SPEECH_ADAPTER:
        return "azure", str(profile.get("name") or "Azure Speech"), "commercial"
    if profile_id in default_provider_ids:
        return (
            profile_id,
            str(profile.get("name") or profile_id),
            "",
        )
    kind = str(profile.get("kind") or "custom").strip() or "custom"
    return profile_id, str(profile.get("name") or profile_id), kind


def _profile_rows(
    profiles: list[dict[str, Any]], default_provider_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    providers: dict[str, dict[str, Any]] = {}
    metadata: dict[tuple[str, str], dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("adapter") or "") == _AUDIO_CPP_ADAPTER:
            # The pinned audio.cpp inventory below is the single source for
            # those package rows; the endpoint profile is only a connection.
            continue
        provider_id, name, kind = _profile_identity(profile, default_provider_ids)
        if not provider_id:
            continue
        provider = providers.setdefault(
            provider_id,
            {"id": provider_id, "name": name, "kind": kind},
        )
        # A canonical default config owns its stable provider name and kind.
        if provider_id not in default_provider_ids and kind:
            provider["kind"] = kind

        model_catalog = _model_metadata(profile)
        model_ids = _string_list(profile.get("models"))
        model_ids.extend(model for model in model_catalog if model not in model_ids)
        default_model = str(profile.get("default_model") or "").strip()
        if default_model and default_model not in model_ids:
            model_ids.append(default_model)

        source_url = str(profile.get("source_url") or "").strip()
        all_voice_metadata = profile.get("voice_metadata")
        for model_id in model_ids:
            key = (provider_id, model_id)
            row = metadata.setdefault(key, {})
            explicit = model_catalog.get(model_id, {})
            row["_adapter"] = str(profile.get("adapter") or "")
            for field in (
                "label",
                "name",
                "family",
                "family_label",
                "category",
                "description",
                "voice_mode",
                "supported_languages",
                "capabilities",
                "pandrator_features",
                "upstream_features",
                "recommended_for",
                "license",
                "package_availability",
            ):
                if field in explicit:
                    row[field] = copy.deepcopy(explicit[field])
            profile_modes = profile.get("model_voice_modes")
            if isinstance(profile_modes, Mapping) and model_id in profile_modes:
                row.setdefault("voice_mode", str(profile_modes[model_id] or ""))
            row_sources = _string_list(explicit.get("sources"))
            if not row_sources and explicit.get("source_url"):
                row_sources = _string_list([explicit["source_url"]])
            if not row_sources and source_url:
                row_sources = [source_url]
            if row_sources:
                row["sources"] = list(dict.fromkeys([*row.get("sources", []), *row_sources]))
            if (
                str(profile.get("adapter") or "") == _AZURE_SPEECH_ADAPTER
                and profile.get("supports_prebuilt_voices") is True
            ):
                row.setdefault("voice_mode", "prebuilt")
            if isinstance(all_voice_metadata, dict):
                model_voices = {
                    key: copy.deepcopy(value)
                    for key, value in all_voice_metadata.items()
                    if isinstance(value, dict) and value.get("model") == model_id
                }
                if model_voices:
                    row["voice_metadata"] = model_voices
            if model_id == default_model and not row.get("recommended_for"):
                row["recommended_for"] = "Default starting model"
    return providers, metadata


def _normalized_voice_mode(
    model_id: str,
    model_metadata: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    *,
    allow_provider_defaults: bool,
) -> str:
    explicit = str(model_metadata.get("voice_mode") or "").strip()
    if explicit:
        return explicit

    if config is None:
        return "unknown"
    modes = config.get("model_voice_modes")
    if isinstance(modes, Mapping) and model_id in modes:
        mode = str(modes[model_id] or "").strip()
        if mode:
            return mode

    # Only the canonical model IDs in _default_service_configs() reach this
    # fallback. Arbitrary provider profiles must state per-model metadata.
    if not allow_provider_defaults:
        return "unknown"
    supports_prebuilt = config.get("supports_prebuilt_voices")
    supports_cloning = config.get("supports_voice_cloning")
    has_prebuilt = supports_prebuilt is True
    has_cloning = supports_cloning is True
    if has_prebuilt and has_cloning:
        return "hybrid"
    if has_prebuilt:
        return "prebuilt"
    if has_cloning:
        return "cloning"
    return "unknown"


def _feature_capabilities(
    model_id: str,
    *,
    provider_id: str,
    adapter: str,
    family: str,
    voice_mode: str,
    model_metadata: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    allow_provider_defaults: bool,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    from .speech_performance import capabilities_for_model

    backend = adapter or provider_id
    if config is not None:
        backend = str(config.get("adapter") or config.get("provider") or config.get("id") or backend)
    control_profile = capabilities_for_model(
        model_id,
        backend=backend,
        family=family,
        voice_mode=voice_mode,
    )

    capabilities = set(_string_list(model_metadata.get("capabilities")))
    if voice_mode in {"prebuilt", "hybrid"}:
        capabilities.add("prebuilt_voices")
    if voice_mode in {"cloning", "optional_cloning", "hybrid"}:
        capabilities.add("voice_cloning")
    if control_profile.get("instructions") not in {None, "", "none"}:
        capabilities.add("instructions")
    emotion = control_profile.get("emotion")
    if isinstance(emotion, Mapping) and emotion.get("mode") not in {None, "", "none"}:
        capabilities.add("emotion_control")
    if control_profile.get("voice_design") is True or voice_mode == "design":
        capabilities.add("voice_design")

    # Gemini's current app path has no verified vocal-event tag dialect. The
    # general speech capability helper currently describes upstream tags, so
    # omit them from this provider-neutral app capability view.
    gemini_route = backend.casefold() in {"gemini", "vertex_ai", "google_gemini", "google_vertex_ai"}
    if control_profile.get("event_tags") and not gemini_route:
        capabilities.add("vocal_events")

    # Preserve only feature claims explicitly attached to this model or
    # backed by one of the canonical provider documentation profiles.
    upstream = model_metadata.get("upstream_features")
    if isinstance(upstream, dict):
        upstream_features = copy.deepcopy(upstream)
    else:
        upstream_features = {}
        if (
            provider_id == "openai" and model_id == "gpt-4o-mini-tts"
        ) or (
            provider_id in {"gemini", "vertex_ai"}
            and model_id in _KNOWN_GEMINI_MODELS
        ):
            upstream_features.update(instructions=True, emotion_control=True)
        elif provider_id == "azure" and adapter == _AZURE_SPEECH_ADAPTER:
            voice_metadata = model_metadata.get("voice_metadata")
            if isinstance(voice_metadata, dict) and any(
                isinstance(value, dict) and value.get("styles")
                for value in voice_metadata.values()
            ):
                upstream_features["emotion_control"] = True

    pandrator = model_metadata.get("pandrator_features")
    if isinstance(pandrator, dict):
        pandrator_features = copy.deepcopy(pandrator)
    else:
        pandrator_features = {}
    pandrator_features.setdefault("speech_generation", "request_supported")
    pandrator_features.setdefault(
        "instructions", control_profile.get("instructions", "none")
    )
    pandrator_features.setdefault(
        "voice_design",
        "documented" if control_profile.get("voice_design") else "none",
    )
    pandrator_features.setdefault(
        "emotion_control",
        emotion.get("mode", "none") if isinstance(emotion, Mapping) else "none",
    )
    if control_profile.get("event_tags") and not gemini_route:
        pandrator_features.setdefault(
            "vocal_events", ", ".join(control_profile["event_tags"].keys())
        )
    else:
        pandrator_features.setdefault("vocal_events", "none")

    if provider_id == "azure" and adapter == _AZURE_SPEECH_ADAPTER:
        # MAI Voice exposes SSML expressive styles; capabilities_for_model has
        # no Azure route yet, so project the exact adapter's documented
        # discrete-style control without widening other Azure profiles.
        has_styles = upstream_features.get("emotion_control") is True
        if has_styles:
            capabilities.add("emotion_control")
            pandrator_features["emotion_control"] = "discrete_styles"
        else:
            pandrator_features["emotion_control"] = "none"
        pandrator_features.update(
            instructions="none",
            voice_design="none",
            vocal_events="none",
        )
        capabilities.difference_update(
            {"instructions", "voice_design", "voice_cloning", "vocal_events"}
        )

    # A generic profile's coarse service-level flags do not establish support
    # for its individual model. It may retain explicit model_catalog claims.
    explicit_model_mode = bool(str(model_metadata.get("voice_mode") or "").strip())
    if not allow_provider_defaults and not model_metadata.get("capabilities"):
        if not explicit_model_mode:
            capabilities.difference_update(
                {"prebuilt_voices", "voice_cloning", "voice_design"}
            )
        capabilities.difference_update(
            {"instructions", "emotion_control", "vocal_events"}
        )
        if not model_metadata.get("upstream_features"):
            upstream_features = {}
        if not isinstance(model_metadata.get("pandrator_features"), dict):
            pandrator_features = {
                "speech_generation": "request_supported",
                "instructions": "none",
                "voice_design": "none",
                "emotion_control": "none",
                "vocal_events": "none",
            }

    return sorted(capabilities), pandrator_features, upstream_features


def _supported_languages(
    model_metadata: Mapping[str, Any],
    *,
    provider_id: str,
    allow_provider_defaults: bool,
) -> list[str]:
    explicit = _string_list(model_metadata.get("supported_languages"))
    if explicit:
        return explicit
    voice_metadata = model_metadata.get("voice_metadata")
    if isinstance(voice_metadata, dict):
        locales = sorted(
            {
                str(value.get("locale")).strip()
                for value in voice_metadata.values()
                if isinstance(value, dict) and value.get("locale")
            },
            key=str.casefold,
        )
        if locales:
            return locales
    if not allow_provider_defaults:
        return []
    canonical_languages = {
        "xtts": XTTS_LANGUAGES,
        "fishs2": FISHS2_LANGUAGES,
        "voxtral": VOXTRAL_LANGUAGES,
        "kokoro": KOKORO_LANGUAGES,
        "magpie": MAGPIE_LANGUAGES,
        "kobold_qwen": QWEN_LANGUAGES,
    }
    return list(canonical_languages.get(provider_id, []))


def _build_item(
    *,
    provider_id: str,
    provider_name: str,
    provider_kind: str,
    model_id: str,
    model_metadata: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    adapter: str = "",
    allow_provider_defaults: bool = False,
) -> dict[str, Any]:
    family = str(model_metadata.get("family") or provider_id)
    family_label = str(
        model_metadata.get("family_label")
        or model_metadata.get("display_name")
        or provider_name
    )
    voice_mode = _normalized_voice_mode(
        model_id,
        model_metadata,
        config,
        allow_provider_defaults=allow_provider_defaults,
    )
    effective_adapter = adapter or str((config or {}).get("adapter") or "")
    capabilities, pandrator_features, upstream_features = _feature_capabilities(
        model_id,
        provider_id=provider_id,
        adapter=effective_adapter,
        family=family,
        voice_mode=voice_mode,
        model_metadata=model_metadata,
        config=config,
        allow_provider_defaults=allow_provider_defaults,
    )
    if config is not None and config.get("kind") == "local":
        availability = {"status": "managed_service", "reason": "Manage via this Pandrator provider."}
    else:
        availability = {
            "status": "external_service",
            "reason": "External provider; credential and provider setup may be required.",
        }
    if isinstance(model_metadata.get("package_availability"), dict):
        availability = copy.deepcopy(model_metadata["package_availability"])

    sources = _string_list(model_metadata.get("sources"))
    if not sources and provider_id in _OFFICIAL_PROVIDER_SOURCES:
        sources = list(_OFFICIAL_PROVIDER_SOURCES[provider_id])
    if not sources and provider_id == "azure" and effective_adapter == _AZURE_SPEECH_ADAPTER:
        sources = [_AZURE_TTS_SOURCE]
    if provider_id == "azure" and effective_adapter == _AZURE_SPEECH_ADAPTER:
        sources = list(dict.fromkeys([_AZURE_TTS_SOURCE, *sources]))

    result: dict[str, Any] = {
        "id": model_id,
        "provider_id": provider_id,
        "provider_name": provider_name,
        "provider_kind": provider_kind,
        "catalogue_id": f"{provider_id}:{model_id}",
        "family": family,
        "family_label": family_label,
        "label": str(model_metadata.get("label") or model_metadata.get("name") or model_id),
        "category": str(model_metadata.get("category") or "tts"),
        "voice_mode": voice_mode,
        "capabilities": capabilities,
        "supported_languages": _supported_languages(
            model_metadata,
            provider_id=provider_id,
            allow_provider_defaults=allow_provider_defaults,
        ),
        "pandrator_features": copy.deepcopy(pandrator_features),
        "package_availability": availability,
        "commercial_use": str(
            (model_metadata.get("license") or {}).get("commercial_use", "unknown")
            if isinstance(model_metadata.get("license"), dict)
            else model_metadata.get("commercial_use", "unknown")
        ),
        "recommended_for": str(model_metadata.get("recommended_for") or ""),
        "sources": sources,
    }
    if upstream_features:
        result["upstream_features"] = copy.deepcopy(upstream_features)
    for field in (
        "description",
        "upstream_status",
        "tasks",
        "precision",
        "language_note",
        "license",
        "reference_audio",
        "reference_text",
        "estimated_download_bytes",
        "repository_license",
    ):
        if field in model_metadata:
            result[field] = copy.deepcopy(model_metadata[field])
    return result


@lru_cache(maxsize=1)
def _catalogue_rows() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    from .tts_handler import _default_service_configs
    from .tts_provider_profiles import list_tts_provider_profiles

    configs = [
        item
        for item in _default_service_configs()
        if isinstance(item, dict) and item.get("id")
    ]
    default_provider_ids = {str(item["id"]) for item in configs}
    profile_providers, profile_metadata = _profile_rows(
        list_tts_provider_profiles(), default_provider_ids
    )

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    providers: dict[str, dict[str, str]] = {}

    def add_provider(provider_id: str, name: str, kind: str) -> None:
        providers.setdefault(
            provider_id,
            {"id": provider_id, "name": name, "kind": kind},
        )

    # audio.cpp packages are static inventory entries; preserve their
    # canonical package metadata without copying the connection profile.
    for package in audio_cpp_inventory().get("packages", []):
        model_id = str(package.get("id") or "")
        if not model_id:
            continue
        raw = package_metadata(model_id)
        metadata = copy.deepcopy(raw)
        provider_id = "audio_cpp"
        provider_name = "audio.cpp"
        provider_kind = "local"
        add_provider(provider_id, provider_name, provider_kind)
        metadata["commercial_use"] = str(
            (metadata.get("license") or {}).get("commercial_use", "unknown")
            if isinstance(metadata.get("license"), dict)
            else "unknown"
        )
        metadata.update(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_kind=provider_kind,
            catalogue_id=f"{provider_id}:{model_id}",
        )
        rows[(provider_id, model_id)] = metadata

    for config in configs:
        provider_id = str(config["id"])
        # audio.cpp is represented above from its pinned package inventory.
        if provider_id == "audio_cpp":
            continue
        provider_name = str(config.get("name") or provider_id)
        provider_kind = str(config.get("kind") or "unknown")
        add_provider(provider_id, provider_name, provider_kind)
        model_catalog = _model_metadata(config)
        model_ids = _string_list(config.get("models"))
        model_ids.extend(model for model in model_catalog if model not in model_ids)
        default_model = str(config.get("default_model") or "").strip()
        if default_model and default_model not in model_ids:
            model_ids.append(default_model)
        for model_id in model_ids:
            profile_meta = profile_metadata.get((provider_id, model_id), {})
            metadata = {**profile_meta, **model_catalog.get(model_id, {})}
            if model_id == default_model and not metadata.get("recommended_for"):
                metadata["recommended_for"] = "Default starting model"
            if provider_id in _OFFICIAL_PROVIDER_SOURCES and not metadata.get("sources"):
                metadata["sources"] = list(_OFFICIAL_PROVIDER_SOURCES[provider_id])
            adapter = str(
                config.get("adapter")
                or profile_meta.get("_adapter")
                or config.get("provider")
                or provider_id
            )
            rows[(provider_id, model_id)] = _build_item(
                provider_id=provider_id,
                provider_name=provider_name,
                provider_kind=provider_kind,
                model_id=model_id,
                model_metadata=metadata,
                config=config,
                adapter=adapter,
                allow_provider_defaults=True,
            )

    # Provider profiles absent from canonical defaults remain discoverable by
    # their stable profile ID. Model IDs and optional model_catalog metadata
    # are all that enter the response; endpoints and auth data never do.
    for (provider_id, model_id), metadata in profile_metadata.items():
        if (provider_id, model_id) in rows:
            # Canonical defaults own provider identity; profile metadata above
            # already contributes documented sources and exact model claims.
            continue
        provider = profile_providers.get(provider_id)
        if provider is None:
            continue
        add_provider(provider_id, provider["name"], provider["kind"])
        adapter = str(metadata.get("_adapter") or "")
        rows[(provider_id, model_id)] = _build_item(
            provider_id=provider_id,
            provider_name=provider["name"],
            provider_kind=provider["kind"],
            model_id=model_id,
            model_metadata=metadata,
            adapter=adapter,
            allow_provider_defaults=adapter == _AZURE_SPEECH_ADAPTER,
        )

    rows_list = list(rows.values())
    rows_list.sort(
        key=lambda row: (
            row["provider_name"].casefold(),
            row["family"].casefold(),
            row["label"].casefold(),
            row["id"].casefold(),
            row["id"],
        )
    )
    provider_list = sorted(
        providers.values(),
        key=lambda row: (row["name"].casefold(), row["id"].casefold(), row["id"]),
    )
    return rows_list, provider_list


def catalogue_page(
    *,
    category: str = "",
    family: str = "",
    query: str = "",
    language: str = "",
    capability: str = "",
    provider: str = "",
    commercial_use: str = "",
    recommended_only: bool = False,
    limit: int = 30,
    offset: int = 0,
) -> dict[str, Any]:
    """Return one deterministic page from the static cross-provider catalogue."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Catalogue limit must be 1–100 and offset must not be negative.")
    if commercial_use not in {"", "permitted", "noncommercial", "conditional", "unknown"}:
        raise ValueError("Unknown commercial-use filter.")

    all_rows, providers = _catalogue_rows()
    normalized_capability = {"emotions": "emotion_control"}.get(capability, capability)
    selected: list[dict[str, Any]] = []
    for row in all_rows:
        if provider and row["provider_id"] != provider:
            continue
        if category and row["category"] != category:
            continue
        if family and row["family"] != family:
            continue
        if recommended_only and not row.get("recommended_for"):
            continue
        if language:
            from .dubbing.languages import normalize_language_code

            requested = normalize_language_code(language, default="") or language.casefold()
            advertised = {str(value).casefold() for value in row["supported_languages"]}
            if not any(
                value == requested.casefold()
                or value.startswith(requested.casefold() + "-")
                for value in advertised
            ):
                continue
        permission = row.get("commercial_use", "unknown")
        if commercial_use and permission != commercial_use and not (
            commercial_use == "permitted"
            and permission == "permitted_with_attribution"
        ):
            continue
        if normalized_capability and normalized_capability not in row.get("capabilities", []):
            continue
        searchable = " ".join(
            str(row.get(key, ""))
            for key in (
                "provider_id",
                "provider_name",
                "id",
                "label",
                "family",
                "family_label",
                "description",
                "recommended_for",
                "supported_languages",
            )
        )
        if query and query.casefold() not in searchable.casefold():
            continue
        selected.append(row)

    families: dict[str, dict[str, str]] = {}
    for row in all_rows:
        families.setdefault(
            row["family"],
            {
                "id": row["family"],
                "display_name": row["family_label"],
                "category": row["category"],
            },
        )
    family_list = sorted(
        families.values(),
        key=lambda row: (row["display_name"].casefold(), row["id"].casefold(), row["id"]),
    )
    total = len(selected)
    return {
        "schema_version": 1,
        "runtime_version": audio_cpp_inventory().get("runtime_version", ""),
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None,
        "items": copy.deepcopy(selected[offset : offset + limit]),
        "families": family_list,
        "providers": copy.deepcopy(providers),
    }
