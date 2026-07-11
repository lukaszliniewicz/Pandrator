"""Task-specific LLM request configuration for dubbing workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import llm_handler
from .settings import migrate_dubbing_payload


STAGE_MODEL_FIELDS = {
    "correction": "correction_model",
    "translation": "translation_model",
    "zoom": "correction_model",
}

DUBBING_LLM_MIN_REQUEST_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class DubbingLLMSettings:
    model_name: str
    llm_settings: dict[str, Any]


def resolve_dubbing_llm_settings(
    settings: dict[str, Any],
    *,
    stage: str,
) -> DubbingLLMSettings:
    """Resolve one dubbing stage through Pandrator's native LLM contract."""
    normalized_stage = str(stage or "").strip().lower()
    model_field = STAGE_MODEL_FIELDS.get(normalized_stage)
    if model_field is None:
        raise ValueError(f"Unsupported dubbing LLM stage: {stage}")

    provider_configs = settings.get("llm_provider_configs")
    if not isinstance(provider_configs, list):
        provider_configs = []
    migrated = migrate_dubbing_payload(settings, provider_configs)
    model_name = str(migrated.get(model_field) or "default").strip() or "default"

    try:
        configured_timeout = int(
            settings.get("request_timeout_seconds", DUBBING_LLM_MIN_REQUEST_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError):
        configured_timeout = DUBBING_LLM_MIN_REQUEST_TIMEOUT_SECONDS

    request_settings = {
        "default_model": str(
            settings.get("llm_default_model")
            or settings.get("default_llm_model")
            or llm_handler.DEFAULT_LITELLM_MODEL
        ).strip(),
        "provider_configs": provider_configs,
        "request_timeout_seconds": max(
            DUBBING_LLM_MIN_REQUEST_TIMEOUT_SECONDS,
            configured_timeout,
        ),
    }
    return DubbingLLMSettings(model_name=model_name, llm_settings=request_settings)
