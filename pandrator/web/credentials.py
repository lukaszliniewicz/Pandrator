"""Write-only credential references and storage for Pandrator integrations."""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .database import Database
from .managed_services import (
    binding_for_provider,
    configured_tts_provider_ids,
    effective_tts_connection_mode,
)
from .models import AppSetting, Provider, StoredCredential, utcnow

DATABASE_REFERENCE_PREFIX = "db:"
ENVIRONMENT_REFERENCE_PREFIX = "env:"
KEYRING_REFERENCE_PREFIX = "keyring:"
SECRET_FILE_REFERENCE_PREFIX = "file-path:"
LEGACY_FILE_REFERENCE_PREFIX = "file:"
KEYRING_SERVICE_NAME = "Pandrator"
AUXILIARY_REFERENCES_SETTING = "credentials.auxiliary_refs"
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
    "elevenlabs": "ELEVENLABS_API_KEY",
}
SHARED_PROVIDER_CREDENTIALS = {"openai", "gemini", "vertex_ai"}
AUXILIARY_CREDENTIALS: tuple[dict[str, str], ...] = (
    {
        "id": "jina",
        "label": "Jina Reader",
        "description": "Optional web search and page extraction for evidence-backed correction and translation.",
        "environment_variable": "JINA_API_KEY",
    },
    {
        "id": "deepl",
        "label": "DeepL",
        "description": "Used when subtitle translation selects the DeepL backend.",
        "environment_variable": "DEEPL_API_KEY",
    },
)

_SENSITIVE_FIELD = re.compile(r"(^|_)(api_key|password|secret|credential)s?$", re.IGNORECASE)
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEXT_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    value: str = field(default="", repr=False)
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


@dataclass(frozen=True, slots=True)
class CredentialConfiguration:
    reference: str | None
    backend: str
    previous_credential_retained: bool = False


def normalize_credential_id(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-")
    if not normalized:
        raise ValueError("Credential identifiers must include letters or numbers.")
    return normalized


def provider_credential_key(provider_id: object) -> str:
    return f"llm:{normalize_credential_id(provider_id)}"


def tts_credential_key(service_id: object) -> str:
    return f"tts:{normalize_credential_id(service_id).replace('-', '_')}"


def shared_provider_credential_key(provider_id: object) -> str:
    normalized = normalize_credential_id(provider_id).replace("-", "_")
    return f"shared:{normalized}"


def llm_provider_credential_key(
    provider_key: object,
    provider_id: object,
    options: dict[str, Any] | None = None,
) -> str:
    normalized = normalize_credential_id(provider_key).replace("-", "_")
    metadata = options or {}
    profile_id = str(metadata.get("profile_id") or "").strip().lower()
    is_custom = bool(metadata.get("is_custom") or profile_id in {"custom-openai", "lm-studio", "ollama"})
    if normalized in SHARED_PROVIDER_CREDENTIALS and not is_custom:
        return shared_provider_credential_key(normalized)
    return provider_credential_key(provider_id)


def tts_service_credential_key(service_id: object) -> str:
    normalized = normalize_credential_id(service_id).replace("-", "_")
    if normalized in SHARED_PROVIDER_CREDENTIALS:
        return shared_provider_credential_key(normalized)
    return tts_credential_key(normalized)


def auxiliary_credential_key(credential_id: object) -> str:
    return f"aux:{normalize_credential_id(credential_id)}"


def database_reference(key: str) -> str:
    return f"{DATABASE_REFERENCE_PREFIX}{key}"


def reference_key(reference: object) -> str:
    value = str(reference or "").strip()
    return value[len(DATABASE_REFERENCE_PREFIX) :].strip() if value.startswith(DATABASE_REFERENCE_PREFIX) else ""


def credential_backend(reference: object) -> str:
    value = str(reference or "").strip()
    if value.startswith(DATABASE_REFERENCE_PREFIX) or not value:
        return "database"
    if value.startswith(ENVIRONMENT_REFERENCE_PREFIX):
        return "environment"
    if value.startswith(KEYRING_REFERENCE_PREFIX):
        return "keyring"
    if value.startswith((SECRET_FILE_REFERENCE_PREFIX, LEGACY_FILE_REFERENCE_PREFIX)):
        return "file"
    return "unavailable"


def credential_reference_input(reference: object) -> str:
    """Return the non-secret locator portion used by the structured UI."""

    value = str(reference or "").strip()
    if value.startswith(ENVIRONMENT_REFERENCE_PREFIX):
        return value[len(ENVIRONMENT_REFERENCE_PREFIX) :].strip()
    if value.startswith(SECRET_FILE_REFERENCE_PREFIX):
        return value[len(SECRET_FILE_REFERENCE_PREFIX) :].strip()
    if value.startswith(LEGACY_FILE_REFERENCE_PREFIX):
        return value[len(LEGACY_FILE_REFERENCE_PREFIX) :].strip()
    return ""


def _load_keyring():
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "The optional keyring package is not installed. Install Pandrator's credential-stores extra first."
        ) from error
    backend = keyring.get_keyring()
    try:
        priority = float(getattr(backend, "priority", 0))
    except (TypeError, ValueError):
        priority = 0
    if priority <= 0:
        raise RuntimeError(
            "No usable operating-system credential store is available for this Pandrator process."
        )
    return keyring, backend


