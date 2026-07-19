import copy
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .retry_utils import (
    retry_after_seconds,
    retry_delay_seconds,
    retryable_error,
    status_code_from_error,
    wait_for_retry,
)

DEFAULT_LITELLM_MODEL = "openai/gpt-5.4-mini"
PLACEHOLDER_API_KEY = "sk-placeholder"


def default_model_record(model_id: str) -> dict[str, Any]:
    return {
        "id": str(model_id or "").strip(),
        "default_temperature": None,
        "default_reasoning_effort": "",
        "input_cost_per_million": None,
        "cached_input_cost_per_million": None,
        "output_cost_per_million": None,
    }


def _builtin_models(*model_ids: str) -> list[dict[str, Any]]:
    return [default_model_record(model_id) for model_id in model_ids]

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
        "models": _builtin_models("gpt-5.4", "gpt-5.4-mini"),
    },
    "gemini": {
        "id": "gemini",
        "name": "Gemini",
        "provider": "gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "api_key_env": "GEMINI_API_KEY",
        "api_key": "",
        "is_custom": False,
        "models": _builtin_models("gemini-3.1-pro-preview", "gemini-3-flash-preview"),
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "provider": "anthropic",
        "api_base": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key": "",
        "is_custom": False,
        "models": _builtin_models("claude-opus-4-7", "claude-sonnet-4-6"),
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
COMMON_PROVIDER_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
CLOUD_API_KEY_PROVIDER_KEYS = {
    "anthropic",
    "azure",
    "bedrock",
    "gemini",
    "groq",
    "mistral",
    "openrouter",
}
KEYLESS_PROVIDER_KEYS = {
    "ollama",
}
LOCAL_BASE_URL_HOSTS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "[::1]",
)

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


@dataclass(frozen=True)
class LLMCredentialStatus:
    ok: bool
    model: str = ""
    provider_id: str = ""
    provider_name: str = ""
    provider: str = ""
    api_key_env: str = ""
    api_base: str = ""
    needs_api_key: bool = False
    message: str = ""


def _get_litellm_clients() -> tuple[Any, Any]:
    global _litellm_completion, _litellm_get_valid_models, _litellm_import_attempted
    if not _litellm_import_attempted:
        _litellm_import_attempted = True
        try:
            import litellm
            from litellm import completion as litellm_completion
            from litellm.utils import get_valid_models as litellm_get_valid_models
        except Exception as e:  # pragma: no cover - runtime dependency guard
            logging.debug("LiteLLM import failed: %s", e)
        else:
            # Drop unsupported parameters (e.g. reasoning_effort on non-reasoning models)
            # silently instead of raising UnsupportedParamsError.
            try:
                litellm.drop_params = True
            except Exception:
                pass
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
        candidate_items = [
            str(item.get("id") or "") if isinstance(item, dict) else str(item)
            for item in raw_models
        ]
    elif isinstance(raw_models, str):
        split_items = re.split(r"[,\n;]", raw_models)
        candidate_items = [str(item) for item in split_items]

    normalized_models = [
        _normalize_model_id(model, provider)
        for model in candidate_items
    ]
    return _dedupe_ordered(normalized_models)


def _model_optional_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def normalize_model_records(raw_models: Any, provider: str) -> list[dict[str, Any]]:
    """Normalize legacy string model IDs and current model-setting records."""
    raw_items = (
        raw_models
        if isinstance(raw_models, list)
        else re.split(r"[,\n;]", str(raw_models or ""))
    )
    records_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        source = item if isinstance(item, dict) else {"id": item}
        model_id = _normalize_model_id(str(source.get("id") or ""), provider)
        if not model_id:
            continue
        record = default_model_record(model_id)
        temperature = _model_optional_float(source.get("default_temperature"))
        if temperature is not None and temperature <= 2.0:
            record["default_temperature"] = temperature
        record["default_reasoning_effort"] = str(
            source.get("default_reasoning_effort") or ""
        ).strip()
        for key in (
            "input_cost_per_million",
            "cached_input_cost_per_million",
            "output_cost_per_million",
        ):
            record[key] = _model_optional_float(source.get(key))
        records_by_id[model_id] = record
    return list(records_by_id.values())


