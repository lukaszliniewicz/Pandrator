"""Explicit public speech catalogue projections without persistence or credentials."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pandrator.logic.speech_capabilities import decorate_service_capabilities
from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_CATALOG

from .tts_catalogue_projection import (
    COMPACT_TTS_SERVICE_FIELDS,
    _filter_service_models,
    _slim_model_catalog,
)

_EXCLUDED = frozenset(
    {
        "source_url",
        "api_base",
        "manager_supported_actions",
        "manager_endpoint_read_only",
        "manager_service",
        "availability_reason",
        "voice_catalogue_warning",
        "voice_reference_text",
        "managed_google_voice_ids",
        "supports_model_upload",
    }
)
_VOICE_FIELDS = frozenset(
    {
        "id",
        "voice_id",
        "name",
        "display_name",
        "language",
        "language_code",
        "languages",
        "gender",
        "accent",
        "style",
        "styles",
        "model",
        "models",
        "type",
    }
)
_STT_FIELDS = frozenset(
    {
        "id",
        "name",
        "adapter",
        "model",
        "models",
        "default_model",
        "language",
        "languages",
        "supported_languages",
        "language_support",
        "supports_diarization",
        "diarization",
        "word_timestamps",
        "word_timing",
        "synchronous",
    }
)
_LANGUAGE_FIELDS = frozenset(
    {
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
    }
)


def _language(value):
    if not isinstance(value, dict):
        return (
            deepcopy(value)
            if isinstance(value, str)
            or isinstance(value, list)
            and all(isinstance(v, str) for v in value)
            else None
        )
    return {
        key: deepcopy(item)
        for key, item in value.items()
        if key in _LANGUAGE_FIELDS
        and (
            isinstance(item, (str, int, bool))
            or (isinstance(item, list) and all(isinstance(v, str) for v in item))
            or (
                key == "request_aliases"
                and isinstance(item, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in item.items())
            )
        )
    }


def _voice_record(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: deepcopy(item)
        for key, item in value.items()
        if key in _VOICE_FIELDS
        and (
            isinstance(item, (str, int, bool))
            or isinstance(item, list)
            and all(isinstance(v, str) for v in item)
        )
    }


def approved_tts_service(source: dict[str, Any], *, selected_models=None):
    row = {}
    identity_maps = {
        "voice_catalogues",
        "model_voice_modes",
        "default_voices",
        "default_voices_by_language",
    }
    for key, value in source.items():
        if (
            key not in COMPACT_TTS_SERVICE_FIELDS
            or key in _EXCLUDED
            or key.startswith("credential_")
        ):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            row[key] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            row[key] = list(value)
        elif key in identity_maps and isinstance(value, dict):
            row[key] = {
                name: deepcopy(item)
                for name, item in value.items()
                if isinstance(item, str)
                or isinstance(item, list)
                and all(isinstance(v, str) for v in item)
            }
    row["model_catalog"] = _slim_model_catalog(source)
    for model in row["model_catalog"]:
        if "language_support" in model:
            model["language_support"] = _language(model["language_support"])
    metadata = source.get("voice_metadata")
    models = set(str(v) for v in source.get("models", []) if isinstance(v, str))
    if isinstance(metadata, dict):
        safe = {}
        for key, value in metadata.items():
            record = _voice_record(value)
            if record:
                safe[key] = record
            elif key in models and isinstance(value, dict):
                nested = {
                    voice: projected
                    for voice, item in value.items()
                    if (projected := _voice_record(item))
                }
                if nested:
                    safe[key] = nested
        row["voice_metadata"] = safe
    if source.get("connection_mode") != "managed_local":
        row.update(online=None, available=None, health_checked_at=None)
    row["catalogue"] = {
        "source": "configured",
        "scope": "service",
        "service_id": source.get("id"),
        "checked_at": None,
        "complete": False,
        "error": None,
    }
    if selected_models is not None:
        row = _filter_service_models(row, selected_models, service_id=str(source.get("id") or ""))
        if source.get("adapter") == "audio_cpp":
            canonical = {item["id"]: item for item in AUDIO_CPP_MODEL_CATALOG}
            selected = []
            for summary in row["model_catalog"]:
                item = deepcopy(canonical.get(summary["id"], summary))
                item.update(
                    {
                        k: v
                        for k, v in summary.items()
                        if k
                        in {
                            "id",
                            "label",
                            "family",
                            "voice_mode",
                            "supported_languages",
                            "language_support",
                        }
                    }
                )
                configured = next(
                    (
                        m
                        for m in source.get("model_catalog", [])
                        if isinstance(m, dict) and m.get("id") == summary["id"]
                    ),
                    {},
                )
                limit = configured.get("recommended_chunk_characters")
                if type(limit) is int and limit > 0:
                    item["recommended_chunk_characters"] = limit
                selected.append(item)
            row["model_catalog"] = selected
            row.pop("generation_prompt_models", None)
            canonical_models = [model for model in selected_models if model in canonical]
            if canonical_models:
                context = {
                    "id": "audio_cpp",
                    "adapter": "audio_cpp",
                    "models": canonical_models,
                    "model_catalog": [deepcopy(canonical[model]) for model in canonical_models],
                }
                decorate_service_capabilities(context)
                row["expressive_capabilities"] = context["expressive_capabilities"]
                row["generation_prompt_models"] = context["generation_prompt_models"]
    return row


def approved_tts_payload(payload: dict[str, Any], *, selected_models=None):
    result = {
        key: deepcopy(payload[key])
        for key in (
            "view",
            "revision",
            "default_service",
            "recommended_service",
            "default_revision",
            "selected_models",
        )
        if key in payload
    }
    defaults = payload.get("default_value") or {}
    result.update(
        value={},
        default_value={
            key: defaults[key]
            for key in ("service", "model", "voice")
            if key in defaults and isinstance(defaults[key], str)
        },
    )
    if "services" in payload:
        result["services"] = [approved_tts_service(row) for row in payload["services"]]
    if "service" in payload:
        result["service"] = approved_tts_service(
            payload["service"], selected_models=selected_models
        )
    return result


def summary_tts_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bound an already-approved collection to safe chooser/status metadata."""
    result = {
        key: deepcopy(payload[key])
        for key in ("revision", "default_service", "default_revision", "recommended_service")
        if key in payload
    }
    result["view"] = "summary"
    result["services"] = []
    excluded = _EXCLUDED | {
        "model_catalog",
        "catalogue",
        "voices",
        "live_voices",
        "voice_metadata",
        "voice_catalogues",
        "default_voices",
        "default_voices_by_language",
        "model_voice_modes",
        "language_support",
    }
    allowed = (COMPACT_TTS_SERVICE_FIELDS | {"languages"}) - excluded
    for source in payload.get("services", []):
        row: dict[str, Any] = {
            key: deepcopy(value)
            for key, value in source.items()
            if key in allowed
            and not key.startswith("credential_")
            and (
                value is None
                or isinstance(value, (str, int, float, bool))
                or isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
        }
        voices = list(
            dict.fromkeys(
                voice
                for key in ("voices", "live_voices")
                for voice in (source.get(key) if isinstance(source.get(key), list) else [])
                if isinstance(voice, str)
            )
        )
        row["voices"] = voices[:20]
        row["voice_count"] = len(voices)
        row["model_catalog"] = [
            {
                key: deepcopy(value)
                for key, value in model.items()
                if key in {"id", "label", "family", "voice_mode", "loaded"}
                and (value is None or isinstance(value, (str, int, float, bool)))
            }
            for model in source.get("model_catalog", [])
            if isinstance(model, dict)
        ]
        catalogue = source.get("catalogue")
        if isinstance(catalogue, dict):
            row["catalogue"] = {
                key: deepcopy(value)
                for key, value in catalogue.items()
                if key in {"source", "scope", "service_id", "checked_at", "complete"}
                and (value is None or isinstance(value, (str, int, float, bool)))
            }
        result["services"].append(row)
    return result


def public_configuration(payload: dict[str, Any]):
    result = deepcopy(payload)
    if "manager" in result:
        manager = result["manager"] or {}
        result["manager"] = {key: bool(manager.get(key)) for key in ("configured", "available")}
    for row in result.get("services", []) if "services" in result else [result.get("service", {})]:
        for key in ("manager_service", "manager_supported_actions", "manager_endpoint_read_only"):
            row.pop(key, None)
    return result


def approved_stt_records(records):
    result = []
    for record in records:
        if not isinstance(record, dict) or record.get("enabled") is False:
            continue
        row: dict[str, Any] = {
            key: deepcopy(value)
            for key, value in record.items()
            if key in _STT_FIELDS
            and (
                isinstance(value, (str, bool))
                or isinstance(value, list)
                and all(isinstance(v, str) for v in value)
            )
        }
        if "language_support" in record:
            row["language_support"] = _language(record["language_support"])
        result.append(row)
    return result
