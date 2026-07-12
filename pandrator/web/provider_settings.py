"""Resolve database-backed LLM provider records for UI-independent workers."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .database import Database
from .models import Provider, ProviderModel


def _secret_value(reference: str | None, paths: DataPaths) -> tuple[str, str]:
    """Return ``(api_key, api_key_env)`` without persisting resolved secrets."""

    value = str(reference or "").strip()
    if not value:
        return "", ""
    if value.startswith("env:"):
        return "", value[4:].strip()
    if value.startswith("keyring:"):
        target = value[8:].strip()
        service, separator, username = target.partition("/")
        if not separator or not service or not username:
            raise ValueError("Keyring secret references must use keyring:<service>/<username>.")
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("The keyring package is required for this provider secret reference.") from error
        return str(keyring.get_password(service, username) or ""), ""
    if value.startswith("file:"):
        key = value[5:].strip()
        if not key:
            raise ValueError("File secret references must use file:<key>.")
        if not paths.secrets_file.is_file():
            return "", ""
        if os.name != "nt":
            mode = stat.S_IMODE(paths.secrets_file.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PermissionError("The headless secrets file must only be accessible by its owner.")
        payload = json.loads(paths.secrets_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The headless secrets file must contain a JSON object.")
        return str(payload.get(key) or ""), ""
    raise ValueError("Secret references must use env:, keyring:, or file:.")


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
        rows = models_by_provider.get(provider.id, [])
        if not rows:
            continue
        api_key, api_key_env = _secret_value(provider.secret_ref, paths)
        is_custom = bool(provider.base_url or (provider.options_json or {}).get("is_custom"))
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
        provider_configs.append(
            {
                "id": provider_id,
                "name": provider.label,
                "provider": provider.provider_key,
                "api_base": provider.base_url or "",
                "api_key_env": api_key_env,
                "api_key": api_key,
                "is_custom": is_custom,
                "models": records,
                "models_explicit": True,
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
