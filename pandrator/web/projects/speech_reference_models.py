"""Saved project speech-reference metadata models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, utcnow


class ProjectSpeechReference(Base):
    """A session-owned saved speech selection and optional retained input pin."""

    __tablename__ = "project_speech_references"
    __table_args__ = (
        CheckConstraint(
            "revision >= 1", name="ck_project_speech_references_revision"
        ),
        CheckConstraint(
            "(input_artifact_id IS NULL AND input_content_hash IS NULL) "
            "OR (input_artifact_id IS NOT NULL AND input_content_hash IS NOT NULL)",
            name="ck_project_speech_references_input_pin_pair",
        ),
        CheckConstraint(
            "length(selector_hash) = 64",
            name="ck_project_speech_references_selector_hash_length",
        ),
        CheckConstraint(
            "input_content_hash IS NULL OR length(input_content_hash) = 64",
            name="ck_project_speech_references_input_hash_length",
        ),
        Index(
            "ix_project_speech_references_input_artifact_id", "input_artifact_id"
        ),
    )

    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slot_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    selector_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    selector_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_artifact_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    input_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