def model_ids(raw_models: Any, provider: str) -> list[str]:
    return [record["id"] for record in normalize_model_records(raw_models, provider)]


def _merge_model_records(existing: Any, discovered: Any, provider: str) -> list[dict[str, Any]]:
    merged = {record["id"]: record for record in normalize_model_records(existing, provider)}
    for model_id in _parse_models(discovered, provider):
        merged.setdefault(model_id, default_model_record(model_id))
    return list(merged.values())


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
                "models": normalize_model_records(raw_models, "openai"),
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
        models = normalize_model_records(item.get("models", []), record["provider"])
        if "models" in item:
            record["models"] = models
            record["models_explicit"] = True

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
        request_options = item.get("request_options")
        record["request_options"] = dict(request_options) if isinstance(request_options, dict) else {}
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
        "models": normalize_model_records(item.get("models", []), explicit_provider or "openai"),
        "models_explicit": True,
        "request_options": dict(item.get("request_options") or {}) if isinstance(item.get("request_options"), dict) else {},
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

        models = normalize_model_records(provider.get("models", []), provider["provider"])
        if models:
            provider["models"] = models
        elif provider["id"] in BUILTIN_PROVIDER_CONFIGS and not provider.get("models_explicit", False):
            provider["models"] = copy.deepcopy(BUILTIN_PROVIDER_CONFIGS[provider["id"]]["models"])
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


def _is_local_base_url(api_base: str | None) -> bool:
    normalized = _normalize_base_url(api_base).lower()
    if not normalized:
        return False
    return any(
        normalized.startswith(f"http://{host}") or normalized.startswith(f"https://{host}")
        for host in LOCAL_BASE_URL_HOSTS
    )


def _common_provider_env(provider_key: str) -> str:
    return COMMON_PROVIDER_API_KEY_ENVS.get(_normalize_provider_key(provider_key), "")


def _common_provider_api_key(provider_key: str) -> str:
    env_name = _common_provider_env(provider_key)
    return os.getenv(env_name, "").strip() if env_name else ""


