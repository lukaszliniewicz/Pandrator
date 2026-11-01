"""Scoped managed-voice lookups shared by library request adapters."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Voice, VoiceSample
from .voice_identity import voice_creation_workspace, voice_sample_workspace, voice_workspace


def voices_in_workspace(session: Session, *, workspace_id: str | None = None) -> list[Voice]:
    with session.no_autoflush:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        return list(
            session.scalars(
                select(Voice)
                .where(Voice.workspace_id == workspace.id)
                .order_by(Voice.name, Voice.id)
            )
        )


def voice_in_workspace(
    session: Session, voice_id: str, *, workspace_id: str | None = None
) -> Voice | None:
    with session.no_autoflush:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        voice = session.scalar(
            select(Voice).where(Voice.id == voice_id, Voice.workspace_id == workspace.id)
        )
        if voice is not None:
            voice_workspace(session, voice, workspace_id=workspace.id)
        return voice


def voice_name_owner(
    session: Session, name: str, *, workspace_id: str | None = None
) -> Voice | None:
    with session.no_autoflush:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        return session.scalar(
            select(Voice).where(Voice.name == name, Voice.workspace_id == workspace.id)
        )


def voice_sample_in_workspace(
    session: Session,
    sample_id: str,
    *,
    voice_id: str,
    workspace_id: str | None = None,
) -> VoiceSample | None:
    with session.no_autoflush:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        sample = session.scalar(
            select(VoiceSample)
            .join(Voice, Voice.id == VoiceSample.voice_id)
            .where(
                VoiceSample.id == sample_id,
                VoiceSample.voice_id == voice_id,
                VoiceSample.workspace_id == workspace.id,
                Voice.workspace_id == workspace.id,
            )
        )
        if sample is not None:
            voice_sample_workspace(session, sample, workspace_id=workspace.id)
        return sample
