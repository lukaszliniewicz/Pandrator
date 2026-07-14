"""Write-only database credentials shared by LLM, TTS, and auxiliary providers."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .database import Database
from .models import StoredCredential, utcnow


DATABASE_REFERENCE_PREFIX = "db:"
DEFAULT_PROVIDER_ENVS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "azure": "AZURE_API_KEY",
}
TTS_SERVICE_ENVS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "voxcpm": "VOXCPM_API_KEY",
    "fishs2": "FISHS2_API_KEY",
    "voxtral": "VOXTRAL_API_KEY",
    "kokoro": "KOKORO_API_KEY",
    "kobold_qwen": "KOBOLD_QWEN_API_KEY",
}
AUXILIARY_CREDENTIALS: tuple[dict[str, str], ...] = (
    {
        "id": "deepl",
        "label": "DeepL",
        "description": "Used when subtitle translation selects the DeepL backend.",
        "environment_variable": "DEEPL_API_KEY",
    },
)

_SENSITIVE_FIELD = re.compile(r"(^|_)(api_key|password|secret|credential)s?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    value: str = ""
    environment_variable: str = ""
    source: str = "none"

    @property
    def configured(self) -> bool:
        return bool(
            self.value
            or (
                self.environment_variable
                and os.environ.get(self.environment_variable, "").strip()
            )
        )

    def resolved_value(self) -> str:
        if self.value:
            return self.value
        if self.environment_variable:
            return os.environ.get(self.environment_variable, "").strip()
        return ""


def normalize_credential_id(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-")
    if not normalized:
        raise ValueError("Credential identifiers must include letters or numbers.")
    return normalized


def provider_credential_key(provider_id: object) -> str:
    return f"llm:{normalize_credential_id(provider_id)}"


def tts_credential_key(service_id: object) -> str:
    return f"tts:{normalize_credential_id(service_id).replace('-', '_')}"


def auxiliary_credential_key(credential_id: object) -> str:
    return f"aux:{normalize_credential_id(credential_id)}"


def database_reference(key: str) -> str:
    return f"{DATABASE_REFERENCE_PREFIX}{key}"


def reference_key(reference: object) -> str:
    value = str(reference or "").strip()
    return value[len(DATABASE_REFERENCE_PREFIX) :].strip() if value.startswith(DATABASE_REFERENCE_PREFIX) else ""


def upsert_credential(session: Session, key: str, label: str, secret_value: object) -> StoredCredential:
    value = str(secret_value or "").strip()
    if not value:
        raise ValueError("API keys cannot be blank.")
    record = session.get(StoredCredential, key)
    if record is None:
        record = StoredCredential(key=key, label=str(label or key), secret_value=value)
        session.add(record)
    else:
        record.label = str(label or record.label)
        record.secret_value = value
        record.updated_at = utcnow()
    return record


def delete_credential(session: Session, key: str) -> bool:
    record = session.get(StoredCredential, key)
    if record is None:
        return False
    session.delete(record)
    return True


def resolve_secret_reference(
    database: Database,
    paths: DataPaths,
    reference: object,
    *,
    fallback_environment_variable: str = "",
) -> ResolvedCredential:
    """Resolve a reference without exposing its value through an API response."""

    value = str(reference or "").strip()
    fallback = str(fallback_environment_variable or "").strip()
    if not value:
        return ResolvedCredential(environment_variable=fallback, source="environment" if fallback else "none")
    if value.startswith(DATABASE_REFERENCE_PREFIX):
        key = reference_key(value)
        if key:
            with database.session() as session:
                record = session.get(StoredCredential, key)
                if record is not None:
                    return ResolvedCredential(value=str(record.secret_value or ""), source="database")
        return ResolvedCredential(environment_variable=fallback, source="environment" if fallback else "none")
    if value.startswith("env:"):
        environment_variable = value[4:].strip()
        return ResolvedCredential(
            environment_variable=environment_variable,
            source="environment" if environment_variable else "none",
        )
    if value.startswith("keyring:"):
        target = value[8:].strip()
        service, separator, username = target.partition("/")
        if not separator or not service or not username:
            raise ValueError("Keyring secret references must use keyring:<service>/<username>.")
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("The keyring package is required for this provider secret reference.") from error
        return ResolvedCredential(value=str(keyring.get_password(service, username) or ""), source="keyring")
    if value.startswith("file:"):
        key = value[5:].strip()
        if not key:
            raise ValueError("File secret references must use file:<key>.")
        if not paths.secrets_file.is_file():
            return ResolvedCredential(environment_variable=fallback, source="environment" if fallback else "none")
        if os.name != "nt":
            mode = stat.S_IMODE(paths.secrets_file.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PermissionError("The headless secrets file must only be accessible by its owner.")
        payload = json.loads(paths.secrets_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The headless secrets file must contain a JSON object.")
        return ResolvedCredential(value=str(payload.get(key) or ""), source="file")
    raise ValueError("Secret references must use db:, env:, keyring:, or file:.")


def credential_status(
    database: Database,
    paths: DataPaths,
    reference: object,
    *,
    fallback_environment_variable: str = "",
) -> dict[str, Any]:
    try:
        resolved = resolve_secret_reference(
            database,
            paths,
            reference,
            fallback_environment_variable=fallback_environment_variable,
        )
        return {"credential_configured": resolved.configured, "credential_source": resolved.source}
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return {"credential_configured": False, "credential_source": "unavailable"}


def is_sensitive_field(key: object) -> bool:
    normalized = re.sub(r"[-\s]+", "_", str(key or "").strip().lower())
    return bool(_SENSITIVE_FIELD.search(normalized)) or normalized.endswith(("_token", "_private_key", "_secret_key")) or normalized in {
        "access_token",
        "refresh_token",
        "azure_ad_token",
        "hf_token",
        "auth_token",
        "bearer_token",
        "token",
        "private_key",
        "secret_key",
        "subscription_key",
        "authorization",
        "proxy_authorization",
    }


def contains_inline_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(is_sensitive_field(key) or contains_inline_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_inline_secret(item) for item in value)
    return False


def redact_inline_secrets(value: Any) -> Any:
    """Return an API-safe copy with secret values removed but references retained."""

    if isinstance(value, dict):
        return {
            key: redact_inline_secrets(item)
            for key, item in value.items()
            if not is_sensitive_field(key)
        }
    if isinstance(value, list):
        return [redact_inline_secrets(item) for item in value]
    return copy.deepcopy(value)


def validate_provider_options(options: dict[str, Any] | None) -> None:
    if contains_inline_secret(options or {}):
        raise ValueError("Provider secrets must be saved in the API key field, not advanced options.")


def prepare_tts_settings_for_storage(
    session: Session,
    value: Any,
    previous_value: Any,
) -> dict[str, Any]:
    """Move submitted inline TTS keys to the credential table and retain only references."""

    if not isinstance(value, dict):
        raise ValueError("TTS service settings must be an object.")
    prepared = copy.deepcopy(value)
    previous = previous_value if isinstance(previous_value, dict) else {}
    previous_records = {
        str(item.get("id") or item.get("name") or item.get("provider") or "").strip().lower().replace("-", "_"): item
        for item in previous.get("provider_configs", [])
        if isinstance(item, dict)
    }
    records = prepared.get("provider_configs")
    if not isinstance(records, list):
        return prepared
    current_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        service_id = str(record.get("id") or record.get("name") or record.get("provider") or "").strip()
        if not service_id:
            raise ValueError("Every TTS provider configuration requires an ID.")
        normalized_id = service_id.lower().replace("-", "_")
        current_ids.add(normalized_id)
        previous_record = previous_records.get(normalized_id, {})
        record.pop("credential_configured", None)
        record.pop("credential_source", None)
        submitted_key = str(record.pop("api_key", "") or "").strip()
        clear_key = bool(record.pop("clear_api_key", False))
        existing_reference = str(record.get("secret_ref") or previous_record.get("secret_ref") or "").strip()
        if submitted_key:
            key = tts_credential_key(normalized_id)
            previous_key = reference_key(previous_record.get("secret_ref"))
            if previous_key and previous_key != key:
                delete_credential(session, previous_key)
            upsert_credential(session, key, f"{record.get('name') or service_id} API key", submitted_key)
            record["secret_ref"] = database_reference(key)
        elif clear_key:
            key = reference_key(existing_reference) or tts_credential_key(normalized_id)
            delete_credential(session, key)
            record.pop("secret_ref", None)
        elif existing_reference:
            record["secret_ref"] = existing_reference
            previous_reference = str(previous_record.get("secret_ref") or "").strip()
            if previous_reference != existing_reference:
                previous_key = reference_key(previous_reference)
                if previous_key:
                    delete_credential(session, previous_key)
    for normalized_id, previous_record in previous_records.items():
        if normalized_id in current_ids:
            continue
        previous_reference = str(previous_record.get("secret_ref") or "").strip()
        delete_credential(session, reference_key(previous_reference) or tts_credential_key(normalized_id))
    if contains_inline_secret(prepared):
        raise ValueError("TTS credentials must be saved in the API key field.")
    return prepared


def hydrate_tts_settings(database: Database, paths: DataPaths, settings: dict[str, Any]) -> dict[str, Any]:
    """Inject only the selected TTS credential into a transient runtime settings copy."""

    from pandrator.logic import tts_handler

    hydrated = copy.deepcopy(settings or {})
    selected_value = str(hydrated.get("service") or hydrated.get("tts_service") or "XTTS")
    if selected_value.strip().lower() in {"openai compatible", "openai-compatible", "custom"}:
        selected_value = str(hydrated.get("openai_audio_endpoint") or selected_value)
    selected = tts_handler.get_service_config(hydrated, selected_value)
    if selected is None:
        return hydrated
    service_id = str(selected.get("id") or selected_value).strip().lower().replace("-", "_")
    fallback_env = str(selected.get("api_key_env") or TTS_SERVICE_ENVS.get(service_id, ""))
    resolved = resolve_secret_reference(
        database,
        paths,
        selected.get("secret_ref"),
        fallback_environment_variable=fallback_env,
    )
    records = [dict(item) for item in hydrated.get("provider_configs", []) if isinstance(item, dict)]
    record = next(
        (
            item
            for item in records
            if str(item.get("id") or item.get("name") or "").strip().lower().replace("-", "_") == service_id
        ),
        None,
    )
    if record is None:
        record = {"id": service_id}
        records.append(record)
    if resolved.value:
        record["api_key"] = resolved.value
        record["api_key_env"] = ""
    elif resolved.environment_variable:
        record["api_key_env"] = resolved.environment_variable
    hydrated["provider_configs"] = records
    return hydrated


def auxiliary_profiles(database: Database, paths: DataPaths) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for profile in AUXILIARY_CREDENTIALS:
        reference = database_reference(auxiliary_credential_key(profile["id"]))
        status = credential_status(
            database,
            paths,
            reference,
            fallback_environment_variable=profile["environment_variable"],
        )
        result.append({**profile, **status})
    return result
