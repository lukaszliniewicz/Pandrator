"""TTS catalogue projection and model-language metadata without HTTP or persistence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from pandrator.logic.tts_language_support import tts_language_support
from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_CATALOG


def normalize_service_id(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "qwen3_tts": "kobold_qwen",
        "qwen3": "kobold_qwen",
        "qwen": "kobold_qwen",
        "kobold_qwen3": "kobold_qwen",
        "audio.cpp": "audio_cpp",
        "audio-cpp": "audio_cpp",
        "audiocpp": "audio_cpp",
        "openai_compatible": "openai_compatible",
    }.get(normalized, normalized)


def _dedupe_catalogue_values(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


class TtsCatalogueServiceNotFoundError(ValueError):
    """Raised when a TTS catalogue filter names an unknown service."""

    def __init__(self, service_ids: Iterable[object]):
        missing = [str(item) for item in service_ids]
        super().__init__(
            "Unknown TTS service(s): "
            + ", ".join(missing)
            + ". Use the full catalogue to list available services."
        )
        self.service_ids = missing


# Slim per-service projection for the opt-in compact catalogue view.
# Only dropdown/status/capability fields the session view needs; heavy
# detail (model_catalog, voice_metadata, expressive capabilities, request
# schemas, pricing, secret references) stays on the full/detail views.
COMPACT_TTS_SERVICE_FIELDS = frozenset({
    "id",
    "name",
    "description",
    "adapter",
    "kind",
    "provider",
    "source_url",
    "api_base",
    "connection_mode",
    "manager_available",
    "manager_component_id",
    "manager_component_state",
    "manager_supported_actions",
    "manager_endpoint_read_only",
    "managed_service_id",
    "manager_service",
    "online",
    "available",
    "availability_reason",
    "models",
    "default_model",
    "voices",
    "live_voices",
    "voice_catalogues",
    "model_catalog",
    "model_voice_modes",
    "voice_metadata",
    "default_voice",
    "default_voices",
    "default_voices_by_language",
    "generation_prompt_models",
    "supports_voice_cloning",
    "supports_voice_deletion",
    "supports_dynamic_catalog",
    "supports_model_upload",
    "supports_prebuilt_voices",
    "supports_batch_synthesis",
    "supports_parallel_synthesis",
    "batch_synthesis",
    "voice_reference_text",
    "credential_required",
    "credential_configured",
    "credential_source",
    "credential_backend",
    "credential_reference",
    "catalogue_role",
    "replacement_service_id",
    "replacement_model_family",
})

TTS_CATALOGUE_VIEWS = ("full", "compact")

MAX_TTS_SERVICE_FILTER_IDS = 20


def _project_compact_service(service: Mapping[str, Any]) -> dict[str, Any]:
    """Project one service row onto the compact allowlist without defaults."""
    return {key: service[key] for key in COMPACT_TTS_SERVICE_FIELDS if key in service}


class TtsCatalogueModelNotFoundError(ValueError):
    """Raised when a TTS service-detail filter names an unknown model."""

    def __init__(self, service_id: str, model_ids: Iterable[object]):
        missing = [str(item) for item in model_ids]
        super().__init__(
            f"Unknown model(s) for TTS service '{service_id}': "
            + ", ".join(missing)
            + "."
        )
        self.service_id = service_id
        self.model_ids = missing


# Lightweight per-model fields for compact chooser rows. The language support
# object is explicitly projected below; other nested detail remains excluded.
SLIM_MODEL_CATALOG_FIELDS = (
    "id",
    "label",
    "family",
    "voice_mode",
    "supported_languages",
    "language_support",
)

SLIM_LANGUAGE_SUPPORT_FIELDS = (
    "schema_version",
    "catalogue_revision",
    "provider_id",
    "service_id",
    "model_id",
    "model_revision",
    "operation",
    "native_route",
    "coverage",
    "languages",
    "request_aliases",
    "runtime_requirement",
    "discovery",
    "note",
    "source_key",
    "source_revision",
    "source_ids",
)
_SENSITIVE_SUPPORT_TEXT_MARKERS = (
    "api_key",
    "api key",
    "base_url",
    "credential",
    "endpoint",
    "password",
    "secret",
    "token",
    "http://",
    "https://",
)

_NATIVE_LANGUAGE_PROVIDER_BY_ADAPTER = {
    "audio_cpp": "audio_cpp",
    "silero": "silero",
    "elevenlabs_native": "elevenlabs",
    "azure_speech": "azure",
}
_LIVE_LANGUAGE_NATIVE_ROUTES = {
    "silero": "silero_audio_speech",
    "elevenlabs": "elevenlabs_text_to_speech",
}
_VERIFIED_LIVE_LANGUAGE_ROW = "_pandrator_verified_live_language_catalog"

MAX_TTS_DETAIL_MODEL_IDS = 20


def _slim_model_catalog(service: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build chooser-grade model entries covering every selectable model id.

    Same precedence as the full builder (configured/discovered record wins
    over static builtins), restricted to chooser scalars, language lists, and
    the portable language-support projection: service model rows overlay the audio.cpp
    builtin index, so custom labels, language options and voice modes can
    never silently differ from the full view. Non-audio.cpp and custom
    providers keep whatever scalar metadata their own records carry. The
    service's model_voice_modes map only fills a voice_mode no other source
    provides. Full records (catalogue_info, request_parameters, per-model
    detail) stay on the full/detail views and are hydrated per chosen model.
    """
    records: dict[str, Mapping[str, Any]] = {}
    raw_catalog = service.get("model_catalog")
    if isinstance(raw_catalog, list):
        for item in raw_catalog:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id and model_id not in records:
                records[model_id] = item
    models = service.get("models")
    wanted = _dedupe_catalogue_values(
        [
            *(models if isinstance(models, list) else []),
            str(service.get("default_model") or ""),
            *records,
        ]
    )
    raw_voice_modes = service.get("model_voice_modes")
    voice_modes = raw_voice_modes if isinstance(raw_voice_modes, dict) else {}
    builtin_light: dict[str, dict[str, Any]] = {}
    if normalize_service_id(service.get("adapter")) == "audio_cpp":
        for item in AUDIO_CPP_MODEL_CATALOG:
            model_id = str(item.get("id") or "").strip()
            if not model_id or model_id in builtin_light:
                continue
            builtin_light[model_id] = _slim_model_fields(item)
    summaries: list[dict[str, Any]] = []
    for model_id in wanted:
        summary = dict(builtin_light.get(model_id, {"id": model_id}))
        record = records.get(model_id)
        if record is not None:
            summary.update(_slim_model_fields(record))
        summary["id"] = model_id
        mode = voice_modes.get(model_id)
        if isinstance(mode, str) and mode.strip() and "voice_mode" not in summary:
            summary["voice_mode"] = mode.strip()
        summaries.append(summary)
    return summaries


