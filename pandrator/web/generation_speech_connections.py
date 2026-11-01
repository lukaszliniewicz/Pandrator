"""Fence an authorized nonlegacy generation run to its approved speech connection."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, NoReturn, TypeGuard

from sqlalchemy.orm import Session

from pandrator.logic import tts_handler
from pandrator.logic.tts_service_identity import normalize_service_id

from . import credentials
from .credential_identity import CredentialNamespace, credential_namespace
from .tts_catalogue_service import TtsCatalogueService

_CHOOSE = "Choose a speech connection configured for this workspace."
_CHANGED = (
    "The speech connection or credential changed. "
    "Review the workspace connection and submit a new run."
)
_TRANSPORT_FIELDS = (
    "id",
    "provider",
    "adapter",
    "api_base",
    "profile_id",
    "speech_path",
    "models_path",
    "voices_path",
    "auth_mode",
    "vertex_project",
    "vertex_location",
    "connection_mode",
    "managed_service_id",
    "direct_http",
    "credential_required",
    "request_fields",
    "request_defaults",
    "secret_ref",
    "api_key_env",
    "settings",
)
_CONNECTION_PROFILE_FIELDS = frozenset(
    (*_TRANSPORT_FIELDS, "api_key", "default_model", "is_custom")
)
_RAW_MODEL_ADAPTERS = frozenset({"audio_cpp", "generic_json", "azure_speech", "elevenlabs_native"})
_CUSTOM_ALIASES = frozenset({"custom", "openai_compatible"})
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class GenerationSpeechConnection:
    workspace_id: str
    service_id: str
    model: str
    connection_fingerprint: str
    credential_fingerprint: str


def _choose() -> NoReturn:
    raise ValueError(_CHOOSE) from None


def _changed() -> NoReturn:
    raise ValueError(_CHANGED) from None


def _identifier(value: object) -> TypeGuard[str]:
    return type(value) is str and bool(value) and value == value.strip()


def _json_value(value: object) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    return False


def _connection_fingerprint(profile: dict[str, Any]) -> str:
    identity = {key: profile.get(key) for key in _TRANSPORT_FIELDS}
    try:
        if not _json_value(identity):
            _choose()
        value = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _choose()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _namespace(session: Session, workspace_id: str) -> CredentialNamespace:
    if not _identifier(workspace_id):
        _choose()
    try:
        namespace = credential_namespace(session, workspace_id)
    except (KeyError, TypeError, ValueError, RuntimeError):
        _choose()
    if namespace.legacy_host_access:
        _choose()
    return namespace


def _profile(
    settings: dict[str, Any],
    service_id: str,
    *,
    _service_config_cache: dict | None = None,
) -> dict[str, Any]:
    if settings.get("api_key") not in (None, ""):
        _choose()
    try:
        if _service_config_cache is None:
            profile = tts_handler.get_service_config(settings, service_id)
        else:
            profile = tts_handler.get_service_config(
                settings,
                service_id,
                _cache=_service_config_cache,
                _fields=_CONNECTION_PROFILE_FIELDS,
            )
    except (TypeError, ValueError, AttributeError, RecursionError):
        _choose()
    if profile is None or profile.get("api_key") not in (None, ""):
        _choose()
    nested_settings = profile.get("settings")
    if type(nested_settings) is dict and nested_settings.get("api_key") not in (None, ""):
        _choose()
    return profile


def _actual_model(settings: dict[str, Any], profile: dict[str, Any]) -> str:
    raw_model = settings.get("xtts_model") or settings.get("model") or profile.get("default_model")
    if type(raw_model) is not str or not raw_model.strip():
        _choose()
    raw_model = raw_model.strip()
    adapter = normalize_service_id(profile.get("adapter"))
    provider = str(profile.get("provider") or profile.get("id") or "")
    if adapter not in _RAW_MODEL_ADAPTERS and (
        (profile.get("is_custom") is True and adapter == "openai_compatible")
        or provider in {"gemini", "vertex_ai", "openai"}
    ):
        raw_model = tts_handler._normalize_model_for_provider(raw_model, provider)
    if not raw_model:
        _choose()
    return raw_model


def _approved_profile(
    session: Session,
    catalogue: TtsCatalogueService,
    service_id: str,
    model: str,
    workspace_id: str,
) -> dict[str, Any]:
    try:
        settings = catalogue.preview_settings(
            service_id,
            model=model,
            voice=None,
            language=None,
            workspace_id=workspace_id,
            db_session=session,
        )
    except (TypeError, ValueError, AttributeError, RecursionError):
        _choose()
    if type(settings) is not dict:
        _choose()
    service_config_cache = catalogue._connection_lookup_cache()
    if service_config_cache is None:
        profile = _profile(settings, service_id)
    else:
        profile = _profile(
            settings,
            service_id,
            _service_config_cache=service_config_cache,
        )
    if normalize_service_id(profile.get("id")) != service_id:
        _choose()
    return profile


def _credential_fingerprint(
    session: Session,
    catalogue: TtsCatalogueService,
    profile: dict[str, Any],
    service_id: str,
    namespace: CredentialNamespace,
) -> str:
    reference = profile.get("secret_ref")
    if reference is not None and type(reference) is not str:
        _choose()
    if credentials.credential_backend(reference) != "database":
        _choose()
    try:
        keys = credentials._credential_database_keys(service_id, reference, True)
        persisted = credentials._project_database_credentials(session, keys, namespace)
        resolved = credentials._resolve_provider_credential_in_namespace(
            session,
            catalogue.paths,
            service_id,
            reference,
            namespace=namespace,
            fallback_environment_variable="",
            preloaded_database_credentials=persisted,
        )
    except (TypeError, ValueError, RuntimeError):
        _choose()
    # Nonlegacy DB resolution must never require a later environment lookup.
    if resolved.environment_variable or type(resolved.value) is not str:
        _choose()
    return hashlib.sha256(resolved.value.encode("utf-8")).hexdigest()


def capture_generation_speech_connection(
    session: Session,
    catalogue: TtsCatalogueService,
    settings: dict[str, Any],
    *,
    workspace_id: str,
) -> GenerationSpeechConnection:
    """Capture connection equality after the caller proves execution authority."""
    with session.no_autoflush:
        namespace = _namespace(session, workspace_id)
        if type(settings) is not dict:
            _choose()
        service = settings.get("service") or settings.get("tts_service") or "XTTS"
        if not _identifier(service):
            _choose()
        if normalize_service_id(service) in _CUSTOM_ALIASES:
            service = settings.get("openai_audio_endpoint")
            if not _identifier(service):
                _choose()
        service_config_cache = catalogue._connection_lookup_cache()
        if service_config_cache is None:
            frozen = _profile(settings, service)
        else:
            frozen = _profile(
                settings,
                service,
                _service_config_cache=service_config_cache,
            )
        service_id = normalize_service_id(frozen.get("id"))
        if not service_id:
            _choose()
        actual = _actual_model(settings, frozen)
        current = _approved_profile(session, catalogue, service_id, actual, workspace_id)
        frozen_fingerprint = _connection_fingerprint(frozen)
        current_fingerprint = _connection_fingerprint(current)
        if frozen_fingerprint != current_fingerprint:
            _changed()
        credential_fingerprint = _credential_fingerprint(
            session,
            catalogue,
            current,
            service_id,
            namespace,
        )
        return GenerationSpeechConnection(
            workspace_id,
            service_id,
            actual,
            current_fingerprint,
            credential_fingerprint,
        )


def require_generation_speech_connection(
    session: Session,
    catalogue: TtsCatalogueService,
    expected: GenerationSpeechConnection,
) -> None:
    """Require the same current approved model, transport and persisted credential."""
    if (
        type(expected) is not GenerationSpeechConnection
        or not _identifier(expected.workspace_id)
        or not _identifier(expected.service_id)
        or normalize_service_id(expected.service_id) != expected.service_id
        or not _identifier(expected.model)
        or type(expected.connection_fingerprint) is not str
        or _DIGEST.fullmatch(expected.connection_fingerprint) is None
        or type(expected.credential_fingerprint) is not str
        or _DIGEST.fullmatch(expected.credential_fingerprint) is None
    ):
        _choose()
    with session.no_autoflush:
        namespace = _namespace(session, expected.workspace_id)
        current = _approved_profile(
            session,
            catalogue,
            expected.service_id,
            expected.model,
            expected.workspace_id,
        )
        if _connection_fingerprint(current) != expected.connection_fingerprint:
            _changed()
        if (
            _credential_fingerprint(
                session,
                catalogue,
                current,
                expected.service_id,
                namespace,
            )
            != expected.credential_fingerprint
        ):
            _changed()


__all__ = [
    "GenerationSpeechConnection",
    "capture_generation_speech_connection",
    "require_generation_speech_connection",
]