def keyring_reference(key: str) -> str:
    return f"{KEYRING_REFERENCE_PREFIX}{KEYRING_SERVICE_NAME}/{key}"


def environment_reference(variable: object) -> str:
    value = str(variable or "").strip()
    if not _ENVIRONMENT_VARIABLE.fullmatch(value):
        raise ValueError(
            "Environment variable names must start with a letter or underscore and contain only letters, numbers, and underscores."
        )
    return f"{ENVIRONMENT_REFERENCE_PREFIX}{value}"


def secret_file_reference(path: object) -> str:
    value = str(path or "").strip()
    if not value:
        raise ValueError("Choose an absolute secret-file path.")
    target = Path(value).expanduser()
    if not target.is_absolute():
        raise ValueError("Secret-file paths must be absolute.")
    return f"{SECRET_FILE_REFERENCE_PREFIX}{target.resolve(strict=False)}"


def _read_secret_file(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError("The configured secret file does not exist or is not a regular file.")
    metadata = path.stat()
    if metadata.st_size > 1024 * 1024:
        raise ValueError("Secret files must be no larger than 1 MiB.")
    if os.name != "nt":
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(
                "The secret file must only be accessible by its owner (for example, chmod 600)."
            )
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("The secret file is empty.")
    if "\x00" in value:
        raise ValueError("The secret file must contain UTF-8 text without NUL bytes.")
    return value


def credential_backend_profiles() -> list[dict[str, Any]]:
    try:
        _keyring, backend = _load_keyring()
        keyring_available = True
        keyring_detail = type(backend).__name__
    except RuntimeError as error:
        keyring_available = False
        keyring_detail = str(error)
    system = platform.system() or "this operating system"
    return [
        {
            "id": "database",
            "label": "Pandrator database",
            "available": True,
            "default": True,
            "requires_secret": True,
            "requires_reference": False,
            "description": (
                "The easiest option. Enter the value here; it stays write-only in Pandrator's local database."
            ),
            "guidance": (
                "Database backups include this credential. Protect the data directory with normal account permissions "
                "and use full-disk encryption when local data theft is a concern."
            ),
        },
        {
            "id": "keyring",
            "label": "Operating-system credential store",
            "available": keyring_available,
            "default": False,
            "requires_secret": True,
            "requires_reference": False,
            "description": (
                "Store the value in Windows Credential Manager, macOS Keychain, or a Linux Secret Service backend."
            ),
            "guidance": (
                f"Detected backend on {system}: {keyring_detail}. Pandrator stores only its service/user reference."
            ),
        },
        {
            "id": "environment",
            "label": "Environment variable",
            "available": True,
            "default": False,
            "requires_secret": False,
            "requires_reference": True,
            "description": "Store only an environment-variable name; Pandrator reads its value at runtime.",
            "guidance": (
                "Set the variable for the account that launches Pandrator. Restart Pandrator after changing a "
                "persistent user or service environment."
            ),
        },
        {
            "id": "file",
            "label": "Secret file",
            "available": True,
            "default": False,
            "requires_secret": False,
            "requires_reference": True,
            "description": "Store only an absolute path to a UTF-8 file containing the secret value.",
            "guidance": (
                "Use an owner-only file (chmod 600 on macOS/Linux) or a managed/container secret mount. "
                "Pandrator validates readability without returning the contents."
            ),
        },
    ]


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
    if value.startswith(ENVIRONMENT_REFERENCE_PREFIX):
        environment_variable = value[len(ENVIRONMENT_REFERENCE_PREFIX) :].strip()
        if environment_variable:
            environment_reference(environment_variable)
        return ResolvedCredential(
            environment_variable=environment_variable,
            source="environment" if environment_variable else "none",
        )
    if value.startswith(KEYRING_REFERENCE_PREFIX):
        target = value[len(KEYRING_REFERENCE_PREFIX) :].strip()
        service, separator, username = target.partition("/")
        if not separator or not service or not username:
            raise ValueError("Keyring secret references must use keyring:<service>/<username>.")
        try:
            keyring, _backend = _load_keyring()
            secret_value = str(keyring.get_password(service, username) or "")
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError("The operating-system credential store could not be read.") from error
        return ResolvedCredential(value=secret_value, source="keyring")
    if value.startswith(SECRET_FILE_REFERENCE_PREFIX):
        configured_path = value[len(SECRET_FILE_REFERENCE_PREFIX) :].strip()
        if not configured_path:
            raise ValueError("Secret-file references must include an absolute path.")
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            raise ValueError("Secret-file references must use an absolute path.")
        return ResolvedCredential(value=_read_secret_file(path), source="file")
    if value.startswith(LEGACY_FILE_REFERENCE_PREFIX):
        key = value[len(LEGACY_FILE_REFERENCE_PREFIX) :].strip()
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
    raise ValueError("Secret references must use db:, env:, keyring:, file-path:, or legacy file:.")


def _delete_keyring_reference(reference: str) -> bool:
    target = reference[len(KEYRING_REFERENCE_PREFIX) :].strip()
    service, separator, username = target.partition("/")
    if not separator or not service or not username:
        return False
    keyring = None
    try:
        keyring, _backend = _load_keyring()
        keyring.delete_password(service, username)
    except RuntimeError:
        raise
    except Exception as error:
        # Missing entries are already in the desired state.  Backends expose
        # different exception types, so confirm absence before failing.
        try:
            if keyring is not None and not keyring.get_password(service, username):
                return True
        except Exception:
            pass
        raise RuntimeError("The old operating-system credential could not be removed.") from error
    return True


def delete_managed_reference(
    session: Session,
    reference: str,
    *,
    preserve_shared: bool = True,
) -> bool:
    """Delete an app-controlled value; environment variables/files remain external."""

    if reference.startswith(DATABASE_REFERENCE_PREFIX):
        key = reference_key(reference)
        if not key or (preserve_shared and key.startswith("shared:")):
            return False
        return delete_credential(session, key)
    if reference.startswith(KEYRING_REFERENCE_PREFIX):
        target = reference[len(KEYRING_REFERENCE_PREFIX) :].strip()
        _service, _separator, username = target.partition("/")
        if preserve_shared and username.startswith("shared:"):
            return False
        return _delete_keyring_reference(reference)
    return False


def configure_credential_reference(
    session: Session,
    database: Database,
    paths: DataPaths,
    *,
    key: str,
    label: str,
    current_reference: object = "",
    backend: object = "database",
    locator: object = "",
    secret_value: object = "",
    delete_previous: bool = False,
) -> CredentialConfiguration:
    """Validate and configure a credential backend before changing its reference.

    Database remains the default.  External backends are verified first, and an
    old app-controlled value is deleted only when the caller explicitly asks.
    Shared credentials are retained because another LLM/TTS connection may
    still use them.
    """

    selected = str(backend or "database").strip().lower()
    if selected not in {"database", "environment", "keyring", "file"}:
        raise ValueError("Credential storage must be database, environment, keyring, or file.")
    current = str(current_reference or "").strip()
    submitted = str(secret_value or "").strip()
    desired: str | None

    if selected == "database":
        desired = database_reference(key)
        if submitted:
            upsert_credential(session, key, label, submitted)
        elif current == desired:
            # Keeping an already configured database reference requires no
            # round-trip of its write-only value.
            pass
        else:
            # Keyless local endpoints are valid.  Do not create a dangling
            # database reference when the user supplied no value.
            desired = None
    elif selected == "keyring":
        desired = keyring_reference(key)
        if submitted:
            target = desired[len(KEYRING_REFERENCE_PREFIX) :]
            service, _separator, username = target.partition("/")
            try:
                keyring, _backend = _load_keyring()
                keyring.set_password(service, username, submitted)
                stored = str(keyring.get_password(service, username) or "")
            except RuntimeError:
                raise
            except Exception as error:
                raise RuntimeError("The operating-system credential store could not save the value.") from error
            if not stored or not secrets.compare_digest(stored, submitted):
                raise RuntimeError("The operating-system credential store did not verify the saved value.")
        elif current != desired:
            raise ValueError("Enter the credential value before moving it to the operating-system store.")
        resolved = resolve_secret_reference(database, paths, desired)
        if not resolved.configured:
            raise ValueError("The operating-system credential store does not contain this credential.")
    elif selected == "environment":
        if submitted:
            raise ValueError("Do not send a credential value when using an environment variable.")
        desired = environment_reference(locator)
        resolved = resolve_secret_reference(database, paths, desired)
        if not resolved.configured:
            raise ValueError(
                "That environment variable is not available to this Pandrator process. "
                "Set it for the launching account, restart Pandrator, and try again."
            )
    else:
        if submitted:
            raise ValueError("Do not send a credential value when using a secret file.")
        desired = secret_file_reference(locator)
        resolved = resolve_secret_reference(database, paths, desired)
        if not resolved.configured:
            raise ValueError("The secret file does not contain a usable value.")

    previous_retained = bool(current and current != str(desired or ""))
    if delete_previous and previous_retained:
        removed = delete_managed_reference(session, current)
        previous_retained = not removed
    return CredentialConfiguration(
        reference=desired,
        backend=selected,
        previous_credential_retained=previous_retained,
    )


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


def resolve_provider_credential(
    database: Database,
    paths: DataPaths,
    provider_id: object,
    reference: object,
    *,
    fallback_environment_variable: str = "",
    shared: bool = True,
) -> ResolvedCredential:
    """Resolve a shared cloud credential before provider-specific fallbacks."""

    normalized = normalize_credential_id(provider_id).replace("-", "_")
    if shared and normalized in SHARED_PROVIDER_CREDENTIALS:
        resolved = resolve_secret_reference(
            database,
            paths,
            database_reference(shared_provider_credential_key(normalized)),
        )
        if resolved.configured:
            return resolved
    return resolve_secret_reference(
        database,
        paths,
        reference,
        fallback_environment_variable=fallback_environment_variable,
    )


def provider_credential_status(
    database: Database,
    paths: DataPaths,
    provider_id: object,
    reference: object,
    *,
    fallback_environment_variable: str = "",
    shared: bool = True,
) -> dict[str, Any]:
    try:
        resolved = resolve_provider_credential(
            database,
            paths,
            provider_id,
            reference,
            fallback_environment_variable=fallback_environment_variable,
            shared=shared,
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


def validate_vertex_service_account_json(value: object) -> dict[str, Any]:
    """Validate pasted Vertex service-account JSON without exposing its contents."""

    try:
        payload = json.loads(str(value or "").strip())
    except json.JSONDecodeError as error:
        raise ValueError("Vertex credentials must be valid JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError("Vertex credentials must be a JSON object.")
    if payload.get("type") != "service_account":
        raise ValueError(
            "Paste a Google service-account key JSON object. For user or federated credentials, configure Application Default Credentials instead."
        )
    required = ("project_id", "client_email", "private_key", "token_uri")
    if any(not str(payload.get(key) or "").strip() for key in required):
        raise ValueError(
            "The service-account JSON must include project_id, client_email, private_key, and token_uri."
        )
    return payload


def prepare_tts_settings_for_storage(
    session: Session,
    database: Database,
    paths: DataPaths,
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
        connection_mode = str(
            record.get("connection_mode") or "external"
        ).strip().lower()
        if connection_mode not in {"external", "managed_local"}:
            raise ValueError(
                "TTS connection mode must be 'external' or 'managed_local'."
            )
        record["connection_mode"] = connection_mode
        if connection_mode == "managed_local":
            binding = binding_for_provider(normalized_id)
            if binding is None:
                raise ValueError(
                    f"{service_id} cannot be bound to a manager-owned service."
                )
            requested_service = str(
                record.get("managed_service_id") or binding.service_id
            ).strip()
            if requested_service != binding.service_id:
                raise ValueError(
                    f"{service_id} must use managed service {binding.service_id}."
                )
            record["managed_service_id"] = binding.service_id
        else:
            record.pop("managed_service_id", None)
        record.pop("credential_configured", None)
        record.pop("credential_source", None)
        record.pop("previous_credential_retained", None)
        submitted_key = str(record.pop("api_key", "") or "").strip()
        clear_key = bool(record.pop("clear_api_key", False))
        requested_backend = record.pop("credential_backend", None)
        requested_locator = record.pop("credential_reference", "")
        delete_previous = bool(record.pop("delete_previous_credential", False))
        existing_reference = str(record.get("secret_ref") or previous_record.get("secret_ref") or "").strip()
        if submitted_key and normalized_id == "vertex_ai":
            validate_vertex_service_account_json(submitted_key)
        if requested_backend is not None or submitted_key:
            key = tts_service_credential_key(normalized_id)
            configured = configure_credential_reference(
                session,
                database,
                paths,
                key=key,
                label=f"{record.get('name') or service_id} API key",
                current_reference=str(previous_record.get("secret_ref") or existing_reference),
                backend=requested_backend or "database",
                locator=requested_locator,
                secret_value=submitted_key,
                delete_previous=delete_previous,
            )
            if configured.reference:
                record["secret_ref"] = configured.reference
            else:
                record.pop("secret_ref", None)
        elif clear_key:
            current_reference = existing_reference or database_reference(
                tts_service_credential_key(normalized_id)
            )
            delete_managed_reference(
                session,
                current_reference,
                preserve_shared=False,
            )
            record.pop("secret_ref", None)
        elif existing_reference:
            record["secret_ref"] = existing_reference
    for normalized_id, previous_record in previous_records.items():
        if normalized_id in current_ids:
            continue
        previous_reference = str(previous_record.get("secret_ref") or "").strip()
        delete_managed_reference(
            session,
            previous_reference
            or database_reference(tts_service_credential_key(normalized_id)),
        )
    if contains_inline_secret(prepared):
        raise ValueError("TTS credentials must be saved in the API key field.")
    return prepared


def hydrate_tts_settings(
    database: Database,
    paths: DataPaths,
    settings: dict[str, Any],
    *,
    manager_bridge: Any | None = None,
) -> dict[str, Any]:
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
    configured_provider_ids = configured_tts_provider_ids(hydrated)
    fallback_env = str(selected.get("api_key_env") or TTS_SERVICE_ENVS.get(service_id, ""))
    resolved = resolve_provider_credential(
        database,
        paths,
        service_id,
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
    from .manager_proxy import LocalManagerProxy, ManagerProxyError

    bridge = manager_bridge or LocalManagerProxy()
    connection_mode = effective_tts_connection_mode(
        selected,
        configured_provider_ids=configured_provider_ids,
        manager_configured=bool(
            getattr(
                bridge,
                "configured",
                manager_bridge is not None,
            )
        ),
    )
    if connection_mode == "managed_local":

        binding = binding_for_provider(service_id)
        if binding is None:
            raise ValueError(
                f"{selected_value} has no qualified local manager binding."
            )
        requested_service = str(
            selected.get("managed_service_id") or binding.service_id
        ).strip()
        if requested_service != binding.service_id:
            raise ValueError(
                f"{selected_value} has an invalid managed-service binding."
            )
        try:
            managed = bridge.managed_service(binding.service_id)
        except ManagerProxyError as error:
            raise RuntimeError(
                f"The managed local {selected_value} service is unavailable: {error}"
            ) from error
        endpoint = str(managed.get("endpoint") or "").strip().rstrip("/")
        health = str((managed.get("health") or {}).get("state") or "stopped")
        if not endpoint:
            raise RuntimeError(
                f"The managed local {selected_value} service has no endpoint."
            )
        if health != "healthy":
            raise RuntimeError(
                f"The managed local {selected_value} service is {health}. "
                "Start it in Providers & Services before generating audio."
            )
        hydrated[binding.settings_url_key] = endpoint
        if str(hydrated.get("preview_service_id") or "") == service_id:
            hydrated["preview_api_base"] = endpoint
        record["api_base"] = endpoint
        record["connection_mode"] = "managed_local"
        record["managed_service_id"] = binding.service_id
    return hydrated


def auxiliary_reference_map(session: Session) -> dict[str, str]:
    record = session.get(AppSetting, AUXILIARY_REFERENCES_SETTING)
    if record is None or not isinstance(record.value_json, dict):
        return {}
    return {
        normalize_credential_id(key): str(value or "").strip()
        for key, value in record.value_json.items()
        if str(value or "").strip()
    }


def set_auxiliary_reference(session: Session, credential_id: object, reference: str | None) -> None:
    normalized = normalize_credential_id(credential_id)
    record = session.get(AppSetting, AUXILIARY_REFERENCES_SETTING)
    values = auxiliary_reference_map(session)
    if reference:
        values[normalized] = str(reference).strip()
    else:
        values.pop(normalized, None)
    if record is None:
        session.add(AppSetting(key=AUXILIARY_REFERENCES_SETTING, value_json=values))
    else:
        record.value_json = values
        record.revision += 1
        record.updated_at = utcnow()


def auxiliary_profiles(database: Database, paths: DataPaths) -> list[dict[str, Any]]:
    with database.session() as session:
        references = auxiliary_reference_map(session)
    result: list[dict[str, Any]] = []
    for profile in AUXILIARY_CREDENTIALS:
        reference = references.get(
            profile["id"],
            database_reference(auxiliary_credential_key(profile["id"])),
        )
        status = credential_status(
            database,
            paths,
            reference,
            fallback_environment_variable=profile["environment_variable"],
        )
        result.append(
            {
                **profile,
                **status,
                "secret_ref": reference,
                "credential_backend": credential_backend(reference),
                "credential_reference": credential_reference_input(reference),
            }
        )
    return result


def _secret_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            references.update(_secret_references(item))
    elif isinstance(value, list):
        for item in value:
            references.update(_secret_references(item))
    elif isinstance(value, str) and value.startswith(
        (
            DATABASE_REFERENCE_PREFIX,
            ENVIRONMENT_REFERENCE_PREFIX,
            KEYRING_REFERENCE_PREFIX,
            SECRET_FILE_REFERENCE_PREFIX,
            LEGACY_FILE_REFERENCE_PREFIX,
        )
    ):
        references.add(value.strip())
    return references


def redact_secret_text(value: object, secret_values: list[str] | tuple[str, ...] = ()) -> str:
    """Remove known values and common inline assignments from diagnostic text."""

    text = str(value or "")
    for secret_value in sorted(
        {str(item) for item in secret_values if len(str(item)) >= 4},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret_value, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]", text)
    text = _TEXT_SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    return text


class SecretRedactor:
    """Short-lived cache of configured values used to sanitize logs and failures."""

    def __init__(self, database: Database, paths: DataPaths | None = None, *, ttl_seconds: float = 30.0):
        self.database = database
        self.paths = paths or DataPaths(database.path.parent)
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._expires_at = 0.0
        self._values: tuple[str, ...] = ()
        self._lock = threading.Lock()

    def _load_values(self) -> tuple[str, ...]:
        values: set[str] = set()
        references: set[str] = set()
        with self.database.session() as session:
            values.update(
                str(item or "")
                for item in session.scalars(select(StoredCredential.secret_value)).all()
            )
            references.update(
                str(item or "").strip()
                for item in session.scalars(select(Provider.secret_ref)).all()
                if str(item or "").strip()
            )
            for setting in session.scalars(select(AppSetting.value_json)).all():
                references.update(_secret_references(setting))
        environment_names = set(DEFAULT_PROVIDER_ENVS.values()) | set(TTS_SERVICE_ENVS.values())
        environment_names.update(
            str(profile["environment_variable"])
            for profile in AUXILIARY_CREDENTIALS
        )
        for reference in references:
            if reference.startswith(ENVIRONMENT_REFERENCE_PREFIX):
                environment_names.add(
                    reference[len(ENVIRONMENT_REFERENCE_PREFIX) :].strip()
                )
            if reference.startswith(DATABASE_REFERENCE_PREFIX):
                continue
            try:
                resolved = resolve_secret_reference(self.database, self.paths, reference)
                values.add(resolved.resolved_value())
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                continue
        values.update(os.environ.get(name, "") for name in environment_names)
        return tuple(sorted({item for item in values if len(item) >= 4}, key=len, reverse=True))

    def values(self) -> tuple[str, ...]:
        now = time.monotonic()
        if now < self._expires_at:
            return self._values
        with self._lock:
            if now >= self._expires_at:
                try:
                    self._values = self._load_values()
                except Exception:
                    # Redaction is used while reporting failures, including
                    # database and credential-backend failures. It must never
                    # hide the original error by raising another one.
                    self._values = ()
                self._expires_at = time.monotonic() + self.ttl_seconds
        return self._values

    def redact(self, value: object) -> str:
        return redact_secret_text(value, self.values())

    def redact_value(self, value: Any) -> Any:
        """Return a diagnostic/export-safe copy while preserving JSON types."""

        structural_copy = redact_inline_secrets(value)

        def redact(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: redact(child) for key, child in item.items()}
            if isinstance(item, list):
                return [redact(child) for child in item]
            if isinstance(item, tuple):
                return [redact(child) for child in item]
            if isinstance(item, str):
                return self.redact(item)
            return item

        return redact(structural_copy)
