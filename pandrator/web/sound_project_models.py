"""Immutable, session-scoped sound cue plan versions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, new_id, utcnow


class ProjectSoundPlan(Base):
    __tablename__ = "project_sound_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    speech_plan_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_plan_revisions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("session_id", "revision", name="uq_project_sound_plan_revision"),
        CheckConstraint("revision >= 1", name="ck_project_sound_plan_revision_positive"),
        Index("ix_project_sound_plans_session_revision", "session_id", "revision"),
    )


class ProjectSoundCue(Base):
    __tablename__ = "project_sound_cues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    logical_id: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("project_sound_plans.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[str] = mapped_column(
        ForeignKey("sound_variants.id", ondelete="RESTRICT"), nullable=False
    )
    anchor_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    playback_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    placement_status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "logical_id", name="uq_project_sound_cue_logical"),
        Index("ix_project_sound_cues_plan_logical", "plan_id", "logical_id"),
        CheckConstraint(
            "placement_status IN ('ready', 'needs_placement')",
            name="ck_project_sound_cue_placement",
        ),
    )
