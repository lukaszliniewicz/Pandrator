"""Durable attribution sidecars for queued jobs and audit events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, utcnow


class JobActorRecord(Base):
    __tablename__ = "job_actors"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NULL AND auth_revision IS NULL) OR "
            "(user_id IS NOT NULL AND auth_revision IS NOT NULL AND auth_revision >= 1)",
            name="ck_job_actors_user_auth_pair",
        ),
        Index("ix_job_actors_user_id", "user_id"),
    )

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_actors_job_id_jobs"),
        primary_key=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_job_actors_user_id_users"),
    )
    auth_revision: Mapped[int | None] = mapped_column(Integer)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditActorRecord(Base):
    __tablename__ = "audit_actors"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NULL AND auth_revision IS NULL) OR "
            "(user_id IS NOT NULL AND auth_revision IS NOT NULL AND auth_revision >= 1)",
            name="ck_audit_actors_user_auth_pair",
        ),
        Index("ix_audit_actors_user_id", "user_id"),
    )

    audit_event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "audit_events.id",
            ondelete="CASCADE",
            name="fk_audit_actors_audit_event_id_audit_events",
        ),
        primary_key=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_audit_actors_user_id_users"),
    )
    auth_revision: Mapped[int | None] = mapped_column(Integer)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
