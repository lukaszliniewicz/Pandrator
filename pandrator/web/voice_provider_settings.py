"""Revision fences for voice publication's server-owned workspace settings."""

from typing import Any

from sqlalchemy.orm import Session

from .accounts.execution_actor import HostActor, current_execution_actor
from .credential_identity import credential_namespace
from .speech_catalogue_access import tts_revision_headers

_KEYS = ("services.tts", "defaults.tts")


def capture_voice_provider_settings(session: Session, workspace_id: str) -> dict[str, Any]:
    return {
        "tts_settings_revisions": dict(
            zip(_KEYS, tts_revision_headers(session, workspace_id), strict=True)
        )
    }


def check_voice_provider_settings(
    session: Session, payload: dict[str, Any], workspace_id: str
) -> None:
    if "tts_settings_revisions" not in payload:
        if (
            type(current_execution_actor.get()) is HostActor
            or credential_namespace(session, workspace_id).legacy_host_access
        ):
            return
        raise ValueError(
            "Voice provider operation has no captured workspace settings. Submit it again."
        )
    expected = payload["tts_settings_revisions"]
    if (
        type(expected) is not dict
        or set(expected) != set(_KEYS)
        or any(type(v) is not int or v < 0 for v in expected.values())
    ):
        raise ValueError("Voice provider operation has invalid captured workspace settings.")
    if expected != capture_voice_provider_settings(session, workspace_id)["tts_settings_revisions"]:
        raise ValueError("Workspace voice provider settings changed. Submit the operation again.")
