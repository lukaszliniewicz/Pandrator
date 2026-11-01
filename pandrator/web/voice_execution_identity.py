"""Resolve and revalidate persisted ownership for voice-processing work."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from .accounts.identity import active_workspace, initial_personal_workspace
from .accounts.models import Workspace
from .models import Artifact, Voice, VoiceSample
from .projects.artifact_identity import resolve_artifact_ownership
from .voice_identity import voice_sample_workspace, voice_workspace


def voice_execution_workspace(
    session: Session,
    payload: Mapping[str, Any],
    *,
    voice_id: str | None = None,
    sample_ids: Iterable[str] = (),
    artifact_ids: Iterable[str] = (),
    session_id: str | None = None,
    expected_workspace_id: str | None = None,
) -> Workspace:
    """Check frozen context against every input before work or publication.

    Legacy work may omit the payload field and use its migrated input owner.
    An explicit field is always validated, including a malformed or null value.
    The returned owner does not grant authority to read inputs or spend credits.
    """
    with session.no_autoflush:
        evidence: set[str] = set()
        if "workspace_id" in payload:
            requested = payload["workspace_id"]
            if not isinstance(requested, str) or not requested:
                raise ValueError("Voice processing has invalid workspace context.")
            evidence.add(active_workspace(session, requested, "Voice processing").id)
        if expected_workspace_id is not None:
            evidence.add(active_workspace(session, expected_workspace_id, "Voice publication").id)
        if voice_id is not None:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice not found.")
            evidence.add(voice_workspace(session, voice).id)
        for sample_id in set(sample_ids):
            sample = session.get(VoiceSample, sample_id)
            if sample is None:
                raise ValueError("Voice sample not found.")
            if voice_id is not None and sample.voice_id != voice_id:
                raise ValueError("The voice sample belongs to another voice.")
            evidence.add(voice_sample_workspace(session, sample).id)
        for artifact_id in set(artifact_ids):
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise ValueError("Voice processing input artifact not found.")
            evidence.add(resolve_artifact_ownership(session, artifact=artifact).workspace_id)
        if session_id is not None:
            evidence.add(resolve_artifact_ownership(session, session_id=session_id).workspace_id)
        if len(evidence) > 1:
            raise ValueError("Voice processing inputs have inconsistent workspace ownership.")
        if not evidence:
            return initial_personal_workspace(session)
        return active_workspace(session, evidence.pop(), "Voice processing")


def voice_reconciliation_voice(
    session: Session, voice_id: str, *, expected_workspace_id: str
) -> Voice:
    """Retain an external side-effect receipt under its unchanged original owner.

    Archiving prevents new work, but cannot undo a provider request already sent.
    Only receipt reconciliation may use this check; admission and new publication
    require active ownership through voice_execution_workspace.
    """
    with session.no_autoflush:
        workspace = session.get(Workspace, expected_workspace_id)
        voice = session.get(Voice, voice_id)
        if workspace is None or workspace.state not in {"active", "archived"}:
            raise ValueError("The original voice-processing workspace no longer exists.")
        if voice is None or voice.workspace_id != workspace.id:
            raise ValueError("The voice owner changed before provider reconciliation.")
        return voice
