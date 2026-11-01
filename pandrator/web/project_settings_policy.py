"""Project settings select delivery choices without configuring connections."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .accounts.execution_actor import UserActor, current_execution_actor
from .credential_identity import credential_namespace
from .settings_identity import settings_workspace_id
from .settings_policy import SECRET_KEYS
from .speech_input_contracts import (
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
)

_CONNECTION_FIELDS = frozenset(
    {
        "provider_configs",
        "service_configs",
        "openai_audio_endpoints_json",
        "secret_ref",
        "credential_ref",
        "api_key_env",
        "api_base",
        "base_url",
        "external_server_url",
        "audio_cpp_reference_audio_path",
        PROJECT_SPEECH_INPUT_KEY,
        PROJECT_SPEECH_BINDING_KEY,
        PROJECT_SPEECH_SELECTOR_KEY,
        PROJECT_SPEECH_SOURCE_KEY,
        "_retained_cast_resolutions",
    }
)
_INTERNAL_TTS_ROUTES = frozenset(
    {"preview_service_id", "service_id", "preview_adapter", "preview_api_base"}
)


class ProjectSpeechPreferencesError(ValueError):
    """A project override attempted to configure a workspace connection."""


def public_project_settings(value: Any) -> Any:
    """Copy a project DTO, including old receipts, without connection bindings.

    Stored reproduction snapshots and server digests remain unchanged. Connection
    management uses its separate workspace surface. Text values are content;
    URLs inside project text or instructions are not rewritten.
    """
    if isinstance(value, list):
        return [public_project_settings(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        normalized = str(key).casefold()
        if (
            normalized in _CONNECTION_FIELDS
            or normalized in SECRET_KEYS
            or normalized.endswith(
                (
                    "_base_url",
                    "_secret_ref",
                    "_credential_ref",
                    "_api_key_env",
                    "_secret",
                    "_password",
                    "_api_key",
                    "_access_token",
                    "_refresh_token",
                    "_credential",
                    "_credentials",
                )
            )
        ):
            continue
        if normalized == "openai_audio_endpoint" and (
            type(item) is not str or "://" in item or item.startswith(("/", "\\"))
        ):
            continue
        result[key] = public_project_settings(item)
    return result


def _connection_override(value: Any) -> bool:
    if isinstance(value, list):
        return any(_connection_override(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        normalized = str(key).casefold()
        if (
            normalized in _CONNECTION_FIELDS
            or normalized in _INTERNAL_TTS_ROUTES
            or normalized in SECRET_KEYS
            or normalized.endswith(
                (
                    "_base_url",
                    "_secret_ref",
                    "_credential_ref",
                    "_api_key_env",
                    "_secret",
                    "_password",
                    "_api_key",
                    "_access_token",
                    "_refresh_token",
                    "_credential",
                    "_credentials",
                )
            )
        ):
            return True
        if (
            normalized == "openai_audio_endpoint"
            and item is not None
            and (type(item) is not str or "://" in item or item.startswith(("/", "\\")))
        ):
            return True
        if _connection_override(item):
            return True
    return False


def validate_project_speech_preferences(session: Session, session_id: str, *layers: Any) -> None:
    """User project layers may select a connection, never configure one.

    Call after session action proof, before storing a user layer. Ordinary
    delivery-only overrides do not need another namespace query. The original
    solo namespace and trusted internal callers retain advanced compatibility.
    """
    if type(current_execution_actor.get()) is not UserActor or not any(
        _connection_override(layer) for layer in layers
    ):
        return
    with session.no_autoflush:
        workspace_id = settings_workspace_id(session, session_id=session_id)
        if credential_namespace(session, workspace_id).legacy_host_access:
            return
    raise ProjectSpeechPreferencesError(
        "Choose an existing workspace speech connection. Configure connections in workspace settings."
    )
