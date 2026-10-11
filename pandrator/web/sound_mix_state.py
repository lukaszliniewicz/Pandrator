"""Narrow artifact invalidation for session sound mixes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifacts import ArtifactService
from .models import Artifact


def invalidate_sound_mixes(session: Session, session_id: str) -> int:
    """Stale current sound-mix artifacts and their artifact descendants only."""

    mixes = list(
        session.scalars(
            select(Artifact).where(
                Artifact.session_id == session_id,
                Artifact.role == "sound_mix.audio",
                Artifact.kind == "audio",
                Artifact.state == "current",
            )
        ).all()
    )
    for artifact in mixes:
        artifact.state = "stale"
        ArtifactService._mark_descendants_stale(session, artifact.id)
    return len(mixes)


__all__ = ["invalidate_sound_mixes"]