def _slim_model_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """Pick the retained scalar/language fields from one model record."""
    fields: dict[str, Any] = {}
    for key in SLIM_MODEL_CATALOG_FIELDS:
        if key in {"id", "supported_languages", "languages"}:
            continue
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    for key in ("supported_languages",):
        value = item.get(key)
        if isinstance(value, list):
            fields[key] = [
                entry
                for entry in value
                if isinstance(entry, str) and entry.strip()
            ]
    language_support = item.get("language_support")
    if isinstance(language_support, Mapping):
        slim_support: dict[str, Any] = {}
        for key in SLIM_LANGUAGE_SUPPORT_FIELDS:
            if key in {"languages", "request_aliases", "source_ids"}:
                continue
            value = language_support.get(key)
            if value is None and key == "source_revision":
                slim_support[key] = None
            elif key == "schema_version" and isinstance(value, int) and not isinstance(
                value, bool
            ):
                slim_support[key] = value
            elif isinstance(value, str) and _safe_support_text(value):
                slim_support[key] = value.strip()
        languages = language_support.get("languages")
        if isinstance(languages, list):
            slim_support["languages"] = [
                value for value in languages if _safe_support_text(value)
            ]
        aliases = language_support.get("request_aliases")
        if isinstance(aliases, Mapping):
            slim_support["request_aliases"] = {
                key: value
                for key, value in aliases.items()
                if isinstance(key, str)
                and key.strip()
                and isinstance(value, str)
                and value.strip()
                and _safe_support_text(key)
                and _safe_support_text(value)
            }
        source_ids = language_support.get("source_ids")
        if isinstance(source_ids, list):
            slim_support["source_ids"] = [
                value for value in source_ids if _safe_support_text(value)
            ]
        fields["language_support"] = slim_support
        fields["supported_languages"] = list(slim_support.get("languages", []))
    model_id = str(item.get("id") or "").strip()
    fields["id"] = model_id
    return fields


