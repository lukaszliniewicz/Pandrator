"""Leaf persistence for account-bound project capabilities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..model_base import Base, new_id, utcnow


class ProjectLink(Base):
    __tablename__ = "project_links"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('manage_project', 'review_translation')", name="ck_project_links_mode"
        ),
        CheckConstraint("revision >= 1", name="ck_project_links_revision"),
        UniqueConstraint("token_hash", name="uq_project_links_token_hash"),
        UniqueConstraint(
            "project_id", "issuer_user_id", "request_key", name="uq_project_links_request"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    issuer_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    issuer_auth_revision: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Logical immutable references survive artifact deletion and never obstruct project purge.
    target_session_id: Mapped[str | None] = mapped_column(String(36))
    target_artifact_id: Mapped[str | None] = mapped_column(String(36))
    target_revision_id: Mapped[str | None] = mapped_column(String(36))
    target_hash: Mapped[str | None] = mapped_column(String(128))
    target_revision_number: Mapped[int | None] = mapped_column(Integer)
    target_composition_hash: Mapped[str | None] = mapped_column(String(128))
    request_key: Mapped[str | None] = mapped_column(String(100))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    # Pin roots so moving a project or replacing its root cannot transfer a capability.
    root_session_id: Mapped[str] = mapped_column(String(36))
    workspace_id: Mapped[str] = mapped_column(String(36))


class ProjectLinkSession(Base):
    __tablename__ = "project_link_sessions"
    browser_session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_auth_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    link_id: Mapped[str] = mapped_column(
        ForeignKey("project_links.id", ondelete="CASCADE"), index=True
    )
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectLinkSubmission(Base):
    __tablename__ = "project_link_submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_project_link_submissions_status",
        ),
        CheckConstraint("revision >= 1", name="ck_project_link_submissions_revision"),
        UniqueConstraint(
            "link_id",
            "browser_session_id",
            "request_key",
            name="uq_project_link_submissions_request",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    link_id: Mapped[str] = mapped_column(
        ForeignKey("project_links.id", ondelete="CASCADE"), index=True
    )
    browser_session_id: Mapped[str | None] = mapped_column(String(36))
    reviewer_label: Mapped[str | None] = mapped_column(String(80))
    changes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    request_key: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(36))
    accepted_artifact_id: Mapped[str | None] = mapped_column(String(36))
