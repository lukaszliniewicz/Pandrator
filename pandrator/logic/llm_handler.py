import copy
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_LITELLM_MODEL = "openai/gpt-5.4-mini"
PLACEHOLDER_API_KEY = "sk-placeholder"

BUILTIN_PROVIDER_ORDER = ["openai", "gemini", "anthropic"]
BUILTIN_PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "provider": "openai",
        "api_base": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "api_key": "",
        "is_custom": False,
        "models": ["gpt-5.4", "gpt-5.4-mini"],
    },
    "gemini": {
        "id": "gemini",
        "name": "Gemini",
        "provider": "gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "api_key_env": "GEMINI_API_KEY",
        "api_key": "",
        "is_custom": False,
        "models": ["gemini-3.1-pro-preview", "gemini-3-flash-preview"],
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "provider": "anthropic",
        "api_base": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key": "",
        "is_custom": False,
        "models": ["claude-opus-4-7", "claude-sonnet-4-6"],
    },
}

KNOWN_PROVIDER_PREFIXES = {
    "openai",
    "gemini",
    "anthropic",
    "vertex_ai",
    "azure",
    "bedrock",
    "openrouter",
    "groq",
    "mistral",
    "ollama",
}

NON_TEXT_MODEL_KEYWORDS = (
    "embedding",
    "embeddings",
    "tts",
    "speech",
    "transcribe",
    "transcription",
    "audio",
    "image",
    "moderation",
    "whisper",
    "dall",
)

_litellm_completion = None
_litellm_get_valid_models = None
_litellm_import_attempted = False


@dataclass
class ChatCompletionResult:
    content: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    cost: float | None = None
    cost_source: str = ""
    response_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_litellm_clients() -> tuple[Any, Any]:
    global _litellm_completion, _litellm_get_valid_models, _litellm_import_attempted
    if not _litellm_import_attempted:
        _litellm_import_attempted = True
        try:
            from litellm import completion as litellm_completion
            from litellm.utils import get_valid_models as litellm_get_valid_models
        except Exception as e:  # pragma: no cover - runtime dependency guard
            logging.debug("LiteLLM import failed: %s", e)
        else:
            _litellm_completion = litellm_completion
            _litellm_get_valid_models = litellm_get_valid_models

    return _litellm_completion, _litellm_get_valid_models


def _read_setting(settings: Any, key: str, default: Any = None) -> Any:
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _dedupe_ordered(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized)
    return deduped