def _safe_support_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(
            marker in value.casefold() for marker in _SENSITIVE_SUPPORT_TEXT_MARKERS
        )
    )


def _language_provider_id(service: Mapping[str, Any]) -> tuple[str, str]:
    """Return the model-evidence provider id and canonical service id.

    Provider labels on arbitrary compatible profiles are descriptive, not
    proof that their models use the named provider's native API. Only a
    documented native adapter may bind a distinct provider id.
    """
    service_id = normalize_service_id(service.get("id") or service.get("name"))
    adapter_id = normalize_service_id(service.get("adapter"))
    provider_id = _NATIVE_LANGUAGE_PROVIDER_BY_ADAPTER.get(adapter_id)
    declared_provider = normalize_service_id(service.get("provider"))
    if provider_id and declared_provider == provider_id:
        return provider_id, service_id
    return service_id, service_id


def _model_language_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep model evidence while excluding service/voice data from inference."""
    return {
        key: value
        for key, value in item.items()
        if key not in {"voice_metadata", "voice_catalogues", "voices"}
    }


def _decorate_model_language_support(service: dict[str, Any]) -> None:
    """Attach model-specific language support to every selectable model row."""
    provider_id, service_id = _language_provider_id(service)
    adapter_id = normalize_service_id(service.get("adapter"))
    audio_cpp_support = {
        str(item.get("id") or "").strip(): item.get("language_support")
        for item in AUDIO_CPP_MODEL_CATALOG
        if str(item.get("id") or "").strip()
    } if adapter_id == "audio_cpp" else {}
    catalog = service.get("model_catalog")
    rows = catalog if isinstance(catalog, list) else []
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if model_id:
            rows_by_id.setdefault(model_id, []).append(item)

    model_values = service.get("models")
    model_ids = _dedupe_catalogue_values(
        [
            *(model_values if isinstance(model_values, list) else []),
            service.get("default_model"),
            *rows_by_id,
        ]
    )
    for model_id in model_ids:
        model_rows = rows_by_id.get(model_id)
        if not model_rows:
            item: dict[str, Any] = {"id": model_id}
            rows.append(item)
            model_rows = [item]
            rows_by_id[model_id] = model_rows
        for item in model_rows:
            verified_live_row = item.pop(_VERIFIED_LIVE_LANGUAGE_ROW, False) is True
            existing_support = item.get("language_support")
            if not isinstance(existing_support, Mapping) and adapter_id == "audio_cpp":
                existing_support = audio_cpp_support.get(model_id)
            if (
                isinstance(existing_support, Mapping)
                and existing_support.get("provider_id") == provider_id
                and existing_support.get("model_id") == model_id
            ):
                support = dict(existing_support)
                languages = support.get("languages")
                supported_languages = (
                    [value for value in languages if isinstance(value, str)]
                    if isinstance(languages, list)
                    else []
                )
            else:
                metadata = _model_language_metadata(item)
                # Raw configured catalogue metadata cannot assert provider-live
                # authority. Only rows marked by the successful native fetch
                # loops below can take the live path.
                for key in ("discovery", "language_coverage", "native_route"):
                    metadata.pop(key, None)
                discovery = "provider_live" if verified_live_row else "static"
                native_route = (
                    _LIVE_LANGUAGE_NATIVE_ROUTES.get(provider_id, "")
                    if verified_live_row
                    else ""
                )
                if verified_live_row:
                    metadata["language_coverage"] = "exact"
                result = tts_language_support(
                    provider_id,
                    model_id,
                    adapter=adapter_id,
                    metadata=metadata,
                    operation="tts",
                    native_route=native_route,
                    discovery=discovery,
                )
                support = dict(result)
                languages = support.get("languages")
                supported_languages = (
                    [value for value in languages if isinstance(value, str)]
                    if isinstance(languages, list)
                    else []
                )
            if provider_id != service_id:
                support["service_id"] = service_id
            item["language_support"] = support
            item["supported_languages"] = supported_languages

    if catalog is not None or model_ids:
        service["model_catalog"] = rows


def _mark_live_language_catalog(
    model_catalog: list[dict[str, Any]], *, provider_id: str, native_route: str
) -> list[dict[str, Any]]:
    """Mark model rows whose own live catalogue contains authoritative languages."""
    for item in model_catalog:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        raw_languages = item.get("supported_languages")
        if not isinstance(raw_languages, list) or not raw_languages:
            raw_languages = item.get("languages")
        if isinstance(raw_languages, list) and raw_languages:
            evidence = tts_language_support(
                provider_id,
                model_id,
                metadata={**item, "language_coverage": "exact"},
                operation="tts",
                native_route=native_route,
                discovery="provider_live",
            )
            if evidence["coverage"] == "exact" and evidence["languages"]:
                item.update(
                    {
                        "language_coverage": "exact",
                        "discovery": "provider_live",
                        "native_route": native_route,
                        _VERIFIED_LIVE_LANGUAGE_ROW: True,
                    }
                )
    return model_catalog


def _classify_voice_metadata_models(
    service: Mapping[str, Any],
    declared_models: set[str],
) -> dict[Any, set[str] | None]:
    """Return eligible model sets for raw keys; None marks shared global voices."""
    metadata = service.get("voice_metadata")
    if not isinstance(metadata, dict):
        return {}
    voice_lists = [service.get("voices")]
    catalogues = service.get("voice_catalogues")
    if isinstance(catalogues, dict):
        voice_lists.extend(catalogues.values())
    known_voices = {
        voice
        for voices in voice_lists
        if isinstance(voices, list)
        for voice in voices
        if isinstance(voice, str) and voice
    }
    associations: dict[Any, set[str] | None] = {}
    identity_models: set[str] = set()
    # Keys are unescaped: match the exact voice suffix rather than a colon position.
    # Row model fields are not authoritative for existing keyed metadata lookups.
    for key, item in metadata.items():
        if not isinstance(item, dict):
            continue
        voice_id = item.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id:
            voice_id = item.get("id")
        if not isinstance(voice_id, str) or not voice_id:
            continue
        identities = [voice_id]
        stripped_id = voice_id.strip()
        if stripped_id and stripped_id != voice_id:
            identities.append(stripped_id)
        raw_key = str(key)
        for identity in identities:
            if raw_key == identity:
                associations[key] = None
                break
            suffix = f":{identity}"
            if raw_key.endswith(suffix):
                model_id = raw_key[:-len(suffix)]
                if model_id:
                    associations[key] = {model_id}
                    identity_models.add(model_id)
                    break
    # Freeze identity-backed models across all rows before classifying sparse rows,
    # so their associations do not depend on metadata row order.
    known_models = frozenset(declared_models | identity_models)
    for key in metadata:
        if key in associations:
            continue
        raw_key = str(key)
        if raw_key in known_voices:
            associations[key] = None
            continue
        # Sparse raw keys can collide across model prefixes, so retain every match.
        # Use the legacy split only when stronger identity/model evidence is absent.
        models = {
            model_id
            for model_id in known_models
            if raw_key.startswith(f"{model_id}:")
            and len(raw_key) > len(model_id) + 1
        }
        if not models:
            prefix, _, suffix = raw_key.partition(":")
            if prefix and suffix:
                models.add(prefix)
        associations[key] = models
    return associations


def _filter_service_models(
    service: dict[str, Any],
    selected: Sequence[str],
    *,
    service_id: str,
) -> dict[str, Any]:
    """Restrict per-model detail maps to the selected models.

    Id lists (``models``/``voices``) and service defaults stay whole as small
    chooser context; only the heavy per-model maps are filtered. Matching is
    exact and case-sensitive: model ids are provider identifiers, not slugs.
    """
    wanted = set(selected)
    catalog = service.get("model_catalog")
    known: set[str] = set()
    if isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, dict):
                model_id = str(item.get("id") or "")
                if model_id:
                    known.add(model_id)
    for key in ("models", "voice_catalogues", "model_voice_modes", "default_voices"):
        value = service.get(key)
        if isinstance(value, dict):
            known.update(str(model_id) for model_id in value)
        elif isinstance(value, list):
            known.update(str(model_id) for model_id in value if str(model_id).strip())
    default_model = str(service.get("default_model") or "")
    if default_model:
        known.add(default_model)
    metadata_models = _classify_voice_metadata_models(service, known)
    for associated_models in metadata_models.values():
        if associated_models is not None:
            known.update(associated_models)
    missing = [model_id for model_id in selected if model_id not in known]
    if missing:
        raise TtsCatalogueModelNotFoundError(service_id, missing)
    if isinstance(catalog, list):
        service["model_catalog"] = [
            item
            for item in catalog
            if isinstance(item, dict) and str(item.get("id") or "") in wanted
        ]
    voice_catalogues = service.get("voice_catalogues")
    if isinstance(voice_catalogues, dict):
        service["voice_catalogues"] = {
            model_id: voices
            for model_id, voices in voice_catalogues.items()
            if str(model_id) in wanted
        }
    voice_metadata = service.get("voice_metadata")
    if isinstance(voice_metadata, dict):
        service["voice_metadata"] = {
            key: item
            for key, item in voice_metadata.items()
            if (associated_models := metadata_models[key]) is None
            or associated_models & wanted
        }
    model_voice_modes = service.get("model_voice_modes")
    if isinstance(model_voice_modes, dict):
        service["model_voice_modes"] = {
            model_id: mode
            for model_id, mode in model_voice_modes.items()
            if str(model_id) in wanted
        }
    default_voices = service.get("default_voices")
    if isinstance(default_voices, dict):
        service["default_voices"] = {
            model_id: voice
            for model_id, voice in default_voices.items()
            if str(model_id) in wanted
        }
    generation_prompt_models = service.get("generation_prompt_models")
    if isinstance(generation_prompt_models, list):
        service["generation_prompt_models"] = [
            model_id
            for model_id in generation_prompt_models
            if str(model_id) in wanted
        ]
    return service


def _audio_cpp_static_model_catalog(service: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge current built-in metadata into configured audio.cpp model rows."""

    builtins = {
        str(item.get("id") or "").strip(): dict(item)
        for item in AUDIO_CPP_MODEL_CATALOG
        if str(item.get("id") or "").strip()
    }
    raw_catalog = service.get("model_catalog")
    configured = (
        [dict(item) for item in raw_catalog if isinstance(item, dict)]
        if isinstance(raw_catalog, list)
        else []
    )
    if not configured:
        configured = [
            {"id": model_id}
            for model_id in _dedupe_catalogue_values(service.get("models") or [])
        ]
    result: list[dict[str, Any]] = []
    for item in configured:
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        result.append({**builtins.get(model_id, {}), **item, "id": model_id})
    return result


def _supports_parallel_cloud_synthesis(service: Mapping[str, Any]) -> bool:
    """Keep local compatible servers serial; enable known cloud transports."""
    adapter = normalize_service_id(service.get("adapter"))
    service_id = normalize_service_id(service.get("id") or service.get("name"))
    cloud_ids = {"openai", "gemini", "vertex_ai", "elevenlabs"}
    if adapter in {"azure_speech", "elevenlabs_native"}:
        return True
    if adapter and adapter != "openai_compatible":
        return False
    if service_id in cloud_ids:
        return True
    if adapter == "openai_compatible":
        host = (
            urlparse(
                str(service.get("api_base") or service.get("base_url") or "")
            ).hostname
            or ""
        ).lower()
        return host == "api.openai.com" or any(
            host.endswith(suffix)
            for suffix in (
                ".openai.azure.com",
                ".cognitiveservices.azure.com",
                ".services.ai.azure.com",
                ".inference.ai.azure.com",
                ".models.ai.azure.com",
            )
        )
    return False


