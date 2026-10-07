"""Model-specific TTS language support derived from pinned evidence and metadata."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from ..constants import (
    KOKORO_LANGUAGES,
    MAGPIE_LANGUAGES,
    QWEN_LANGUAGES,
    VOXTRAL_LANGUAGES,
    XTTS_LANGUAGES,
)
from .language_capabilities import (
    canonical_language_tag,
    registry_snapshot,
    source_language_record,
    support_record,
)

_SOURCE_RECORDS: dict[tuple[str, str], str] = {
    ("gemini", "gemini-2.5-flash-preview-tts"): "gemini25_tts_languages",
    ("gemini", "gemini-2.5-pro-preview-tts"): "gemini25_tts_languages",
    ("gemini", "gemini-3.1-flash-tts-preview"): "gemini31_tts_languages",
    ("gemini", "gemini-3.8-flash-tts"): "gemini38_tts_languages",
    ("vertex_ai", "gemini-3.8-flash-tts"): "vertex_gemini38_tts_languages",
    ("fishs2", "fishaudio/s2-pro"): "fish_s2_pro83",
    ("openai", "tts-1"): "openai_tts57",
    ("openai", "tts-1-hd"): "openai_tts57",
    ("openai", "gpt-4o-mini-tts"): "openai_tts57",
    ("vertex_ai", "gemini-3.1-flash-tts-preview"): "vertex_gemini_tts87",
    ("vertex_ai", "gemini-2.5-flash-tts"): "vertex_gemini_tts87",
    ("vertex_ai", "gemini-2.5-pro-tts"): "vertex_gemini_tts87",
    ("elevenlabs", "eleven_multilingual_v2"): "eleven_multilingual_v2_29",
    ("elevenlabs", "eleven_flash_v2_5"): "eleven_multilingual_v2_29",
    ("elevenlabs", "eleven_turbo_v2_5"): "eleven_multilingual_v2_29",
    ("silero", "v3_de"): "silero_v3_de",
    ("silero", "v3_en"): "silero_v3_en",
    ("silero", "v3_en_indic"): "silero_v3_en_indic",
    ("silero", "v3_es"): "silero_v3_es",
    ("silero", "v3_fr"): "silero_v3_fr",
    ("silero", "v3_indic"): "silero_v3_indic",
    ("silero", "v5_5_ru"): "silero_v5_5_ru",
    ("silero", "v5_cis_base"): "silero_v5_cis_base",
    ("silero", "v5_cis_base_nostress"): "silero_v5_cis_base_nostress",
    ("silero", "v5_cis_ext"): "silero_v5_cis_ext",
}
_ELEVENLABS_WIDER_MODELS = frozenset({"eleven_flash_v2_5", "eleven_turbo_v2_5"})
_ELEVENLABS_ADDITIONAL_LANGUAGES = ("hu", "no", "vi")
_LEGACY_STATIC_LANGUAGES: dict[tuple[str, str], Sequence[str]] = {
    ("xtts", "tts_models/multilingual/multi-dataset/xtts_v2"): XTTS_LANGUAGES,
    ("voxtral", "auto"): VOXTRAL_LANGUAGES,
    ("voxtral", "gguf"): VOXTRAL_LANGUAGES,
    ("voxtral", "bf16"): VOXTRAL_LANGUAGES,
    ("kokoro", "kokoro"): KOKORO_LANGUAGES,
    ("kokoro", "tts-1"): KOKORO_LANGUAGES,
    ("kokoro", "tts-1-hd"): KOKORO_LANGUAGES,
    ("kokoro", "gpt-4o-mini-tts"): KOKORO_LANGUAGES,
    ("magpie", "magpie-tts-multilingual"): MAGPIE_LANGUAGES,
    ("kobold_qwen", "prebuilt voices"): QWEN_LANGUAGES,
    ("kobold_qwen", "voice cloning"): QWEN_LANGUAGES,
}
_ROUTES_BY_ID = {
    "fishs2": "fishs2_tts",
    "silero": "silero_tts",
    "openai": "openai_audio_speech",
    "gemini": "gemini_generate_content",
    "vertex_ai": "vertex_generate_content",
    "elevenlabs": "elevenlabs_native",
    "azure": "azure_speech",
    "xtts": "xtts_tts",
    "voxtral": "voxtral_tts",
    "kokoro": "kokoro_tts",
    "magpie": "magpie_tts",
    "kobold_qwen": "kobold_qwen_tts",
    "openai_compatible": "openai_compatible_tts",
    "generic_json": "generic_json_tts",
    "elevenlabs_native": "elevenlabs_native",
    "azure_speech": "azure_speech",
    "audio_cpp": "audio_cpp",
}
_SAFE_ROUTE_RE = re.compile(r"^[A-Za-z0-9_:/-]{1,96}$")
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_VERSIONED_PATH_RE = re.compile(r"(?:^|/)v[0-9]+(?:beta[0-9]*)?(?:/|$)", re.IGNORECASE)
_SENSITIVE_URL_RE = re.compile(
    r"(?:api[_-]?(?:key|base)|base[_-]?url|credential|endpoint|secret|token|password)",
    re.IGNORECASE,
)
_CONFIG_PATH_RE = re.compile(r"(?:^|/)(?:api[-_]?base|endpoint|credentials?|config)(?:/|$)", re.I)
_LEGACY_NOTE = "Legacy static language metadata does not establish runtime adapter parity."
_INVALID_LANGUAGE_NOTE = "Invalid language metadata entries were ignored."
_AZURE_LOCALE_NOTE = "Azure voice locales describe listed voices and may be a subset of model coverage."


def _safe_route(provider_id: str, adapter: str, native_route: str) -> str:
    if native_route:
        candidate = native_route.strip()
        if (
            _SAFE_ROUTE_RE.fullmatch(candidate)
            and "://" not in candidate
            and not candidate.startswith("//")
            and not _SENSITIVE_URL_RE.search(candidate)
        ):
            return candidate
    provider_key = provider_id.strip().casefold()
    adapter_key = adapter.strip().casefold()
    if not _SAFE_PROVIDER_RE.fullmatch(provider_key):
        provider_key = "custom"
    return _ROUTES_BY_ID.get(
        provider_key,
        _ROUTES_BY_ID.get(
            adapter_key,
            f"provider:{provider_key or 'unknown'}",
        ),
    )


def _source_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or _SENSITIVE_URL_RE.search(raw):
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if parsed.scheme.casefold() != "https" or not host or parsed.port:
            return ""
        if parsed.username or parsed.password:
            return ""
        host = host.casefold().rstrip(".")
        if (
            "." not in host
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal", ".test"))
            or host.split(".", 1)[0] == "api"
            or _VERSIONED_PATH_RE.search(parsed.path)
            or _CONFIG_PATH_RE.search(parsed.path)
        ):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return ""
        safe_parts = SplitResult("https", host, parsed.path, "", "")
        return urlunsplit(safe_parts)
    except ValueError:
        return ""


def _source_urls(metadata: Mapping[str, Any], source_key: str) -> list[str]:
    raw_sources = metadata.get("sources")
    if not isinstance(raw_sources, (list, tuple)):
        return []
    pinned_urls: set[str] = set()
    source_ids = source_language_record(source_key).get("source_ids", []) if source_key else []
    source_ids = source_ids if isinstance(source_ids, list) else []
    if source_ids:
        registry = registry_snapshot()
        for source in registry.get("sources", []):
            if isinstance(source, dict) and source.get("id") in source_ids:
                pinned_url = source.get("url")
                if isinstance(pinned_url, str):
                    pinned_urls.add(pinned_url)
    result: list[str] = []
    for item in raw_sources:
        safe = _source_url(item)
        if safe and safe not in pinned_urls and safe not in result:
            result.append(safe)
    return result


def _metadata_language_values(metadata: Mapping[str, Any]) -> tuple[list[object], bool]:
    for field in ("supported_languages", "languages"):
        value = metadata.get(field)
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and value
        ):
            return list(value), True
    return [], False


def _language_tag(value: object) -> str | None:
    if isinstance(value, str):
        candidate: object = value
    elif isinstance(value, Mapping):
        candidate = next(
            (
                value[key]
                for key in (
                    "language_id",
                    "tag",
                    "language_code",
                    "code",
                    "locale",
                    "name",
                )
                if isinstance(value.get(key), str)
            ),
            None,
        )
    else:
        candidate = None
    try:
        normalized = canonical_language_tag(candidate)
    except ValueError:
        return None
    return normalized or None


def _clean_languages(values: Sequence[object]) -> tuple[list[str], bool]:
    languages: set[str] = set()
    dropped = False
    for value in values:
        tag = _language_tag(value)
        if tag is None:
            dropped = True
        else:
            languages.add(tag)
    return sorted(languages), dropped


def _azure_voice_locales(metadata: Mapping[str, Any]) -> list[str]:
    raw_voices = metadata.get("voice_metadata")
    if not isinstance(raw_voices, Mapping):
        return []
    locales = {
        tag
        for value in raw_voices.values()
        if isinstance(value, Mapping)
        for locale in [value.get("locale")]
        if (tag := _language_tag(locale)) is not None
    }
    return sorted(locales)


def _model_revision(model_id: str, metadata: Mapping[str, Any]) -> str:
    revision = metadata.get("model_revision")
    if isinstance(revision, str) and revision.strip():
        return revision.strip()
    manifest = metadata.get("weight_manifest")
    if isinstance(manifest, Mapping):
        revision = manifest.get("revision")
        if isinstance(revision, str) and revision.strip():
            return revision.strip()
    return model_id


def _base_record(
    *,
    provider_id: str,
    model_id: str,
    adapter: str,
    metadata: Mapping[str, Any],
    operation: str,
    native_route: str,
    source_key: str = "",
    languages: Sequence[str] | None = None,
    coverage: str = "unknown",
    discovery: str = "static",
    note: str = "",
) -> dict[str, Any]:
    aliases = metadata.get("request_aliases")
    if not isinstance(aliases, Mapping):
        aliases = None
    runtime_requirement = metadata.get("runtime_requirement")
    if (
        not isinstance(runtime_requirement, str)
        or "\u0000" in runtime_requirement
        or "://" in runtime_requirement
        or _SENSITIVE_URL_RE.search(runtime_requirement)
    ):
        runtime_requirement = ""
    return support_record(
        provider_id=provider_id,
        model_id=model_id,
        model_revision=_model_revision(model_id, metadata),
        operation=operation,
        native_route=_safe_route(provider_id, adapter, native_route),
        source_key=source_key,
        languages=languages,
        coverage=coverage,
        request_aliases=aliases,
        source_urls=_source_urls(metadata, source_key),
        runtime_requirement=runtime_requirement,
        discovery=discovery,
        note=note,
    )


def tts_language_support(
    provider_id: str,
    model_id: str,
    *,
    adapter: str = "",
    metadata: Mapping[str, Any] | None = None,
    operation: str = "tts",
    native_route: str = "",
    discovery: str = "static",
) -> dict[str, Any]:
    """Build model-specific TTS language support without probing a provider."""

    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id must be a non-empty string")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")
    provider_key = provider_id.strip().casefold()
    model_key = model_id.strip().casefold()
    if (
        native_route in {"", "gemini", "gemini_generate_content"}
        and provider_key == "gemini"
        and model_key == "gemini-3.8-flash-tts"
    ):
        native_route = "gemini_interactions"
    model_metadata = metadata if isinstance(metadata, Mapping) else {}
    selected_discovery = "provider_live" if discovery == "provider_live" else "static"

    metadata_languages, has_metadata_languages = _metadata_language_values(model_metadata)
    cleaned_metadata_languages, dropped_invalid = _clean_languages(metadata_languages)
    language_metadata_note = _INVALID_LANGUAGE_NOTE if dropped_invalid else ""

    # A caller explicitly identified this list as provider-live. Treat it as
    # exhaustive only when the same metadata explicitly labels it exact.
    if selected_discovery == "provider_live" and has_metadata_languages:
        if cleaned_metadata_languages:
            coverage = (
                "exact"
                if model_metadata.get("language_coverage") == "exact" and not dropped_invalid
                else "claim"
            )
        else:
            coverage = "unknown"
        return _base_record(
            provider_id=provider_id,
            model_id=model_id,
            adapter=adapter,
            metadata=model_metadata,
            operation=operation,
            native_route=native_route,
            languages=cleaned_metadata_languages,
            coverage=coverage,
            discovery=selected_discovery,
            note=language_metadata_note,
        )

    source_key = _SOURCE_RECORDS.get((provider_key, model_key), "")
    if source_key:
        source_languages = source_language_record(source_key).get("languages", [])
        if not isinstance(source_languages, list):
            source_languages = []
        if (provider_key, model_key) in {
            ("elevenlabs", model) for model in _ELEVENLABS_WIDER_MODELS
        }:
            source_languages = [*source_languages, *_ELEVENLABS_ADDITIONAL_LANGUAGES]
        return _base_record(
            provider_id=provider_id,
            model_id=model_id,
            adapter=adapter,
            metadata=model_metadata,
            operation=operation,
            native_route=native_route,
            source_key=source_key,
            languages=source_languages,
            coverage=str(source_language_record(source_key).get("coverage", "unknown")),
            discovery="static",
            note="Static upstream language evidence does not establish runtime adapter parity.",
        )

    if provider_key == "azure" and not has_metadata_languages:
        locales = _azure_voice_locales(model_metadata)
        if locales:
            return _base_record(
                provider_id=provider_id,
                model_id=model_id,
                adapter=adapter,
                metadata=model_metadata,
                operation=operation,
                native_route=native_route,
                languages=locales,
                coverage="subset",
                discovery=selected_discovery,
                note=_AZURE_LOCALE_NOTE,
            )

    if (provider_key, model_key) in _LEGACY_STATIC_LANGUAGES:
        return _base_record(
            provider_id=provider_id,
            model_id=model_id,
            adapter=adapter,
            metadata=model_metadata,
            operation=operation,
            native_route=native_route,
            languages=_LEGACY_STATIC_LANGUAGES[(provider_key, model_key)],
            coverage="claim",
            discovery="static",
            note=_LEGACY_NOTE,
        )

    if has_metadata_languages:
        coverage = "claim" if cleaned_metadata_languages else "unknown"
        return _base_record(
            provider_id=provider_id,
            model_id=model_id,
            adapter=adapter,
            metadata=model_metadata,
            operation=operation,
            native_route=native_route,
            languages=cleaned_metadata_languages,
            coverage=coverage,
            discovery=selected_discovery,
            note=language_metadata_note,
        )

    return _base_record(
        provider_id=provider_id,
        model_id=model_id,
        adapter=adapter,
        metadata=model_metadata,
        operation=operation,
        native_route=native_route,
        coverage="unknown",
        discovery=selected_discovery,
    )
