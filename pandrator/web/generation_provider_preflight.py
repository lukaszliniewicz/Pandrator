"""Read-only provider configuration projection for generation preparation."""

from __future__ import annotations

import json
import os
from typing import Any

from pandrator.logic import google_tts_audio, tts_handler
from pandrator.logic.tts_provider_policy import DEFAULT_TTS_SERVICE_ID
from pandrator.runtime import DataPaths

from .credential_identity import credential_namespace
from .credentials import resolve_provider_credential
from .database import Database
from .settings_identity import settings_workspace_id

_MAX_SERVICE_LENGTH = 120
_MAX_ROUTE_ID_LENGTH = 120
_MAX_MODEL_LENGTH = 160
_MAX_VOICE_LENGTH = 120
_MAX_PROJECT_LENGTH = 160
_MAX_LOCATION_LENGTH = 80

_VERTEX_INVALID_CREDENTIAL_WARNING = (
    "Vertex credentials must be valid service-account JSON."
)
_VERTEX_GLOBAL_LOCATION_WARNING = (
    "Gemini 3.8 TTS on Vertex requires the global location."
)
_WORKSPACE_CREDENTIAL_WARNING = "Choose a speech connection configured for this workspace."


def _bounded_text(value: object, limit: int) -> str:
    """Return bounded, whitespace-normalized metadata suitable for projection."""

    return " ".join(str(value or "").split())[:limit]


def _credential_origin(source: object) -> str:
    normalized = str(source or "").strip().lower()
    if normalized == "none":
        return "unconfigured"
    if normalized in {"database", "environment", "keyring", "file"}:
        return normalized
    return "unavailable"


def _credential_scope(
    database: Database,
    *,
    session_id: str | None,
    workspace_id: str | None,
) -> tuple[str | None, bool]:
    """Resolve supplied roots without granting authority from settings data."""

    if session_id is None and workspace_id is None:
        # Preserve the historical resolver default and support old callers that
        # use an unseeded synthetic database for a personal-scope projection.
        return None, True
    with database.snapshot_session() as session:
        resolved_workspace_id = settings_workspace_id(
            session, session_id=session_id, workspace_id=workspace_id
        )
        namespace = credential_namespace(session, resolved_workspace_id)
    return resolved_workspace_id, namespace.host_credentials_allowed