def _normalize_base_url(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/")


def _normalize_provider_key(raw_provider: str | None) -> str:
    provider = str(raw_provider or "").strip().lower()
    aliases = {
        "google": "gemini",
        "google-ai": "gemini",
        "google_ai": "gemini",
        "google-ai-studio": "gemini",
        "openai-compatible": "openai",
        "openai_compatible": "openai",
    }
    provider = aliases.get(provider, provider)
    if provider in {"openai", "gemini", "anthropic"}:
        return provider
    return provider


def _normalize_provider_id(raw_name: str | None) -> str:
    lowered = str(raw_name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug


def _normalize_model_id(raw_model: str, provider: str) -> str:
    normalized = str(raw_model or "").strip()
    if not normalized:
        return ""

    if normalized.lower().startswith("models/"):
        normalized = normalized.split("/", 1)[1].strip()

    if normalized.startswith("custom:"):
        remainder = normalized[len("custom:") :].strip()
        _, separator, model_part = remainder.partition("/")
        if separator and model_part.strip():
            normalized = model_part.strip()

    if "/" in normalized:
        prefix, remainder = normalized.split("/", 1)
        if _normalize_provider_key(prefix) == provider and remainder.strip():
            return remainder.strip()

    return normalized


def _to_litellm_model_name(provider: str, model_id: str) -> str:
    normalized_provider = _normalize_provider_key(provider) or "openai"
    normalized_model = str(model_id or "").strip()
    if not normalized_model:
        return ""

    if "/" in normalized_model:
        prefix, _ = normalized_model.split("/", 1)
        if prefix.strip().lower() in KNOWN_PROVIDER_PREFIXES:
            return normalized_model

    return f"{normalized_provider}/{normalized_model}"


def _parse_models(raw_models: Any, provider: str) -> list[str]:
    candidate_items: list[str] = []
    if isinstance(raw_models, list):
        candidate_items = [str(item) for item in raw_models]
    elif isinstance(raw_models, str):
        split_items = re.split(r"[,\n;]", raw_models)
        candidate_items = [str(item) for item in split_items]

    normalized_models = [
        _normalize_model_id(model, provider)
        for model in candidate_items
    ]
    return _dedupe_ordered(normalized_models)


def _parse_legacy_custom_endpoints(raw_json: str) -> list[dict[str, Any]]:
    raw_text = str(raw_json or "").strip()
    if not raw_text:
        return []

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logging.warning("Could not parse legacy custom endpoint JSON: %s", e)
        return []

    if not isinstance(payload, list):
        return []

    converted: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        provider_id = _normalize_provider_id(name)
        if not provider_id or provider_id in BUILTIN_PROVIDER_CONFIGS:
            continue

        api_base = _normalize_base_url(
            item.get("base_url")
            or item.get("api_base")
            or ""
        )
        if not api_base:
            continue

        raw_models = item.get("models")
        if not raw_models and item.get("default_model"):
            raw_models = [item.get("default_model")]

        converted.append(
            {
                "id": provider_id,
                "name": name or provider_id,
                "provider": "openai",
                "api_base": api_base,
                "api_key_env": str(item.get("api_key_env", "")).strip(),
                "api_key": str(item.get("api_key", "")).strip(),
                "is_custom": True,
                "models": _parse_models(raw_models, "openai"),
            }
        )

    return converted


def _coerce_provider_record(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    raw_id = str(item.get("id") or item.get("name") or "").strip()
    provider_id = _normalize_provider_id(raw_id)
    if not provider_id:
        return None

    explicit_provider = _normalize_provider_key(
        item.get("provider")
        or item.get("litellm_provider")
        or provider_id
    )

    if provider_id in BUILTIN_PROVIDER_CONFIGS:
        record = copy.deepcopy(BUILTIN_PROVIDER_CONFIGS[provider_id])
        models = _parse_models(item.get("models", []), record["provider"])
        if models:
            record["models"] = models

        name = str(item.get("name") or "").strip()
        if name:
            record["name"] = name

        api_base = _normalize_base_url(item.get("api_base") or item.get("base_url") or "")
        if api_base:
            record["api_base"] = api_base

        api_key_env = str(item.get("api_key_env") or "").strip()
        if api_key_env:
            record["api_key_env"] = api_key_env

        api_key = str(item.get("api_key") or "").strip()
        if api_key:
            record["api_key"] = api_key

        record["is_custom"] = False
        return record

    api_base = _normalize_base_url(item.get("api_base") or item.get("base_url") or "")

    return {
        "id": provider_id,
        "name": str(item.get("name") or raw_id or provider_id).strip() or provider_id,
        "provider": explicit_provider or "openai",
        "api_base": api_base,
        "api_key_env": str(item.get("api_key_env") or "").strip(),
        "api_key": str(item.get("api_key") or "").strip(),
        "is_custom": True,
        "models": _parse_models(item.get("models", []), explicit_provider or "openai"),
    }


def get_provider_configs(llm_settings: Any | None = None) -> list[dict[str, Any]]:
    builtins: dict[str, dict[str, Any]] = {
        provider_id: copy.deepcopy(config)
        for provider_id, config in BUILTIN_PROVIDER_CONFIGS.items()
    }

    custom_configs: dict[str, dict[str, Any]] = {}
    raw_provider_configs = _read_setting(llm_settings, "provider_configs", [])
    if isinstance(raw_provider_configs, list):
        for item in raw_provider_configs:
            record = _coerce_provider_record(item)
            if record is None:
                continue

            provider_id = record["id"]
            if provider_id in builtins and not record.get("is_custom", False):
                builtins[provider_id] = record
            elif provider_id not in builtins:
                custom_configs[provider_id] = record

    legacy_raw_json = _read_setting(llm_settings, "custom_openai_endpoints_json", "")
    for legacy_record in _parse_legacy_custom_endpoints(str(legacy_raw_json or "")):
        provider_id = legacy_record["id"]
        if provider_id not in custom_configs:
            custom_configs[provider_id] = legacy_record

    ordered_providers: list[dict[str, Any]] = [
        builtins[provider_id]
        for provider_id in BUILTIN_PROVIDER_ORDER
        if provider_id in builtins
    ]

    sorted_custom = sorted(
        custom_configs.values(),
        key=lambda item: str(item.get("name") or item.get("id") or "").lower(),
    )

    for provider in ordered_providers + sorted_custom:
        provider_key = _normalize_provider_key(provider.get("provider"))
        if not provider_key:
            provider_key = "openai"
        provider["provider"] = provider_key

        provider["api_base"] = _normalize_base_url(provider.get("api_base"))
        provider["api_key_env"] = str(provider.get("api_key_env") or "").strip()
        provider["api_key"] = str(provider.get("api_key") or "").strip()

        models = _parse_models(provider.get("models", []), provider["provider"])
        if models:
            provider["models"] = models
        elif provider["id"] in BUILTIN_PROVIDER_CONFIGS:
            provider["models"] = list(BUILTIN_PROVIDER_CONFIGS[provider["id"]]["models"])
        else:
            provider["models"] = []

    return ordered_providers + sorted_custom


def normalize_default_model(model_name: str | None) -> str:
    normalized = str(model_name or "").strip()
    if not normalized or normalized == "default":
        return DEFAULT_LITELLM_MODEL
    return normalized


def normalize_llm_settings(llm_settings: Any | None):
    if llm_settings is None:
        return

    normalized_configs = get_provider_configs(llm_settings)
    if _read_setting(llm_settings, "provider_configs", []) != normalized_configs:
        setattr(llm_settings, "provider_configs", normalized_configs)

    normalized_default_model = normalize_default_model(
        _read_setting(llm_settings, "default_model", DEFAULT_LITELLM_MODEL)
    )
    if _read_setting(llm_settings, "default_model", "") != normalized_default_model:
        setattr(llm_settings, "default_model", normalized_default_model)


def list_custom_provider_configs(llm_settings: Any | None = None) -> list[dict[str, Any]]:
    return [
        provider
        for provider in get_provider_configs(llm_settings)
        if provider.get("is_custom", False)
    ]


def _resolve_api_key(provider_config: dict[str, Any]) -> str:
    api_key_env = str(provider_config.get("api_key_env") or "").strip()
    if api_key_env:
        env_value = os.getenv(api_key_env, "").strip()
        if env_value:
            return env_value

    explicit = str(provider_config.get("api_key") or "").strip()
    if explicit:
        return explicit

    return ""


def _is_text_model(model_id: str) -> bool:
    lowered = model_id.lower()
    if not lowered:
        return False

    for keyword in NON_TEXT_MODEL_KEYWORDS:
        if keyword in lowered:
            return False

    return True


def _normalize_detected_models(models: list[str], provider: str) -> list[str]:
    normalized_models: list[str] = []
    for model in models:
        model_id = _normalize_model_id(model, provider)
        if model_id and _is_text_model(model_id):
            normalized_models.append(model_id)
    return _dedupe_ordered(normalized_models)


def _detect_models_for_builtin_provider(provider_config: dict[str, Any]) -> list[str]:
    fallback_models = _parse_models(provider_config.get("models", []), provider_config.get("provider", ""))
    provider = _normalize_provider_key(provider_config.get("provider"))
    if not provider:
        return fallback_models

    _, get_valid_models = _get_litellm_clients()
    if get_valid_models is None:
        return fallback_models

    api_key = _resolve_api_key(provider_config)
    api_base = _normalize_base_url(provider_config.get("api_base"))

    discovered_models: list[str] = []
    try:
        discovered_models = get_valid_models(
            check_provider_endpoint=True,
            custom_llm_provider=provider,
            api_key=api_key or None,
            api_base=api_base or None,
        )
    except Exception as e:
        logging.debug("LiteLLM endpoint model detection failed for provider '%s': %s", provider, e)

    if not discovered_models:
        try:
            discovered_models = get_valid_models(
                check_provider_endpoint=False,
                custom_llm_provider=provider,
                api_key=api_key or None,
                api_base=api_base or None,
            )
        except Exception as e:
            logging.debug("LiteLLM fallback model detection failed for provider '%s': %s", provider, e)

    normalized_detected = _normalize_detected_models(
        [str(model) for model in discovered_models],
        provider,
    )

    if normalized_detected:
        return normalized_detected

    return fallback_models


def refresh_builtin_provider_models(
    llm_settings: Any | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    provider_configs = get_provider_configs(llm_settings)
    status_lines: list[str] = []

    for provider in provider_configs:
        if provider.get("is_custom", False):
            continue

        detected_models = _detect_models_for_builtin_provider(provider)
        if detected_models:
            provider["models"] = detected_models
            status_lines.append(
                f"{provider['name']}: loaded {len(detected_models)} model(s)."
            )
        else:
            status_lines.append(f"{provider['name']}: no models detected.")

    return provider_configs, status_lines


def save_custom_provider(
    llm_settings: Any | None,
    provider_name: str,
    provider_key: str,
    api_base: str,
    api_key: str = "",
    models: list[str] | str | None = None,
) -> tuple[bool, list[dict[str, Any]], str, str]:
    display_name = str(provider_name or "").strip()
    if not display_name:
        return False, get_provider_configs(llm_settings), "", "Provider name is required."

    provider_id = _normalize_provider_id(display_name)
    if not provider_id:
        return False, get_provider_configs(llm_settings), "", "Provider name must include letters or numbers."

    if provider_id in BUILTIN_PROVIDER_CONFIGS:
        return (
            False,
            get_provider_configs(llm_settings),
            "",
            f"'{display_name}' is reserved for a built-in provider.",
        )

    normalized_provider_key = _normalize_provider_key(provider_key)
    if not normalized_provider_key:
        return False, get_provider_configs(llm_settings), "", "LiteLLM provider is required."

    normalized_api_base = _normalize_base_url(api_base)
    if normalized_provider_key == "openai" and not normalized_api_base:
        return False, get_provider_configs(llm_settings), "", "API base URL is required."

    provider_configs = get_provider_configs(llm_settings)
    normalized_models = _parse_models(models or [], normalized_provider_key)

    custom_record = {
        "id": provider_id,
        "name": display_name,
        "provider": normalized_provider_key,
        "api_base": normalized_api_base,
        "api_key_env": "",
        "api_key": str(api_key or "").strip(),
        "is_custom": True,
        "models": normalized_models,
    }

    custom_updated = False
    updated_configs: list[dict[str, Any]] = []
    for record in provider_configs:
        if record.get("id") == provider_id and record.get("is_custom", False):
            updated_configs.append(custom_record)
            custom_updated = True
        else:
            updated_configs.append(record)

    if not custom_updated:
        updated_configs.append(custom_record)

    builtins = [item for item in updated_configs if not item.get("is_custom", False)]
    customs = [item for item in updated_configs if item.get("is_custom", False)]
    customs = sorted(customs, key=lambda item: str(item.get("name") or "").lower())

    return True, builtins + customs, provider_id, ""


def update_provider(
    llm_settings: Any | None,
    provider_id: str,
    provider_name: str,
    provider_key: str,
    api_base: str,
    api_key: str,
    models: list[str] | str | None,
) -> tuple[bool, list[dict[str, Any]], str]:
    normalized_provider_id = _normalize_provider_id(provider_id)
    if not normalized_provider_id:
        return False, get_provider_configs(llm_settings), "Provider id is required."

    provider_configs = get_provider_configs(llm_settings)
    existing = next(
        (
            provider
            for provider in provider_configs
            if str(provider.get("id") or "") == normalized_provider_id
        ),
        None,
    )

    is_builtin = normalized_provider_id in BUILTIN_PROVIDER_CONFIGS
    if existing is None and not is_builtin:
        return False, provider_configs, f"Provider '{provider_id}' was not found."

    if is_builtin:
        normalized_provider_key = _normalize_provider_key(provider_key) or BUILTIN_PROVIDER_CONFIGS[normalized_provider_id]["provider"]
    else:
        normalized_provider_key = _normalize_provider_key(provider_key)
        if not normalized_provider_key:
            return False, provider_configs, "LiteLLM provider is required."

    normalized_api_base = _normalize_base_url(api_base)
    if not is_builtin and normalized_provider_key == "openai" and not normalized_api_base:
        return False, provider_configs, "API base URL is required for OpenAI-compatible providers."

    if is_builtin:
        updated_record = copy.deepcopy(BUILTIN_PROVIDER_CONFIGS[normalized_provider_id])
        if existing is not None:
            updated_record.update(existing)
    else:
        updated_record = copy.deepcopy(existing or {})

    display_name = str(provider_name or "").strip()
    if not display_name:
        display_name = str(updated_record.get("name") or normalized_provider_id)

    parsed_models = _parse_models(models or [], normalized_provider_key)
    if not parsed_models:
        if is_builtin:
            parsed_models = list(BUILTIN_PROVIDER_CONFIGS[normalized_provider_id]["models"])
        else:
            parsed_models = _parse_models(updated_record.get("models", []), normalized_provider_key)

    updated_record.update(
        {
            "id": normalized_provider_id,
            "name": display_name,
            "provider": normalized_provider_key,
            "api_base": normalized_api_base or str(updated_record.get("api_base") or "").strip(),
            "api_key_env": str(updated_record.get("api_key_env") or "").strip(),
            "api_key": str(api_key or "").strip(),
            "is_custom": not is_builtin,
            "models": parsed_models,
        }
    )

    updated_configs: list[dict[str, Any]] = []
    replaced = False
    for provider in provider_configs:
        if str(provider.get("id") or "") == normalized_provider_id:
            updated_configs.append(updated_record)
            replaced = True
        else:
            updated_configs.append(provider)

    if not replaced:
        updated_configs.append(updated_record)

    builtins = [item for item in updated_configs if not item.get("is_custom", False)]
    customs = [item for item in updated_configs if item.get("is_custom", False)]
    customs = sorted(customs, key=lambda item: str(item.get("name") or "").lower())

    builtins_by_id = {
        str(item.get("id") or ""): item
        for item in builtins
    }
    ordered_builtins = [
        builtins_by_id[provider_id]
        for provider_id in BUILTIN_PROVIDER_ORDER
        if provider_id in builtins_by_id
    ]

    return True, ordered_builtins + customs, ""


def remove_custom_provider(
    llm_settings: Any | None,
    provider_name_or_id: str,
) -> tuple[bool, list[dict[str, Any]], str]:
    provider_id = _normalize_provider_id(provider_name_or_id)
    if not provider_id:
        return False, get_provider_configs(llm_settings), "Select a custom provider first."

    if provider_id in BUILTIN_PROVIDER_CONFIGS:
        return False, get_provider_configs(llm_settings), "Built-in providers cannot be removed."

    provider_configs = get_provider_configs(llm_settings)
    updated_configs = [
        provider
        for provider in provider_configs
        if not (provider.get("is_custom", False) and provider.get("id") == provider_id)
    ]

    if len(updated_configs) == len(provider_configs):
        return False, provider_configs, f"Custom provider '{provider_name_or_id}' was not found."

    return True, updated_configs, ""


def _provider_models_for_catalog(provider_config: dict[str, Any]) -> list[str]:
    provider = provider_config.get("provider") or "openai"
    models = _parse_models(provider_config.get("models", []), provider)
    if models:
        return models

    provider_id = str(provider_config.get("id") or "").strip().lower()
    if provider_id in BUILTIN_PROVIDER_CONFIGS:
        return list(BUILTIN_PROVIDER_CONFIGS[provider_id]["models"])

    return []


def list_models(llm_settings: Any | None = None) -> list[str]:
    """Returns model suggestions for LiteLLM usage."""
    normalize_llm_settings(llm_settings)
    suggestions: list[str] = ["default"]

    default_model = normalize_default_model(
        _read_setting(llm_settings, "default_model", DEFAULT_LITELLM_MODEL)
    )
    if default_model not in suggestions:
        suggestions.append(default_model)

    for provider in get_provider_configs(llm_settings):
        provider_models = _provider_models_for_catalog(provider)
        provider_id = str(provider.get("id") or "").strip()
        if provider.get("is_custom", False):
            if provider_models:
                suggestions.extend(
                    [
                        f"custom:{provider_id}/{model_id}"
                        for model_id in provider_models
                    ]
                )
            elif provider_id:
                suggestions.append(f"custom:{provider_id}/<model-id>")
            continue

        provider_name = str(provider.get("provider") or "openai")
        suggestions.extend(
            [
                _to_litellm_model_name(provider_name, model_id)
                for model_id in provider_models
            ]
        )

    return _dedupe_ordered(suggestions)


def _infer_provider_for_unprefixed_model(
    model_id: str,
    provider_configs: list[dict[str, Any]],
) -> str:
    normalized_model_id = str(model_id or "").strip()
    if not normalized_model_id:
        return ""

    matching_providers: list[str] = []
    for provider in provider_configs:
        if provider.get("is_custom", False):
            continue

        provider_models = _provider_models_for_catalog(provider)
        if normalized_model_id in provider_models:
            matching_providers.append(str(provider.get("provider") or ""))

    unique_matches = list({provider for provider in matching_providers if provider})
    if len(unique_matches) == 1:
        return unique_matches[0]

    lowered = normalized_model_id.lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"

    return ""


def _resolve_model_request(
    model_name: str | None,
    llm_settings: Any | None,
) -> tuple[str, dict[str, Any]]:
    configured_default = normalize_default_model(
        _read_setting(llm_settings, "default_model", DEFAULT_LITELLM_MODEL)
    )
    normalized_model = (model_name or "").strip()
    if not normalized_model or normalized_model == "default":
        normalized_model = configured_default

    provider_configs = get_provider_configs(llm_settings)
    providers_by_id = {
        str(provider.get("id") or "").strip(): provider
        for provider in provider_configs
    }
    custom_provider_ids = {
        provider_id
        for provider_id, provider in providers_by_id.items()
        if provider.get("is_custom", False)
    }

    custom_provider_id = ""

    if normalized_model.startswith("custom:"):
        remainder = normalized_model[len("custom:") :].strip()
        custom_provider_id, separator, endpoint_model = remainder.partition("/")
        custom_provider_id = custom_provider_id.strip()
        if not separator or not custom_provider_id or not endpoint_model.strip():
            raise ValueError("Custom provider model must use custom:<provider>/<model>.")
        normalized_model = endpoint_model.strip()

    if not custom_provider_id and "@" in normalized_model:
        model_part, endpoint_suffix = normalized_model.rsplit("@", 1)
        endpoint_suffix = endpoint_suffix.strip()
        if endpoint_suffix in custom_provider_ids:
            custom_provider_id = endpoint_suffix
            normalized_model = model_part.strip()

    if not custom_provider_id and "/" in normalized_model:
        prefix, remainder = normalized_model.split("/", 1)
        if prefix in custom_provider_ids and remainder.strip():
            custom_provider_id = prefix
            normalized_model = remainder.strip()

    request_overrides: dict[str, Any] = {}
    if custom_provider_id:
        provider = providers_by_id.get(custom_provider_id)
        if provider is None or not provider.get("is_custom", False):
            raise ValueError(f"Custom provider '{custom_provider_id}' is not configured.")

        if not normalized_model or normalized_model == "<model-id>":
            raise ValueError(
                f"Custom provider '{custom_provider_id}' does not have a concrete model selected."
            )

        custom_provider_key = _normalize_provider_key(provider.get("provider")) or "openai"
        custom_model_id = _normalize_model_id(normalized_model, custom_provider_key)
        if not custom_model_id:
            raise ValueError(
                f"Custom provider '{custom_provider_id}' has an invalid model identifier."
            )

        resolved_model = _to_litellm_model_name(custom_provider_key, custom_model_id)
        api_base = str(provider.get("api_base") or "").strip()
        if custom_provider_key == "openai" and not api_base:
            raise ValueError(
                f"Custom provider '{custom_provider_id}' is missing an API base URL."
            )

        if api_base:
            request_overrides["api_base"] = api_base

        resolved_api_key = _resolve_api_key(provider)
        if custom_provider_key == "openai":
            request_overrides["api_key"] = resolved_api_key or PLACEHOLDER_API_KEY
        elif resolved_api_key:
            request_overrides["api_key"] = resolved_api_key

        return resolved_model, request_overrides

    if "/" not in normalized_model:
        inferred_provider = _infer_provider_for_unprefixed_model(
            normalized_model,
            provider_configs,
        )
        if inferred_provider:
            normalized_model = _to_litellm_model_name(inferred_provider, normalized_model)

    return normalized_model, request_overrides


def _extract_choice_content(response_data: Any) -> str:
    if response_data is None:
        return ""

    if hasattr(response_data, "model_dump"):
        payload = response_data.model_dump()
    elif isinstance(response_data, dict):
        payload = response_data
    else:
        payload = {}

    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    if not choices:
        return ""

    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)

    return str(content or "")


def _response_payload(response_data: Any) -> dict[str, Any]:
    if response_data is None:
        return {}
    if isinstance(response_data, dict):
        return response_data
    if hasattr(response_data, "model_dump"):
        try:
            payload = response_data.model_dump(mode="json")
        except TypeError:
            payload = response_data.model_dump()
        return payload if isinstance(payload, dict) else {}
    if hasattr(response_data, "dict"):
        payload = response_data.dict()
        return payload if isinstance(payload, dict) else {}
    return {}


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_response_cost(response_data: Any, payload: dict[str, Any]) -> tuple[float | None, str]:
    hidden_params = getattr(response_data, "_hidden_params", None)
    if not isinstance(hidden_params, dict):
        hidden_params = payload.get("_hidden_params")
    if not isinstance(hidden_params, dict):
        hidden_params = {}

    response_cost = _coerce_optional_float(hidden_params.get("response_cost"))
    if response_cost is not None:
        return response_cost, "litellm_hidden_params"

    usage = payload.get("usage")
    if isinstance(usage, dict):
        usage_cost = _coerce_optional_float(usage.get("cost"))
        if usage_cost is not None:
            return usage_cost, "response_usage"

    header_sources = [
        getattr(response_data, "_response_headers", None),
        hidden_params.get("additional_headers"),
    ]
    for headers in header_sources:
        if not isinstance(headers, dict):
            continue
        normalized_headers = {str(key).lower(): value for key, value in headers.items()}
        for key in (
            "x-litellm-response-cost",
            "llm_provider-x-litellm-response-cost",
        ):
            header_cost = _coerce_optional_float(normalized_headers.get(key))
            if header_cost is not None:
                return header_cost, "litellm_response_header"

    try:
        from litellm import completion_cost

        calculated_cost = _coerce_optional_float(completion_cost(completion_response=response_data))
    except Exception:
        calculated_cost = None
    if calculated_cost is not None:
        return calculated_cost, "litellm_completion_cost"

    return None, ""


def _extract_chat_completion_result(response_data: Any, requested_model: str = "") -> ChatCompletionResult:
    payload = _response_payload(response_data)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    cost, cost_source = _extract_response_cost(response_data, payload)
    return ChatCompletionResult(
        content=_extract_choice_content(response_data),
        model=str(payload.get("model") or requested_model or ""),
        usage=usage,
        cost=cost,
        cost_source=cost_source,
        response_id=str(payload.get("id") or ""),
    )


def chat_completion_with_metadata(
    messages: list[dict[str, Any]],
    model_name: str | None = None,
    llm_settings: Any | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.2,
) -> ChatCompletionResult:
    """Runs a LiteLLM chat completion and preserves usage/cost metadata."""
    completion, _ = _get_litellm_clients()
    if completion is None:
        logging.error("LiteLLM is not installed. Please install the 'litellm' package.")
        return ChatCompletionResult()

    try:
        resolved_model, request_overrides = _resolve_model_request(model_name, llm_settings)
    except ValueError as e:
        logging.error("Could not resolve LLM model '%s': %s", model_name, e)
        return ChatCompletionResult()

    timeout_setting = _read_setting(llm_settings, "request_timeout_seconds", 180)
    try:
        request_timeout = max(10, int(timeout_setting))
    except (TypeError, ValueError):
        request_timeout = 180

    request_payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "max_tokens": max(256, int(max_tokens)),
        "temperature": float(temperature),
        "timeout": request_timeout,
    }
    request_payload.update(request_overrides)

    logging.info("LiteLLM chat request model=%s", resolved_model)
    try:
        response = completion(**request_payload)
        result = _extract_chat_completion_result(response, requested_model=resolved_model)
        if not result.content:
            logging.warning("LiteLLM returned an empty chat response body.")
        return result
    except Exception as e:
        logging.error("LiteLLM chat request failed: %s", e)
        return ChatCompletionResult(model=resolved_model)


def chat_completion(
    messages: list[dict[str, Any]],
    model_name: str | None = None,
    llm_settings: Any | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.2,
) -> str:
    """Runs a LiteLLM chat completion using Pandrator's configured providers."""
    result = chat_completion_with_metadata(
        messages=messages,
        model_name=model_name,
        llm_settings=llm_settings,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return result.content


def load_model(model_name: str, llm_settings: Any | None = None) -> bool:
    """LiteLLM calls are stateless; this validates model configuration only."""
    try:
        normalize_llm_settings(llm_settings)
        _resolve_model_request(model_name, llm_settings)
        return True
    except ValueError as e:
        logging.error("Invalid LiteLLM model configuration '%s': %s", model_name, e)
        return False


def unload_model() -> bool:
    """Compatibility no-op for stateless LiteLLM calls."""
    return True


def _make_api_request(
    text: str,
    user_prompt: str,
    model_name: str | None = None,
    llm_settings: Any | None = None,
) -> str:
    """Makes a single LiteLLM chat completion request."""
    completion, _ = _get_litellm_clients()
    if completion is None:
        logging.error("LiteLLM is not installed. Please install the 'litellm' package.")
        return ""

    sanitized_text = text.replace("\n", " ").replace("\t", " ")
    try:
        resolved_model, request_overrides = _resolve_model_request(model_name, llm_settings)
    except ValueError as e:
        logging.error("Could not resolve LLM model '%s': %s", model_name, e)
        return ""

    timeout_setting = _read_setting(llm_settings, "request_timeout_seconds", 180)
    try:
        request_timeout = max(10, int(timeout_setting))
    except (TypeError, ValueError):
        request_timeout = 180

    request_payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": f"{user_prompt}{sanitized_text}"}],
        "max_tokens": 1500,
        "temperature": 0.4,
        "timeout": request_timeout,
    }
    request_payload.update(request_overrides)

    logging.info("LiteLLM request model=%s", resolved_model)
    logging.debug("LiteLLM request payload: %s", request_payload)

    try:
        response = completion(**request_payload)
        content = _extract_choice_content(response)
        if not content:
            logging.warning("LiteLLM returned an empty response body.")
        return content
    except Exception as e:
        logging.error("LiteLLM request failed: %s", e)
        return ""


def _evaluate_and_choose(
    original_text: str,
    original_prompt: str,
    result1: str,
    result2: str,
    model_name: str | None = None,
    llm_settings: Any | None = None,
) -> str:
    """Asks the LLM to evaluate which of two results is better."""
    cleaned_prompt = original_prompt.replace("This is your text:", "").strip()
    evaluation_prompt = (
        f"A language model was asked to perform this task twice: '{cleaned_prompt}'. "
        f"This was the text to process: '{original_text}'. "
        f"This was result 1: '{result1}'. This was result 2: '{result2}' "
        "Which is better? Output ONLY the digit 1 or 2 and nothing else. "
        "No explanations, acknowledgments, notes, comments."
    )

    evaluation_result = _make_api_request(
        evaluation_prompt,
        "",
        model_name=model_name,
        llm_settings=llm_settings,
    )

    first_20_chars = evaluation_result.strip()[:20]
    if "1" in first_20_chars and "2" not in first_20_chars:
        return result1
    if "2" in first_20_chars and "1" not in first_20_chars:
        return result2

    # Default to result1 if evaluation is ambiguous
    return result1


def process_text(
    text: str,
    prompt: str,
    evaluate: bool = False,
    model_name: str | None = None,
    llm_settings: Any | None = None,
) -> str:
    """
    Processes a given text using an LLM with a specified prompt.
    Optionally evaluates two runs to choose the better result.
    """
    if not text or not prompt:
        return ""

    if not evaluate:
        return _make_api_request(
            text,
            prompt,
            model_name=model_name,
            llm_settings=llm_settings,
        )

    result1 = _make_api_request(
        text,
        prompt,
        model_name=model_name,
        llm_settings=llm_settings,
    )
    result2 = _make_api_request(
        text,
        prompt,
        model_name=model_name,
        llm_settings=llm_settings,
    )
    return _evaluate_and_choose(
        text,
        prompt,
        result1,
        result2,
        model_name=model_name,
        llm_settings=llm_settings,
    )
