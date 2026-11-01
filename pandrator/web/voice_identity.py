"""Persisted ownership checks for voices, samples, and training runs."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .accounts.identity import active_workspace, initial_personal_workspace
from .accounts.models import Workspace
from .models import Artifact, TrainingRun, Voice, VoiceSample
from .projects.artifact_identity import resolve_artifact_ownership


def voice_creation_workspace(
    session: Session,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Resolve an active requested owner or the migration-seeded personal workspace."""
    with session.no_autoflush:
        if workspace_id is None:
            return initial_personal_workspace(session)
        return active_workspace(session, workspace_id, "Requested voice workspace")


def _stored_workspace(
    session: Session,
    workspace_id: str | None,
    *,
    owner_kind: str,
    owner_id: str,
) -> Workspace:
    if workspace_id is None:
        raise ValueError(f"{owner_kind} {owner_id!r} has no stored workspace ownership.")
    return active_workspace(session, workspace_id, f"{owner_kind} {owner_id!r} ownership")


def _require_requested_workspace(
    session: Session,
    *,
    requested_workspace_id: str | None,
    stored_workspace: Workspace,
    owner_kind: str,
    owner_id: str,
) -> None:
    if requested_workspace_id is None:
        return
    requested = active_workspace(
        session, requested_workspace_id, f"Requested {owner_kind.lower()} workspace"
    )
    if requested.id != stored_workspace.id:
        raise ValueError(
            f"{owner_kind} {owner_id!r} belongs to workspace {stored_workspace.id!r}, "
            f"not requested workspace {requested.id!r}."
        )


def voice_workspace(
    session: Session,
    voice: Voice,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Validate a voice's stored active owner and any requested scope."""
    with session.no_autoflush:
        workspace = _stored_workspace(
            session,
            voice.workspace_id,
            owner_kind="Voice",
            owner_id=voice.id,
        )
        _require_requested_workspace(
            session,
            requested_workspace_id=workspace_id,
            stored_workspace=workspace,
            owner_kind="Voice",
            owner_id=voice.id,
        )
        return workspace


def voice_sample_workspace(
    session: Session,
    sample: VoiceSample,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Validate a sample owner and its linked voice and artifact ownership."""
    with session.no_autoflush:
        workspace = _stored_workspace(
            session,
            sample.workspace_id,
            owner_kind="Voice sample",
            owner_id=sample.id,
        )
        _require_requested_workspace(
            session,
            requested_workspace_id=workspace_id,
            stored_workspace=workspace,
            owner_kind="Voice sample",
            owner_id=sample.id,
        )

        voice = session.get(Voice, sample.voice_id)
        if voice is None:
            raise ValueError(
                f"Voice sample {sample.id!r} references missing voice {sample.voice_id!r}."
            )
        voice_workspace(session, voice, workspace_id=workspace.id)

        artifact = session.get(Artifact, sample.artifact_id)
        if artifact is None:
            raise ValueError(
                f"Voice sample {sample.id!r} references missing artifact {sample.artifact_id!r}."
            )
        artifact_owner = resolve_artifact_ownership(session, artifact=artifact)
        if artifact_owner.workspace_id != workspace.id:
            raise ValueError(
                f"Voice sample {sample.id!r} and artifact {artifact.id!r} have "
                "different workspace ownership."
            )
        return workspace


def training_workspace(
    session: Session,
    training: TrainingRun,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Validate a retained run owner and every non-null linked voice or artifact."""
    with session.no_autoflush:
        workspace = _stored_workspace(
            session,
            training.workspace_id,
            owner_kind="Training run",
            owner_id=training.id,
        )
        _require_requested_workspace(
            session,
            requested_workspace_id=workspace_id,
            stored_workspace=workspace,
            owner_kind="Training run",
            owner_id=training.id,
        )

        if training.voice_id is not None:
            voice = session.get(Voice, training.voice_id)
            if voice is None:
                raise ValueError(
                    f"Training run {training.id!r} references missing voice {training.voice_id!r}."
                )
            voice_workspace(session, voice, workspace_id=workspace.id)

        for column_name, artifact_id in (
            ("source artifact", training.source_artifact_id),
            ("source text artifact", training.source_text_artifact_id),
            ("output artifact", training.output_artifact_id),
        ):
            if artifact_id is None:
                continue
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise ValueError(
                    f"Training run {training.id!r} references missing {column_name} "
                    f"{artifact_id!r}."
                )
            artifact_owner = resolve_artifact_ownership(session, artifact=artifact)
            if artifact_owner.workspace_id != workspace.id:
                raise ValueError(
                    f"Training run {training.id!r} and {column_name} {artifact.id!r} "
                    "have different workspace ownership."
                )

        return workspace


__all__ = [
    "training_workspace",
    "voice_creation_workspace",
    "voice_sample_workspace",
    "voice_workspace",
]
