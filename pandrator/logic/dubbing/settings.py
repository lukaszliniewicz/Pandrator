"""Normalization and legacy migration for Pandrator dubbing settings."""

from __future__ import annotations

import re
from typing import Any


TRANSLATION_BACKEND_LLM = "llm"
TRANSLATION_BACKEND_DEEPL = "deepl"

LEGACY_DUBBING_MODEL_ALIASES: dict[str, str] = {
    "gpt 5.4": "openai/gpt-5.4",
    "gpt 5.4-mini": "openai/gpt-5.4-mini",
    "gemini 3.1 pro": "gemini/gemini-3.1-pro-preview",
    "gemini 3.0 flash": "gemini/gemini-3-flash-preview",
    "opus 4.7": "anthropic/claude-opus-4-7",
    "sonnet 4.6": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-3-5-haiku-20241022",
    "sonnet": "anthropic/claude-3-5-sonnet-20241022",
    "sonnet thinking": "anthropic/claude-3-5-sonnet-20241022",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "gemini-flash": "gemini/gemini-2.0-flash",
    "gemini-pro": "gemini/gemini-1.5-pro",
    "deepseek-r1": "openrouter/deepseek/deepseek-r1",
    "qwq-32b": "openrouter/qwen/qwq-32b",
}


def normalize_provider_id(raw_value: str | None) -> str:
    lowered = str(raw_value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def normalize_translation_backend(raw_value: str | None) -> str:
    normalized = str(raw_value or "").strip().lower()
    return TRANSLATION_BACKEND_DEEPL if normalized == TRANSLATION_BACKEND_DEEPL else TRANSLATION_BACKEND_LLM


def _provider_by_id(
    provider_id: str,
    provider_configs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_id = normalize_provider_id(provider_id)
    for provider in provider_configs:
        if normalize_provider_id(provider.get("id")) == normalized_id:
            return provider
    return None


def _provider_for_bare_model(
    model_name: str,
    provider_configs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for provider in provider_configs:
        models = provider.get("models", [])
        if isinstance(models, list) and model_name in {str(model).strip() for model in models}:
            matches.append(provider)
    return matches[0] if len(matches) == 1 else None


def normalize_native_llm_model(
    model_name: str | None,
    *,
    provider_id: str = "",
    provider_configs: list[dict[str, Any]] | None = None,
    custom_model: str = "",
) -> str:
    """Convert legacy provider/model pairs to Pandrator's native model identifier."""
    providers = provider_configs if isinstance(provider_configs, list) else []
    normalized_model = str(model_name or "").strip()
    normalized_provider_id = normalize_provider_id(provider_id)

    if normalized_model.lower() in {"custom", "custom (litellm)", "local"}:
        normalized_model = str(custom_model or "").strip()
    if not normalized_model or normalized_model.lower() == "default":
        return "default"

    alias = LEGACY_DUBBING_MODEL_ALIASES.get(normalized_model.lower())
    if alias:
        return alias
    if normalized_model.startswith("custom:"):
        remainder = normalized_model[len("custom:") :]
        custom_provider_id, separator, custom_model_id = remainder.partition("/")
        if not separator or not custom_provider_id.strip() or not custom_model_id.strip():
            return "default"
        if providers and _provider_by_id(custom_provider_id, providers) is None:
            return "default"
        return normalized_model

    provider = _provider_by_id(normalized_provider_id, providers) if normalized_provider_id else None
    if provider and provider.get("is_custom", False):
        model_id = normalized_model
        custom_prefix = f"{normalized_provider_id}/"
        if model_id.startswith(custom_prefix):
            model_id = model_id[len(custom_prefix) :]
        return f"custom:{normalized_provider_id}/{model_id}"

    if normalized_model.startswith("models/"):
        normalized_model = normalized_model.split("/", 1)[1].strip()

    if "/" in normalized_model:
        return normalized_model

    if provider is None:
        provider = _provider_for_bare_model(normalized_model, providers)

    provider_key = str(provider.get("provider") or "").strip().lower() if provider else ""
    if not provider_key:
        lowered = normalized_model.lower()
        if lowered.startswith("claude"):
            provider_key = "anthropic"
        elif lowered.startswith("gemini"):
            provider_key = "gemini"
        elif lowered.startswith(("gpt", "o1", "o3", "o4")):
            provider_key = "openai"

    return f"{provider_key}/{normalized_model}" if provider_key else normalized_model


def migrate_dubbing_payload(
    payload: dict[str, Any] | None,
    provider_configs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read legacy Subdub-shaped fields and return the current Pandrator schema."""
    migrated = dict(payload or {})
    providers = provider_configs if isinstance(provider_configs, list) else []

    if "stt_language" not in migrated:
        migrated["stt_language"] = str(migrated.get("whisper_language") or "English")

    legacy_provider = str(migrated.get("translation_provider") or "").strip()
    legacy_model = str(migrated.get("translation_model") or "").strip()
    legacy_custom_model = str(migrated.get("custom_translation_model") or "").strip()
    explicit_backend = migrated.get("translation_backend")
    if explicit_backend is None:
        is_legacy_deepl = (
            normalize_provider_id(legacy_provider) == TRANSLATION_BACKEND_DEEPL
            or legacy_model.lower() == TRANSLATION_BACKEND_DEEPL
        )
        explicit_backend = TRANSLATION_BACKEND_DEEPL if is_legacy_deepl else TRANSLATION_BACKEND_LLM

    backend = normalize_translation_backend(str(explicit_backend))
    migrated["translation_backend"] = backend

    if backend == TRANSLATION_BACKEND_DEEPL:
        migrated["translation_model"] = "default"
    else:
        migrated["translation_model"] = normalize_native_llm_model(
            legacy_model,
            provider_id=legacy_provider,
            provider_configs=providers,
            custom_model=legacy_custom_model,
        )

    correction_model = migrated.get("correction_model")
    if correction_model is None:
        correction_model = legacy_model if backend == TRANSLATION_BACKEND_LLM else "default"
        correction_provider = legacy_provider if backend == TRANSLATION_BACKEND_LLM else ""
    else:
        correction_provider = ""
    migrated["correction_model"] = normalize_native_llm_model(
        str(correction_model or "default"),
        provider_id=correction_provider,
        provider_configs=providers,
        custom_model=legacy_custom_model,
    )

    for legacy_field in (
        "whisper_language",
        "translation_provider",
        "custom_translation_model",
        "custom_api_base",
    ):
        migrated.pop(legacy_field, None)
    return migrated


def normalize_dubbing_state(
    dubbing_state: Any,
    provider_configs: list[dict[str, Any]] | None = None,
) -> None:
    """Normalize an in-memory DubbingSettings-like object in place."""
    payload = migrate_dubbing_payload(vars(dubbing_state), provider_configs)
    for field_name in (
        "stt_language",
        "correction_model",
        "translation_backend",
        "translation_model",
    ):
        setattr(dubbing_state, field_name, payload[field_name])