def _provider_requires_api_key(provider_config: dict[str, Any] | None, provider_key: str) -> bool:
    normalized_provider = _normalize_provider_key(provider_key)
    if normalized_provider in KEYLESS_PROVIDER_KEYS:
        return False

    if provider_config:
        if _is_local_base_url(provider_config.get("api_base")):
            return False
        if provider_config.get("is_custom", False) and normalized_provider == "openai":
            return False
        provider_id = str(provider_config.get("id") or "").strip()
        if provider_id in BUILTIN_PROVIDER_CONFIGS:
            return True

    return normalized_provider in CLOUD_API_KEY_PROVIDER_KEYS


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
            provider["models"] = _merge_model_records(
                provider.get("models", []), detected_models, str(provider.get("provider") or "")
            )
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
    models: Any = None,
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
    existing_custom = next(
        (
            record for record in provider_configs
            if record.get("id") == provider_id and record.get("is_custom", False)
        ),
        None,
    )
    normalized_models = (
        normalize_model_records(models, normalized_provider_key)
        if isinstance(models, list) and any(isinstance(item, dict) for item in models)
        else _merge_model_records(
            (existing_custom or {}).get("models", []), models or [], normalized_provider_key
        )
    )

    custom_record = {
        "id": provider_id,
        "name": display_name,
        "provider": normalized_provider_key,
        "api_base": normalized_api_base,
        "api_key_env": "",
        "api_key": str(api_key or "").strip(),
        "is_custom": True,
        "models": normalized_models,
        "models_explicit": True,
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
    models: Any,
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

    parsed_models = (
        normalize_model_records(models, normalized_provider_key)
        if isinstance(models, list) and any(isinstance(item, dict) for item in models)
        else _merge_model_records(
            updated_record.get("models", []), models or [], normalized_provider_key
        )
    )
    if not parsed_models:
        if is_builtin:
            parsed_models = copy.deepcopy(BUILTIN_PROVIDER_CONFIGS[normalized_provider_id]["models"])
        else:
            parsed_models = normalize_model_records(updated_record.get("models", []), normalized_provider_key)

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
            "models_explicit": True,
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
    if provider_id in BUILTIN_PROVIDER_CONFIGS and not provider_config.get("models_explicit", False):
        return _parse_models(BUILTIN_PROVIDER_CONFIGS[provider_id]["models"], provider)

    return []


def _find_model_record(provider_config: dict[str, Any] | None, model_id: str) -> dict[str, Any] | None:
    if not provider_config:
        return None
    provider = str(provider_config.get("provider") or "openai")
    normalized_id = _normalize_model_id(model_id, provider)
    for record in normalize_model_records(provider_config.get("models", []), provider):
        if record["id"] == normalized_id:
            return record
    return None


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


def _provider_key_for_resolved_model(model_name: str) -> str:
    normalized_model = str(model_name or "").strip()
    if "/" not in normalized_model:
        return ""

    prefix, _remainder = normalized_model.split("/", 1)
    return _normalize_provider_key(prefix)


def _builtin_provider_config_for_key(
    provider_configs: list[dict[str, Any]],
    provider_key: str,
) -> dict[str, Any] | None:
    normalized_provider_key = _normalize_provider_key(provider_key)
    if not normalized_provider_key:
        return None

    for provider in provider_configs:
        if provider.get("is_custom", False):
            continue
        provider_id = str(provider.get("id") or "").strip()
        configured_provider_key = _normalize_provider_key(provider.get("provider"))
        if provider_id == normalized_provider_key or configured_provider_key == normalized_provider_key:
            return provider

    return None


def _resolve_model_request_details(
    model_name: str | None,
    llm_settings: Any | None,
) -> dict[str, Any]:
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

        provider_options = provider.get("request_options")
        if isinstance(provider_options, dict):
            reserved = {"model", "messages", "timeout", "api_key", "api_base", "temperature", "reasoning_effort"}
            request_overrides.update(
                {str(key): value for key, value in provider_options.items() if str(key) not in reserved}
            )

        if api_base:
            request_overrides["api_base"] = api_base

        resolved_api_key = _resolve_api_key(provider)
        if custom_provider_key == "vertex_ai":
            if resolved_api_key and "vertex_credentials" not in request_overrides:
                request_overrides["vertex_credentials"] = resolved_api_key
        elif custom_provider_key == "openai":
            request_overrides["api_key"] = resolved_api_key or PLACEHOLDER_API_KEY
        elif resolved_api_key:
            request_overrides["api_key"] = resolved_api_key

        return {
            "model": resolved_model,
            "request_overrides": request_overrides,
            "provider_config": provider,
            "model_record": _find_model_record(provider, custom_model_id),
            "provider_key": custom_provider_key,
            "is_custom": True,
        }

    if "/" not in normalized_model:
        inferred_provider = _infer_provider_for_unprefixed_model(
            normalized_model,
            provider_configs,
        )
        if inferred_provider:
            normalized_model = _to_litellm_model_name(inferred_provider, normalized_model)

    provider_key = _provider_key_for_resolved_model(normalized_model)
    provider_config = _builtin_provider_config_for_key(provider_configs, provider_key)
    provider_options = provider_config.get("request_options") if provider_config else None
    if isinstance(provider_options, dict):
        reserved = {"model", "messages", "timeout", "api_key", "api_base", "temperature", "reasoning_effort"}
        request_overrides.update(
            {str(key): value for key, value in provider_options.items() if str(key) not in reserved}
        )
    provider_api_base = str(provider_config.get("api_base") or "").strip() if provider_config else ""
    if provider_api_base:
        request_overrides["api_base"] = provider_api_base
    resolved_api_key = _resolve_api_key(provider_config) if provider_config else _common_provider_api_key(provider_key)
    if provider_key == "vertex_ai" and resolved_api_key:
        request_overrides.setdefault("vertex_credentials", resolved_api_key)
    elif resolved_api_key:
        request_overrides["api_key"] = resolved_api_key

    return {
        "model": normalized_model,
        "request_overrides": request_overrides,
        "provider_config": provider_config,
        "model_record": _find_model_record(provider_config, normalized_model),
        "provider_key": provider_key,
        "is_custom": False,
    }


def _resolve_model_request(
    model_name: str | None,
    llm_settings: Any | None,
) -> tuple[str, dict[str, Any]]:
    details = _resolve_model_request_details(model_name, llm_settings)
    return str(details["model"]), dict(details.get("request_overrides") or {})


def validate_model_credentials(
    model_name: str | None,
    llm_settings: Any | None = None,
) -> LLMCredentialStatus:
    """Validate local model/provider configuration and required API key presence."""
    try:
        details = _resolve_model_request_details(model_name, llm_settings)
    except ValueError as error:
        return LLMCredentialStatus(ok=False, message=str(error))

    resolved_model = str(details.get("model") or "")
    request_overrides = dict(details.get("request_overrides") or {})
    provider_config = details.get("provider_config")
    provider_config = provider_config if isinstance(provider_config, dict) else None
    provider_key = _normalize_provider_key(details.get("provider_key")) or _provider_key_for_resolved_model(resolved_model)

    provider_id = str(provider_config.get("id") or "") if provider_config else provider_key
    provider_name = str(provider_config.get("name") or "") if provider_config else provider_key
    api_base = str(provider_config.get("api_base") or "") if provider_config else ""
    api_key_env = str(provider_config.get("api_key_env") or "") if provider_config else ""
    if not api_key_env:
        api_key_env = _common_provider_env(provider_key)

    resolved_api_key = ""
    override_api_key = request_overrides.get("api_key")
    if isinstance(override_api_key, str) and override_api_key != PLACEHOLDER_API_KEY:
        resolved_api_key = override_api_key.strip()
    if not resolved_api_key and provider_config:
        resolved_api_key = _resolve_api_key(provider_config)
    if not resolved_api_key and api_key_env:
        resolved_api_key = os.getenv(api_key_env, "").strip()

    needs_api_key = _provider_requires_api_key(provider_config, provider_key)
    if needs_api_key and not resolved_api_key:
        label = provider_name or provider_id or provider_key or resolved_model
        env_hint = f" Save an API key in Connections or set {api_key_env}." if api_key_env else ""
        return LLMCredentialStatus(
            ok=False,
            model=resolved_model,
            provider_id=provider_id,
            provider_name=provider_name,
            provider=provider_key,
            api_key_env=api_key_env,
            api_base=api_base,
            needs_api_key=True,
            message=f"{label} requires an API key before this LLM request can run.{env_hint}",
        )

    return LLMCredentialStatus(
        ok=True,
        model=resolved_model,
        provider_id=provider_id,
        provider_name=provider_name,
        provider=provider_key,
        api_key_env=api_key_env,
        api_base=api_base,
        needs_api_key=needs_api_key,
        message="LLM provider configuration is valid.",
    )


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


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage_tokens(usage: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(usage) if isinstance(usage, dict) else {}
    prompt_tokens = _usage_int(normalized.get("prompt_tokens") or normalized.get("input_tokens"))
    completion_tokens = _usage_int(
        normalized.get("completion_tokens") or normalized.get("output_tokens")
    )
    cached_candidates = [
        normalized.get("cache_read_input_tokens"),
        normalized.get("cached_input_tokens"),
    ]
    for details_key in ("prompt_tokens_details", "input_tokens_details", "token_details"):
        details = normalized.get(details_key)
        if isinstance(details, dict):
            cached_candidates.extend(
                [details.get("cached_tokens"), details.get("cache_read_input_tokens")]
            )
    cached_tokens = max((_usage_int(value) for value in cached_candidates), default=0)
    cached_tokens = min(prompt_tokens, cached_tokens) if prompt_tokens else cached_tokens
    normalized["prompt_tokens"] = prompt_tokens
    normalized["completion_tokens"] = completion_tokens
    normalized["cached_prompt_tokens"] = cached_tokens
    normalized["uncached_prompt_tokens"] = max(0, prompt_tokens - cached_tokens)
    normalized.setdefault("total_tokens", prompt_tokens + completion_tokens)
    return normalized


def _custom_model_cost(
    usage: dict[str, Any], model_record: dict[str, Any] | None
) -> float | None:
    if not model_record:
        return None
    input_rate = _model_optional_float(model_record.get("input_cost_per_million"))
    output_rate = _model_optional_float(model_record.get("output_cost_per_million"))
    if input_rate is None or output_rate is None:
        return None
    cached_rate = _model_optional_float(model_record.get("cached_input_cost_per_million"))
    if cached_rate is None:
        cached_rate = input_rate
    normalized_usage = normalize_usage_tokens(usage)
    return round(
        (
            normalized_usage["uncached_prompt_tokens"] * input_rate
            + normalized_usage["cached_prompt_tokens"] * cached_rate
            + normalized_usage["completion_tokens"] * output_rate
        )
        / 1_000_000.0,
        12,
    )


def _extract_chat_completion_result(
    response_data: Any,
    requested_model: str = "",
    model_record: dict[str, Any] | None = None,
) -> ChatCompletionResult:
    payload = _response_payload(response_data)
    usage = payload.get("usage")
    usage = normalize_usage_tokens(usage if isinstance(usage, dict) else {})
    cost, cost_source = _extract_response_cost(response_data, payload)
    if cost is None:
        cost = _custom_model_cost(usage, model_record)
        if cost is not None:
            cost_source = "custom_model_pricing"
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
    max_tokens: int | None = None,
    cancel_event: Any | None = None,
    retry_callback: Any | None = None,
) -> ChatCompletionResult:
    """Runs a LiteLLM chat completion and preserves usage/cost metadata."""
    completion, _ = _get_litellm_clients()
    if completion is None:
        logging.error("LiteLLM is not installed. Please install the 'litellm' package.")
        return ChatCompletionResult()

    try:
        details = _resolve_model_request_details(model_name, llm_settings)
        resolved_model = str(details["model"])
        request_overrides = dict(details.get("request_overrides") or {})
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
        "timeout": request_timeout,
    }
    request_payload.update(request_overrides)
    if max_tokens is not None:
        try:
            request_payload["max_tokens"] = max(1, int(max_tokens))
        except (TypeError, ValueError):
            logging.warning("Ignoring invalid max_tokens value: %r", max_tokens)
    model_record = details.get("model_record")
    if isinstance(model_record, dict):
        temperature = _model_optional_float(model_record.get("default_temperature"))
        if temperature is not None and temperature <= 2.0:
            request_payload["temperature"] = temperature
    reasoning_effort = str(
        model_record.get("default_reasoning_effort", "")
        if isinstance(model_record, dict)
        else ""
    ).strip()
    if reasoning_effort:
        request_payload["reasoning_effort"] = reasoning_effort

    try:
        max_attempts = max(1, min(20, int(_read_setting(llm_settings, "llm_max_attempts", 5) or 5)))
    except (TypeError, ValueError):
        max_attempts = 5
    try:
        maximum_retry_delay = max(
            1.0,
            min(300.0, float(_read_setting(llm_settings, "llm_retry_max_delay_seconds", 90) or 90)),
        )
    except (TypeError, ValueError):
        maximum_retry_delay = 90.0
    try:
        base_retry_delay = max(
            0.1,
            min(30.0, float(_read_setting(llm_settings, "llm_retry_base_delay_seconds", 1) or 1)),
        )
    except (TypeError, ValueError):
        base_retry_delay = 1.0

    accumulated_cost = 0.0
    accumulated_cost_sources: list[str] = []
    accumulated_usage: dict[str, Any] = {}
    provider_response_count = 0
    logging.info("LiteLLM chat request model=%s max_attempts=%d", resolved_model, max_attempts)
    for attempt in range(1, max_attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            logging.info("LiteLLM chat request canceled before attempt %d/%d", attempt, max_attempts)
            return ChatCompletionResult(
                model=resolved_model,
                usage={**accumulated_usage, "provider_response_count": provider_response_count},
                cost=accumulated_cost if accumulated_cost_sources else None,
                cost_source=",".join(accumulated_cost_sources),
            )
        error: BaseException | None = None
        try:
            response = completion(**request_payload)
            result = _extract_chat_completion_result(
                response,
                requested_model=resolved_model,
                model_record=model_record if isinstance(model_record, dict) else None,
            )
            provider_response_count += 1
            for key, value in (result.usage or {}).items():
                if isinstance(value, (int, float)):
                    accumulated_usage[key] = accumulated_usage.get(key, 0) + value
            if result.cost is not None:
                accumulated_cost += float(result.cost)
                if result.cost_source and result.cost_source not in accumulated_cost_sources:
                    accumulated_cost_sources.append(result.cost_source)
            if result.content:
                result.usage = {
                    **accumulated_usage,
                    "provider_response_count": provider_response_count,
                }
                result.cost = accumulated_cost if accumulated_cost_sources else result.cost
                result.cost_source = ",".join(accumulated_cost_sources) or result.cost_source
                return result
            error = RuntimeError("LiteLLM returned an empty chat response body.")
        except Exception as caught:
            error = caught

        assert error is not None
        status = status_code_from_error(error)
        retryable = retryable_error(error)
        logging.warning(
            "LiteLLM chat attempt %d/%d failed%s: %s",
            attempt,
            max_attempts,
            f" (HTTP {status})" if status else "",
            error,
        )
        if not retryable or attempt >= max_attempts:
            logging.error("LiteLLM chat request failed after %d attempt(s): %s", attempt, error)
            break
        delay = retry_delay_seconds(
            attempt,
            retry_after=retry_after_seconds(error),
            base_delay=base_retry_delay,
            maximum_delay=maximum_retry_delay,
        )
        if retry_callback is not None:
            retry_callback(attempt + 1, max_attempts, delay)
        logging.info(
            "Retrying LiteLLM chat request in %.1f seconds (attempt %d/%d)",
            delay,
            attempt + 1,
            max_attempts,
        )
        if not wait_for_retry(delay, cancel_event):
            logging.info("LiteLLM chat retry wait was canceled.")
            break

    return ChatCompletionResult(
        model=resolved_model,
        usage={**accumulated_usage, "provider_response_count": provider_response_count},
        cost=accumulated_cost if accumulated_cost_sources else None,
        cost_source=",".join(accumulated_cost_sources),
    )


def chat_completion(
    messages: list[dict[str, Any]],
    model_name: str | None = None,
    llm_settings: Any | None = None,
    max_tokens: int | None = None,
) -> str:
    """Runs a LiteLLM chat completion using Pandrator's configured providers."""
    result = chat_completion_with_metadata(
        messages=messages,
        model_name=model_name,
        llm_settings=llm_settings,
        max_tokens=max_tokens,
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
        details = _resolve_model_request_details(model_name, llm_settings)
        resolved_model = str(details["model"])
        request_overrides = dict(details.get("request_overrides") or {})
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
        "timeout": request_timeout,
    }
    request_payload.update(request_overrides)
    model_record = details.get("model_record")
    if isinstance(model_record, dict):
        temperature = _model_optional_float(model_record.get("default_temperature"))
        if temperature is not None and temperature <= 2.0:
            request_payload["temperature"] = temperature
        reasoning_effort = str(model_record.get("default_reasoning_effort") or "").strip()
        if reasoning_effort:
            request_payload["reasoning_effort"] = reasoning_effort
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