def generation_provider_preflight(
    database: Database,
    snapshot: dict[str, Any],
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Describe the selected TTS route without contacting its provider.

    Credential values are resolved only for Google routes so the projection
    can determine Vertex's configured project. The values and their references
    are never copied into the returned structure.
    """

    credential_workspace_id, host_allowed = _credential_scope(
        database, session_id=session_id, workspace_id=workspace_id
    )

    raw_tts = snapshot.get("tts")
    tts_settings = raw_tts if isinstance(raw_tts, dict) else {}
    selected_service = str(
        tts_settings.get("service")
        or tts_settings.get("tts_service")
        or DEFAULT_TTS_SERVICE_ID
    ).strip()
    service_config = tts_handler.get_service_config(tts_settings, selected_service) or {}
    service_id = tts_handler._normalize_service_id(
        str(service_config.get("id") or selected_service)
    )
    provider_id = tts_handler._normalize_service_id(
        str(service_config.get("provider") or service_id)
    )

    model = str(
        tts_settings.get("xtts_model")
        or tts_settings.get("model")
        or service_config.get("default_model")
        or ""
    ).strip()
    if provider_id == tts_handler.VERTEX_PROVIDER:
        model = tts_handler._normalize_model_for_provider(model, provider_id)
    elif provider_id == tts_handler.GEMINI_PROVIDER:
        model = tts_handler._normalize_model_for_provider(model, provider_id)
    voice = str(
        tts_settings.get("speaker")
        or tts_settings.get("voice")
        or service_config.get("default_voice")
        or ""
    ).strip()

    projection: dict[str, Any] = {
        "service": _bounded_text(service_config.get("name") or selected_service, _MAX_SERVICE_LENGTH),
        "route_id": _bounded_text(service_id, _MAX_ROUTE_ID_LENGTH),
        "model": _bounded_text(model, _MAX_MODEL_LENGTH),
        "voice": _bounded_text(voice, _MAX_VOICE_LENGTH),
        "access_checked": False,
    }

    if provider_id not in {tts_handler.GEMINI_PROVIDER, tts_handler.VERTEX_PROVIDER}:
        return projection

    secret_ref = str(service_config.get("secret_ref") or "").strip()
    api_key_env = str(service_config.get("api_key_env") or "").strip()
    credential = None
    uses_application_default = False
    try:
        fallback_environment_variable = api_key_env if host_allowed else ""
        if credential_workspace_id is None:
            credential = resolve_provider_credential(
                database,
                DataPaths(database.path.parent),
                service_id,
                secret_ref,
                fallback_environment_variable=fallback_environment_variable,
            )
        else:
            credential = resolve_provider_credential(
                database,
                DataPaths(database.path.parent),
                service_id,
                secret_ref,
                fallback_environment_variable=fallback_environment_variable,
                workspace_id=credential_workspace_id,
            )
    except (OSError, RuntimeError, TypeError, ValueError):
        # Resolver errors can include local credential-store details. Keep the
        # public projection stable and free of paths, references, and values.
        projection["credential_origin"] = "unavailable"
        projection["credential_configured"] = False

    configured = credential.configured if credential is not None else False
    if credential is not None:
        source = _credential_origin(credential.source)
        uses_application_default = (
            provider_id == tts_handler.VERTEX_PROVIDER and not configured and host_allowed
        )
        if not configured and not uses_application_default:
            projection["credential_origin"] = "unconfigured"
            projection["credential_configured"] = False
        elif uses_application_default:
            projection["credential_origin"] = "application_default"
            projection["credential_configured"] = None
        else:
            projection["credential_origin"] = source
            projection["credential_configured"] = configured

    nonlegacy_credential_missing = not host_allowed and (credential is None or not configured)

    if provider_id != tts_handler.VERTEX_PROVIDER:
        if nonlegacy_credential_missing:
            projection["configuration_warning"] = _WORKSPACE_CREDENTIAL_WARNING
        return projection

    configured_project = _bounded_text(
        service_config.get("vertex_project"), _MAX_PROJECT_LENGTH
    )
    location = _bounded_text(
        service_config.get("vertex_location")
        or tts_handler.VERTEX_AUDIO_DEFAULT_LOCATION,
        _MAX_LOCATION_LENGTH,
    )
    configuration_warnings: list[str] = []
    if nonlegacy_credential_missing:
        configuration_warnings.append(_WORKSPACE_CREDENTIAL_WARNING)

    credential_value = ""
    if credential is not None and credential.configured:
        try:
            credential_value = credential.resolved_value()
        except (OSError, RuntimeError, TypeError, ValueError):
            credential_value = ""

    credential_info: dict[str, Any] | None = None
    if credential is not None and credential.configured:
        try:
            parsed_credential = json.loads(credential_value)
        except (json.JSONDecodeError, TypeError):
            configuration_warnings.append(_VERTEX_INVALID_CREDENTIAL_WARNING)
        else:
            if isinstance(parsed_credential, dict):
                credential_info = parsed_credential
            else:
                configuration_warnings.append(_VERTEX_INVALID_CREDENTIAL_WARNING)

    project = configured_project
    project_resolution = "service" if configured_project else "runtime"
    if not configured_project and credential_info is not None:
        project = _bounded_text(credential_info.get("project_id"), _MAX_PROJECT_LENGTH)
        project_resolution = "credential"
    elif not configured_project and uses_application_default:
        project = _bounded_text(
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT"),
            _MAX_PROJECT_LENGTH,
        )
        project_resolution = "environment" if project else "runtime"
    elif (
        not configured_project
        and credential is not None
        and (credential.configured or host_allowed)
    ):
        project_resolution = "credential"
    elif not configured_project:
        project_resolution = "unavailable"

    if (
        credential is not None
        and credential.configured
        and credential_info is None
        and _VERTEX_INVALID_CREDENTIAL_WARNING not in configuration_warnings
    ):
        # The credential store reported a configured value but did not return
        # it, so avoid claiming it is a valid service-account configuration.
        configuration_warnings.append(_VERTEX_INVALID_CREDENTIAL_WARNING)

    projection.update(
        {
            "project": project,
            "project_resolution": project_resolution,
            "location": location,
        }
    )
    if google_tts_audio.is_structured_tts_model(model) and location != "global":
        configuration_warnings.append(_VERTEX_GLOBAL_LOCATION_WARNING)
    if configuration_warnings:
        projection["configuration_warning"] = " ".join(configuration_warnings)
    return projection
