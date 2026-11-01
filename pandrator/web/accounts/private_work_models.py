"""Stable ownership records for private quick transcriptions."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base


class QuickTranscriptionOwner(Base):
    __tablename__ = "quick_transcription_owners"
    __table_args__ = (
        Index(
            "ix_quick_transcription_owners_user_id",
            "user_id",
        ),
        Index(
            "ix_quick_transcription_owners_workspace_id",
            "workspace_id",
        ),
    )

    transcription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "quick_transcriptions.id",
            name="fk_quick_transcription_owners_transcription_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_quick_transcription_owners_user_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "workspaces.id",
            name="fk_quick_transcription_owners_workspace_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


__all__ = ["QuickTranscriptionOwner"]
