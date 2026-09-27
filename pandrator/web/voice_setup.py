"""Shared, revisioned voice-mode setup for audiobook and voiceover sessions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from sqlalchemy.orm import Session

from .models import SessionRecord
from .settings_policy import RevisionConflict, stable_hash

VoiceMode = Literal["single_voice", "multi_voice"]
_WORKFLOWS = frozenset({"audiobook", "voiceover"})


def _service(services: Any, name: str) -> Any:
    if isinstance(services, Mapping):
        return services[name]
    return getattr(services, name)


def _record(session: Session, session_id: str) -> SessionRecord:
    record = session.get(SessionRecord, session_id)
    if record is None or record.trashed_at is not None:
        raise KeyError(session_id)
    if record.workflow_kind not in _WORKFLOWS:
        raise ValueError("Voice setup is only available for audiobook and voiceover sessions.")
    return record


def _mode_version(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _snapshot(services: Any, session: Session, session_id: str) -> dict[str, Any]:
    record = _record(session, session_id)
    tts = _service(services, "workspace_settings").get_in_session(
        session, session_id, "tts"
    )
    effective = tts["effective"] if isinstance(tts.get("effective"), dict) else {}
    tts_summary = {
        "service": str(effective.get("service") or effective.get("tts_service") or ""),
        "model": str(effective.get("model") or effective.get("xtts_model") or ""),
        "voice": str(effective.get("voice") or effective.get("speaker") or ""),
        "language": str(effective.get("language") or ""),
    }
    voice_mode_version = _mode_version(effective.get("voice_mode_version"))
    casting_enabled = bool(effective.get("casting_enabled"))
    mode: VoiceMode = "multi_voice" if casting_enabled else "single_voice"
    configuration_revision = stable_hash(
        {
            "workflow_kind": record.workflow_kind,
            "record_revision": int(record.revision),
            "tts_revision": int(tts["revision"]),
            "voice_mode_version": voice_mode_version,
            "casting_enabled": casting_enabled,
            "tts": tts_summary,
        }
    )
    return {
        "session_id": session_id,
        "workflow_kind": record.workflow_kind,
        "mode": mode,
        "configuration_revision": configuration_revision,
        "casting_enabled": casting_enabled,
        "legacy_voice_overrides": voice_mode_version < 1,
        "tts": tts_summary,
    }


def get_voice_setup(
    services: Any,
    session: Session,
    session_id: str,
) -> dict[str, Any]:
    """Read shared voice-mode setup without creating or changing session state."""

    return _snapshot(services, session, session_id)


def configure_voice_setup(
    services: Any,
    session: Session,
    session_id: str,
    *,
    expected_revision: str,
    mode: VoiceMode,
) -> dict[str, Any]:
    """Change only casting mode and the strict-rendering adoption marker."""

    if mode not in {"single_voice", "multi_voice"}:
        raise ValueError(f"Unknown voice setup mode: {mode}")
    current = _snapshot(services, session, session_id)
    if (
        not isinstance(expected_revision, str)
        or expected_revision.lower() != current["configuration_revision"]
    ):
        raise RevisionConflict("Voice setup changed in another client.")

    settings = _service(services, "workspace_settings")
    tts = settings.get_in_session(session, session_id, "tts")
    fields = {
        "casting_enabled": mode == "multi_voice",
        "voice_mode_version": 1,
    }
    override = tts.get("override")
    override = override if isinstance(override, dict) else {}
    if any(override.get(key) != value for key, value in fields.items()):
        settings.patch_in_session(
            session,
            session_id,
            "tts",
            int(tts["revision"]),
            fields,
        )
    session.flush()
    return get_voice_setup(services, session, session_id)


__all__ = ["VoiceMode", "configure_voice_setup", "get_voice_setup"]
