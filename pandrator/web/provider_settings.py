"""Resolve database-backed LLM provider records for UI-independent workers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from pandrator.logic.llm_handler import strip_output_limit_options
from pandrator.runtime import DataPaths

from .credential_identity import CredentialNamespace
from .credentials import (
    DATABASE_REFERENCE_PREFIX,
    DEFAULT_PROVIDER_ENVS,
    is_sensitive_field,
    reference_key,
    resolve_provider_credential,
    validate_vertex_service_account_json,
)
from .database import Database
from .models import Provider, ProviderModel
from .provider_identity import NONINITIAL_LLM_PROVIDER_KEYS, provider_namespace

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
    return [
        dict(profile, options=dict(profile.get("options") or {}))
        for profile in LLM_PROVIDER_PROFILES
    ]


def _request_options(options: dict[str, Any] | None) -> dict[str, Any]:
    raw = strip_output_limit_options((options or {}).get("request_options") or {})
    reserved = {
        "model",
        "messages",
        "timeout",
        "api_key",
        "api_base",
        "temperature",
        "reasoning_effort",
    }
    return {
        str(key): value
        for key, value in raw.items()
        if str(key) not in reserved and not is_sensitive_field(key)
    }


class _SecretValue(str):
    def __repr__(self) -> str:
        return "'<redacted>'"


_NONINITIAL_PROVIDER_ERROR = (
    "This provider requires a host-approved binding before it can be used in this workspace."
)
_NONINITIAL_CREDENTIAL_ERROR = (
    "Configure a database credential for this workspace before running this provider."
)


def _model_record(row: ProviderModel) -> dict[str, Any]:
    modalities_in = row.input_modalities_json or ["text"]
    return {
        "id": row.model_id,
        "default_temperature": row.default_temperature,
        "default_reasoning_effort": row.default_reasoning_effort or "",
        "input_cost_per_million": row.input_cost_per_million,
        "cached_input_cost_per_million": row.cached_input_cost_per_million,
        "output_cost_per_million": row.output_cost_per_million,
        "context_window_tokens": row.context_window_tokens,
        "max_output_tokens": row.max_output_tokens,
        "input_modalities": list(modalities_in),
        "output_modalities": list(row.output_modalities_json or ["text"]),
        "supports_audio_input": "audio" in modalities_in,
    }


def _select_scoped_provider_model(
    providers: list[Provider],
    model_rows: list[ProviderModel],
    requested_model: str,
    *,
    legacy_compatibility: bool = False,
) -> tuple[Provider, ProviderModel]:
    models_by_provider: dict[str, list[ProviderModel]] = {}
    for row in model_rows:
        models_by_provider.setdefault(row.provider_id, []).append(row)

    if requested_model:
        matches: list[tuple[Provider, ProviderModel]] = []
        for provider in providers:
            provider_key = str(provider.provider_key or "").strip()
            legacy_alias = ""
            if legacy_compatibility:
                options = provider.options_json or {}
                if options.get("is_custom") or provider.provider_key not in {
                    "openai",
                    "gemini",
                    "anthropic",
                }:
                    legacy_alias = str(options.get("provider_id") or "")
            for row in models_by_provider.get(provider.id, []):
                if not (row.is_active or row.is_default):
                    continue
                selectors = {
                    row.model_id,
                    f"{provider_key}/{row.model_id}",
                    f"custom:{provider.id}/{row.model_id}",
                }
                if legacy_alias:
                    selectors.update(
                        {
                            f"custom:{legacy_alias}/{row.model_id}",
                            f"{row.model_id}@{legacy_alias}",
                            f"{legacy_alias}/{row.model_id}",
                        }
                    )
                if requested_model in selectors:
                    matches.append((provider, row))
        if len(matches) != 1:
            raise ValueError("The requested LLM model is unavailable or ambiguous.")
        return matches[0]

    defaults = [
        (provider, row)
        for provider in providers
        for row in models_by_provider.get(provider.id, [])
        if row.is_default
    ]
    if not legacy_compatibility:
        defaults.sort(
            key=lambda item: (item[0].created_at, item[0].id, item[1].created_at, item[1].id)
        )
    if not defaults:
        raise ValueError(
            "Configure an enabled LLM provider and select a default model before running this stage."
        )
    return defaults[0]


def _build_scoped_settings(
    database: Database,
    paths: DataPaths,
    provider: Provider,
    selected_row: ProviderModel,
    model_rows: list[ProviderModel],
    namespace: CredentialNamespace,
    request_timeout_seconds: int | None,
) -> tuple[SimpleNamespace, str]:
    provider_key = str(provider.provider_key or "").strip().lower()
    if provider_key not in NONINITIAL_LLM_PROVIDER_KEYS:
        raise ValueError(_NONINITIAL_PROVIDER_ERROR)

    secret_ref = str(provider.secret_ref or "").strip()
    if not secret_ref.startswith(DATABASE_REFERENCE_PREFIX) or not reference_key(secret_ref):
        raise ValueError(_NONINITIAL_CREDENTIAL_ERROR)

    options = provider.options_json or {}
    profile_id = str(options.get("profile_id") or "").strip().lower()
    explicitly_custom = bool(
        options.get("is_custom") or profile_id in {"custom-openai", "lm-studio", "ollama"}
    )
    credential = resolve_provider_credential(
        database,
        paths,
        provider_key,
        secret_ref,
        shared=not explicitly_custom,
        workspace_id=namespace.workspace_id,
    )
    if credential.source != "database" or not credential.value:
        raise ValueError(_NONINITIAL_CREDENTIAL_ERROR)

    request_options = _request_options(options)
    credential_value = credential.value
    api_key = credential_value
    if provider_key == "vertex_ai":
        credential_payload = validate_vertex_service_account_json(credential_value)
        request_options["vertex_credentials"] = _SecretValue(credential_value)
        project_id = credential_payload.get("project_id")
        if project_id:
            request_options.setdefault("vertex_project", str(project_id))
        request_options.setdefault("vertex_location", "global")
        api_key = ""

    api_base = str(provider.base_url or "").strip()
    if provider_key == "openai" and not api_base and not explicitly_custom:
        api_base = next(
            str(profile["base_url"])
            for profile in LLM_PROVIDER_PROFILES
            if profile["id"] == "openai"
        )

    rows = [row for row in model_rows if row.is_active or row.is_default]
    resolved_model = f"custom:{provider.id}/{selected_row.model_id}"
    config = {
        "id": provider.id,
        "name": provider.label,
        "provider": provider_key,
        "api_base": api_base,
        "api_key_env": "",
        "api_key": _SecretValue(api_key) if api_key else "",
        "is_custom": True,
        "models": [_model_record(row) for row in rows],
        "models_explicit": True,
        "request_options": request_options,
    }
    return (
        SimpleNamespace(
            provider_configs=[config],
            default_model=resolved_model,
            request_timeout_seconds=max(1, int(request_timeout_seconds or 600)),
            provider_binding_id=provider.id,
            workspace_id=namespace.workspace_id,
        ),
        resolved_model,
    )


def build_llm_settings(
    database: Database,
    paths: DataPaths,
    *,
    requested_model: str | None = None,
    requested_provider_id: str | None = None,
    request_timeout_seconds: int | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> tuple[SimpleNamespace, str]:
    """Build worker settings or scope a diagnostic to one database provider.

    Explicit provider diagnostics may test disabled providers without changing
    their enabled state. Unscoped workers retain enabled-provider selection.
    """

    provider_scope = str(requested_provider_id or "").strip()
    selected_model = str(requested_model or "").strip()
    with database.snapshot_session() as session:
        namespace = provider_namespace(session, session_id=session_id, workspace_id=workspace_id)
        provider_statement = select(Provider).where(
            Provider.kind == "llm",
            Provider.workspace_id == namespace.workspace_id,
        )
        if provider_scope:
            provider_statement = provider_statement.where(Provider.id == provider_scope)
        else:
            provider_statement = provider_statement.where(Provider.enabled.is_(True))
        provider_order: list[Any] = [Provider.created_at]
        model_order: list[Any] = [ProviderModel.created_at]
        if not namespace.legacy_host_access:
            provider_order.append(Provider.id)
            model_order = [
                Provider.created_at,
                Provider.id,
                ProviderModel.created_at,
                ProviderModel.id,
            ]
        providers = list(session.scalars(provider_statement.order_by(*provider_order)).all())
        if provider_scope and not providers:
            raise ValueError("The requested LLM provider is unavailable.")
        model_statement = (
            select(ProviderModel)
            .join(Provider, ProviderModel.provider_id == Provider.id)
            .where(
                Provider.workspace_id == namespace.workspace_id,
                Provider.id.in_([provider.id for provider in providers]),
            )
        )
        model_rows = list(session.scalars(model_statement.order_by(*model_order)).all())

    if not namespace.host_credentials_allowed:
        provider, selected_row = _select_scoped_provider_model(
            providers,
            model_rows,
            selected_model,
            legacy_compatibility=namespace.legacy_host_access,
        )
        return _build_scoped_settings(
            database,
            paths,
            provider,
            selected_row,
            [row for row in model_rows if row.provider_id == provider.id],
            namespace,
            request_timeout_seconds,
        )

    if provider_scope and not providers:
        raise ValueError("The requested LLM provider is unavailable.")

    models_by_provider: dict[str, list[ProviderModel]] = {}
    for model in model_rows:
        models_by_provider.setdefault(model.provider_id, []).append(model)

    provider_configs: list[dict[str, Any]] = []
    default_model = ""
    for provider in providers:
        # A default model is necessarily usable. Treat legacy/directly-created
        # rows that predate the activation flag the same way the API does when
        # it promotes a model to the application default.
        rows = [
            row
            for row in models_by_provider.get(provider.id, [])
            if row.is_active or row.is_default
        ]
        if provider_scope and (
            not rows or (selected_model and not any(row.model_id == selected_model for row in rows))
        ):
            raise ValueError("Activate the selected model on this provider before testing it.")
        if not rows:
            continue
        fallback_env = str((provider.options_json or {}).get("api_key_env") or "").strip()
        if not fallback_env:
            fallback_env = DEFAULT_PROVIDER_ENVS.get(
                str(provider.provider_key or "").strip().lower(), ""
            )
        profile_id = str((provider.options_json or {}).get("profile_id") or "").strip().lower()
        share_credential = not bool(
            (provider.options_json or {}).get("is_custom")
            or profile_id in {"custom-openai", "lm-studio", "ollama"}
        )
        credential = resolve_provider_credential(
            database,
            paths,
            provider.provider_key,
            provider.secret_ref,
            fallback_environment_variable=fallback_env,
            shared=share_credential,
            workspace_id=namespace.workspace_id,
        )
        is_custom = bool(
            (provider.options_json or {}).get("is_custom")
            or provider.provider_key not in {"openai", "gemini", "anthropic"}
        )
        provider_id = str(
            (provider.options_json or {}).get("provider_id") or provider.provider_key or provider.id
        )
        if is_custom:
            provider_id = str((provider.options_json or {}).get("provider_id") or provider.id)
        records = [_model_record(row) for row in rows]
        request_options = _request_options(provider.options_json)
        credential_value = credential.value
        if str(provider.provider_key or "").strip().lower() == "vertex_ai":
            if credential_value:
                request_options["vertex_credentials"] = _SecretValue(credential_value)
                try:
                    credential_payload = json.loads(credential_value)
                except (TypeError, ValueError):
                    credential_payload = {}
                if isinstance(credential_payload, dict) and credential_payload.get("project_id"):
                    request_options.setdefault(
                        "vertex_project", str(credential_payload["project_id"])
                    )
            request_options.setdefault("vertex_location", "global")
            credential_value = ""
        provider_configs.append(
            {
                "id": provider_id,
                "name": provider.label,
                "provider": provider.provider_key,
                "api_base": provider.base_url or "",
                "api_key_env": credential.environment_variable,
                "api_key": _SecretValue(credential_value) if credential_value else "",
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
        raise ValueError(
            "Configure an enabled LLM provider and select a default model before running this stage."
        )
    return (
        SimpleNamespace(
            provider_configs=provider_configs,
            default_model=resolved_model,
            request_timeout_seconds=max(1, int(request_timeout_seconds or 600)),
            provider_binding_id=None,
            workspace_id=namespace.workspace_id,
        ),
        resolved_model,
    )
