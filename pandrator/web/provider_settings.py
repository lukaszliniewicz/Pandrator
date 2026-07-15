"""Resolve database-backed LLM provider records for UI-independent workers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .credentials import DEFAULT_PROVIDER_ENVS, is_sensitive_field, resolve_provider_credential
from .database import Database
from .models import Provider, ProviderModel


LLM_PROVIDER_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "label": "OpenAI",
        "provider_key": "openai",
        "base_url": "https://api.openai.com/v1",
        "secret_ref": "env:OPENAI_API_KEY",
        "description": "OpenAI chat and reasoning models through LiteLLM.",
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "provider_key": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "secret_ref": "env:ANTHROPIC_API_KEY",
        "description": "Claude models using the Anthropic API.",
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "provider_key": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "secret_ref": "env:GEMINI_API_KEY",
        "description": "Gemini models using a Google AI Studio API key.",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "provider_key": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "secret_ref": "env:OPENROUTER_API_KEY",
        "description": "A broad hosted model catalogue routed through OpenRouter.",
    },
    {
        "id": "groq",
        "label": "Groq",
        "provider_key": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "secret_ref": "env:GROQ_API_KEY",
        "description": "Low-latency hosted inference through Groq.",
    },
    {
        "id": "mistral",
        "label": "Mistral AI",
        "provider_key": "mistral",
        "base_url": "https://api.mistral.ai/v1",
        "secret_ref": "env:MISTRAL_API_KEY",
        "description": "Mistral's hosted chat model catalogue.",
    },
    {
        "id": "lm-studio",
        "label": "LM Studio",
        "provider_key": "openai",
        "base_url": "http://127.0.0.1:1234/v1",
        "secret_ref": "",
        "description": "A local OpenAI-compatible LM Studio server.",
        "options": {"is_custom": True, "profile_id": "lm-studio"},
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "provider_key": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "secret_ref": "",
        "description": "A local Ollama server using LiteLLM's native adapter.",
        "options": {"is_custom": True, "profile_id": "ollama"},
    },
    {
        "id": "azure",
        "label": "Azure OpenAI",
        "provider_key": "azure",
        "base_url": "",
        "secret_ref": "env:AZURE_API_KEY",
        "description": "Azure-hosted deployments. Set the endpoint and any API version in advanced LiteLLM options.",
    },
    {
        "id": "vertex-ai",
        "label": "Google Vertex AI",
        "provider_key": "vertex_ai",
        "base_url": "",
        "secret_ref": "",
        "description": "Vertex AI using preferred application-default credentials or a pasted service-account JSON key. The project is read from the JSON and the location defaults to global.",
        "options": {"request_options": {"vertex_location": "global"}},
    },
    {
        "id": "bedrock",
        "label": "Amazon Bedrock",
        "provider_key": "bedrock",
        "base_url": "",
        "secret_ref": "",
        "description": "Bedrock using the standard AWS credential environment or profile.",
    },
    {
        "id": "custom-openai",
        "label": "OpenAI-compatible endpoint",
        "provider_key": "openai",
        "base_url": "",
        "secret_ref": "",
        "description": "LM Studio, llama.cpp, vLLM, or another compatible endpoint with a custom URL.",
        "options": {"is_custom": True, "profile_id": "custom-openai"},
    },
)


def list_llm_provider_profiles() -> list[dict[str, Any]]:
    return [dict(profile, options=dict(profile.get("options") or {})) for profile in LLM_PROVIDER_PROFILES]


def _request_options(options: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict((options or {}).get("request_options") or {})
    reserved = {"model", "messages", "timeout", "api_key", "api_base", "temperature", "reasoning_effort"}
    return {
        str(key): value
        for key, value in raw.items()
        if str(key) not in reserved and not is_sensitive_field(key)
    }


def build_llm_settings(
    database: Database,
    paths: DataPaths,
    *,
    requested_model: str | None = None,
    request_timeout_seconds: int | None = None,
) -> tuple[SimpleNamespace, str]:
    """Build the legacy-compatible settings object consumed by the shared LLM engine."""

    with database.session() as session:
        providers = list(
            session.scalars(
                select(Provider).where(Provider.kind == "llm", Provider.enabled.is_(True)).order_by(Provider.created_at)
            ).all()
        )
        model_rows = list(session.scalars(select(ProviderModel).order_by(ProviderModel.created_at)).all())

    models_by_provider: dict[str, list[ProviderModel]] = {}
    for model in model_rows:
        models_by_provider.setdefault(model.provider_id, []).append(model)

    provider_configs: list[dict[str, Any]] = []
    selected_model = str(requested_model or "").strip()
    default_model = ""
    for provider in providers:
        rows = [row for row in models_by_provider.get(provider.id, []) if row.is_active]
        if not rows:
            continue
        fallback_env = str((provider.options_json or {}).get("api_key_env") or "").strip()
        if not fallback_env:
            fallback_env = DEFAULT_PROVIDER_ENVS.get(str(provider.provider_key or "").strip().lower(), "")
        profile_id = str((provider.options_json or {}).get("profile_id") or "").strip().lower()
        share_credential = not bool((provider.options_json or {}).get("is_custom") or profile_id in {"custom-openai", "lm-studio", "ollama"})
        credential = resolve_provider_credential(
            database,
            paths,
            provider.provider_key,
            provider.secret_ref,
            fallback_environment_variable=fallback_env,
            shared=share_credential,
        )
        is_custom = bool(
            (provider.options_json or {}).get("is_custom")
            or provider.provider_key not in {"openai", "gemini", "anthropic"}
        )
        provider_id = str((provider.options_json or {}).get("provider_id") or provider.provider_key or provider.id)
        if is_custom:
            provider_id = str((provider.options_json or {}).get("provider_id") or provider.id)
        records = [
            {
                "id": row.model_id,
                "default_temperature": row.default_temperature,
                "default_reasoning_effort": row.default_reasoning_effort or "",
                "input_cost_per_million": row.input_cost_per_million,
                "cached_input_cost_per_million": row.cached_input_cost_per_million,
                "output_cost_per_million": row.output_cost_per_million,
            }
            for row in rows
        ]
        request_options = _request_options(provider.options_json)
        credential_value = credential.value
        if str(provider.provider_key or "").strip().lower() == "vertex_ai":
            if credential_value:
                request_options["vertex_credentials"] = credential_value
                try:
                    credential_payload = json.loads(credential_value)
                except (TypeError, ValueError):
                    credential_payload = {}
                if isinstance(credential_payload, dict) and credential_payload.get("project_id"):
                    request_options.setdefault("vertex_project", str(credential_payload["project_id"]))
            request_options.setdefault("vertex_location", "global")
            credential_value = ""
        provider_configs.append(
            {
                "id": provider_id,
                "name": provider.label,
                "provider": provider.provider_key,
                "api_base": provider.base_url or "",
                "api_key_env": credential.environment_variable,
                "api_key": credential_value,
                "is_custom": is_custom,
                "models": records,
                "models_explicit": True,
                "request_options": request_options,
            }
        )
        default_row = next((row for row in rows if row.is_default), None)
        matching_row = next((row for row in rows if row.model_id == selected_model), None)
        if matching_row is not None:
            selected_model = (
                f"custom:{provider_id}/{matching_row.model_id}"
                if is_custom
                else f"{provider.provider_key}/{matching_row.model_id}"
            )
        if default_row is not None and not default_model:
            default_model = (
                f"custom:{provider_id}/{default_row.model_id}"
                if is_custom
                else f"{provider.provider_key}/{default_row.model_id}"
            )

    resolved_model = selected_model or default_model
    if not resolved_model:
        raise ValueError("Configure an enabled LLM provider and select a default model before running this stage.")
    return (
        SimpleNamespace(
            provider_configs=provider_configs,
            default_model=resolved_model,
            request_timeout_seconds=max(1, int(request_timeout_seconds or 600)),
        ),
        resolved_model,
    )
