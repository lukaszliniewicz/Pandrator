"""Read project-owned segment choices independently of mutable library links."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .access.current_session_policy import require_current_session_access
from .project_speech_references import (
    StoredProjectSpeechReference,
    list_project_speech_references,
    normalize_speech_reference_binding,
    speech_reference_selector,
)


class SegmentSpeechReferenceIndex:
    """Read-only memoization within one caller-owned DB unit and actor scope."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._revisions: dict[tuple[str, str], dict[str, StoredProjectSpeechReference]] = {}

    def for_revision(self, session_id: str, revision_id: str) -> dict[str, StoredProjectSpeechReference]:
        key = (session_id, revision_id)
        if key not in self._revisions:
            def guard(current: Session) -> None:
                require_current_session_access(current, session_id)

            rows = list_project_speech_references(
                self.session, session_id, segment_revision_id=revision_id,
                authorization_guard=guard,
            )
            self._revisions[key] = {row.slot_key: row for row in rows}
        return self._revisions[key]


def has_selected_voice_override(snapshot: dict[str, Any]) -> bool:
    from .generation_rendering import is_strict_single_voice

    if is_strict_single_voice(snapshot.get("tts") or {}):
        return False
    alternate = (snapshot.get("selected_segment_override") or {}).get("tts") or {}
    return any(bool(str(alternate.get(key) or "").strip()) for key in ("voice_id", "voice", "speaker"))


def uses_segment_speech_references(settings: dict[str, Any]) -> bool:
    from .generation_rendering import is_strict_single_voice
    from .speech_inputs import speech_input_adapter

    return not is_strict_single_voice(settings) and speech_input_adapter(settings)[0] == "audio_cpp"


def retained_voice_projection(stored: StoredProjectSpeechReference) -> dict[str, Any]:
    """Expose the editable choice and label without pins or library authority."""
    return retained_voice_metadata_projection(stored.selector, stored.binding)


def retained_voice_metadata_projection(selector: object, binding: object) -> dict[str, Any]:
    """Project finite public metadata without loading a private material locator."""
    selected = normalize_speech_reference_binding(binding)
    return {"binding": speech_reference_selector(selector), "display_name": selected["display_name"],
        "adapter": selected["adapter"], "model": selected["model"]}
