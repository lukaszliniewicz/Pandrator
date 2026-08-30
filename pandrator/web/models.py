"""Authoritative SQLAlchemy models for the browser application."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AppSettingHistory(Base):
    __tablename__ = "app_settings_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class StoredCredential(Base):
    """A write-only provider credential stored in the application database."""

    __tablename__ = "stored_credentials"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    secret_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="llm")
    provider_key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    base_url: Mapped[str | None] = mapped_column(Text)
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    options_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("kind", "provider_key", "label", name="uq_provider_identity"),
    )


class ProviderModel(Base):
    __tablename__ = "provider_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_temperature: Mapped[float | None] = mapped_column(Float)
    default_reasoning_effort: Mapped[str | None] = mapped_column(String(80))
    input_cost_per_million: Mapped[float | None] = mapped_column(Float)
    cached_input_cost_per_million: Mapped[float | None] = mapped_column(Float)
    output_cost_per_million: Mapped[float | None] = mapped_column(Float)
    context_window_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=262_144
    )
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    options_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "model_id", name="uq_provider_model"),
    )


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, default=new_id
    )
    legacy_name: Mapped[str | None] = mapped_column(String(255), index=True)
    legacy_path: Mapped[str | None] = mapped_column(Text)
    workflow_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="audiobook"
    )
    source_language: Mapped[str] = mapped_column(
        String(40), nullable=False, default="auto"
    )
    target_language: Mapped[str | None] = mapped_column(String(40))
    workflow_preset: Mapped[str] = mapped_column(
        String(64), nullable=False, default="custom"
    )
    included_stages_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="idle", index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionSetting(Base):
    __tablename__ = "session_settings"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    section: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SessionSettingHistory(Base):
    __tablename__ = "session_settings_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section: Mapped[str] = mapped_column(String(80), nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class OutcomePlan(Base):
    __tablename__ = "outcome_plans"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class OutcomePlanHistory(Base):
    __tablename__ = "outcome_plan_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SourceRecord(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    external_path: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SourceAsset(Base):
    __tablename__ = "source_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    external_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="current", index=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SessionSource(Base):
    __tablename__ = "session_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_asset_id: Mapped[str] = mapped_column(
        ForeignKey("source_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(80), nullable=False, default="primary")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "source_asset_id", "role", name="uq_session_source_attachment"
        ),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    preset: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class StageRun(Base):
    __tablename__ = "stage_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    settings_hash: Mapped[str | None] = mapped_column(String(128))
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("workflow_run_id", "stage_key", name="uq_workflow_stage"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    workflow_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    resource_keys_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    progress_detail: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(120), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(120), nullable=False, default="artifact")
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    settings_hash: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("relative_path", name="uq_artifact_relative_path"),
    )


class ArtifactEdge(Base):
    __tablename__ = "artifact_edges"

    parent_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    child_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(
        String(80), nullable=False, default="derived_from"
    )


class SessionStageSelection(Base):
    """The artifact currently chosen for a transformation stage.

    Artifact ``state`` describes the historical default path retained for
    compatibility with older clients.  This table is the explicit user-facing
    selection and may intentionally point at a historical (``stale``) artifact.
    """

    __tablename__ = "session_stage_selections"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    stage_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(80), nullable=False)
    language: Mapped[str | None] = mapped_column(String(40))
    active_revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DocumentRevision(Base):
    __tablename__ = "document_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="SET NULL")
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settings_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("document_id", "revision_number", name="uq_document_revision"),
    )


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    node_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, default="subtitle_cue"
    )
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(160))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    __table_args__ = (
        UniqueConstraint("revision_id", "ordinal", name="uq_revision_segment_ordinal"),
    )


class SegmentLineage(Base):
    __tablename__ = "segment_lineage"

    parent_segment_id: Mapped[str] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True
    )
    child_segment_id: Mapped[str] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(40), nullable=False, default="derived")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TimedWord(Base):
    __tablename__ = "timed_words"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_id: Mapped[str | None] = mapped_column(
        ForeignKey("segments.id", ondelete="SET NULL"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(160))
    confidence: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    __table_args__ = (
        UniqueConstraint("revision_id", "ordinal", name="uq_timed_word_ordinal"),
    )


class GenerationPlan(Base):
    __tablename__ = "generation_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    active_revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class GenerationPlanRevision(Base):
    __tablename__ = "generation_plan_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="SET NULL"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_id", "revision_number", name="uq_generation_plan_revision"
        ),
    )


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_revision_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plan_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    operation: Mapped[str] = mapped_column(
        String(32), nullable=False, default="generate"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ready", index=True
    )
    pause_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    settings_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    settings_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence_number", name="uq_generation_run_sequence"
        ),
    )


class GenerationSegment(Base):
    __tablename__ = "generation_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_revision_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plan_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_segment_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    alignment_group: Mapped[str | None] = mapped_column(String(64), index=True)
    node_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, default="paragraph"
    )
    paragraph_break_after: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    speaker: Mapped[str | None] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    optimized_text: Mapped[str | None] = mapped_column(Text)
    speech_plan_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    optimization_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_requested", index=True
    )
    optimization_source_hash: Mapped[str | None] = mapped_column(String(128))
    optimization_reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    optimization_model: Mapped[str | None] = mapped_column(String(255))
    voice_id: Mapped[str | None] = mapped_column(
        ForeignKey("voices.id", ondelete="SET NULL"), index=True
    )
    voice: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str | None] = mapped_column(String(40))
    silence_after_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    marked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ready", index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_revision_id", "ordinal", name="uq_generation_segment_ordinal"
        ),
    )


class GenerationSegmentRevision(Base):
    __tablename__ = "generation_segment_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generation_segment_id: Mapped[str] = mapped_column(
        ForeignKey("generation_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    alignment_group: Mapped[str | None] = mapped_column(String(64))
    node_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, default="paragraph"
    )
    paragraph_break_after: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    speaker: Mapped[str | None] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    optimized_text: Mapped[str | None] = mapped_column(Text)
    speech_plan_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    optimization_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_requested"
    )
    optimization_reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    marked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    voice_id: Mapped[str | None] = mapped_column(String(36))
    voice: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str | None] = mapped_column(String(40))
    silence_after_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AudioTake(Base):
    __tablename__ = "audio_takes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_segment_id: Mapped[str] = mapped_column(
        ForeignKey("generation_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    parent_take_id: Mapped[str | None] = mapped_column(
        ForeignKey("audio_takes.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="tts")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    settings_hash: Mapped[str | None] = mapped_column(String(128))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class OutputAssembly(Base):
    __tablename__ = "output_assemblies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    settings_hash: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ResourceClaim(Base):
    __tablename__ = "resource_claims"

    resource_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lease_owner: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class UploadSessionRecord(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    received_json: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    expected_hash: Mapped[str | None] = mapped_column(String(128))
    temporary_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(
        String(80), nullable=False, default="source_cleaning", index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    result_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ready", index=True
    )
    source_content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    settings_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DispatchRun(Base):
    """A passive, externally processed subtitle correction/translation run."""

    __tablename__ = "dispatch_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    output_role: Mapped[str] = mapped_column(String(120), nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="current"
    )
    source_content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    source_language: Mapped[str] = mapped_column(
        String(40), nullable=False, default="auto"
    )
    target_language: Mapped[str | None] = mapped_column(String(40))
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    selection_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_head_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    result_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    result_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="SET NULL"), index=True
    )
    glossary_json: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="ready", index=True
    )
    batch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_batch_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DispatchBatch(Base):
    """An immutable semantic subtitle block and its externally supplied result."""

    __tablename__ = "dispatch_batches"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_run_id", "ordinal", name="uq_dispatch_batch_ordinal"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dispatch_run_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    input_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="ready", index=True
    )
    lease_token: Mapped[str | None] = mapped_column(String(160))
    claim_key: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    normalized_output_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    submission_key: Mapped[str | None] = mapped_column(String(200))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_key: Mapped[str | None] = mapped_column(String(160))
    input_hash: Mapped[str | None] = mapped_column(String(128))
    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    summary: Mapped[str | None] = mapped_column(Text)
    input_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    output_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("agent_run_id", "ordinal", name="uq_agent_step_ordinal"),
        UniqueConstraint("agent_run_id", "unit_key", name="uq_agent_step_unit_key"),
    )


class KnowledgeLedger(Base):
    """Versioned, session-scoped research or terminology knowledge."""

    __tablename__ = "knowledge_ledgers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_language: Mapped[str] = mapped_column(
        String(40), nullable=False, default="auto"
    )
    target_language: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "kind",
            "source_language",
            "target_language",
            name="uq_knowledge_ledger_scope",
        ),
    )


class ResearchCacheEntry(Base):
    """Credential-free persistent cache for bounded web-research tool results."""

    __tablename__ = "research_cache_entries"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    response_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class PronunciationEntry(Base):
    """Reviewed or proposed pronunciation reusable by speech-plan compilation."""

    __tablename__ = "pronunciation_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="global", index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
    )
    source_form: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_form: Mapped[str] = mapped_column(
        String(512), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(
        String(40), nullable=False, default="und", index=True
    )
    phonetic: Mapped[str] = mapped_column(Text, nullable=False)
    alphabet: Mapped[str] = mapped_column(
        String(32), nullable=False, default="respelling"
    )
    backend: Mapped[str] = mapped_column(
        String(80), nullable=False, default="*", index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="reviewed", index=True
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "scope",
            "session_id",
            "normalized_form",
            "language",
            "backend",
            name="uq_pronunciation_entry_identity",
        ),
    )


class Voice(Base):
    __tablename__ = "voices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    language: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    rvc_model_ref: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class VoiceSample(Base):
    __tablename__ = "voice_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    voice_id: Mapped[str] = mapped_column(
        ForeignKey("voices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_language: Mapped[str | None] = mapped_column(String(40))
    transcript_reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, default="xtts")
    voice_id: Mapped[str | None] = mapped_column(
        ForeignKey("voices.id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True
    )
    source_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    source_text_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    output_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), index=True
    )
    workflow_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="SET NULL"), index=True
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    request_key: Mapped[str | None] = mapped_column(String(200))
    stage: Mapped[str | None] = mapped_column(String(80))
    provider_key: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cost_source: Mapped[str | None] = mapped_column(String(40))
    raw_usage_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "request_key",
            name="uq_usage_event_agent_request",
        ),
    )


class ExportRecord(Base):
    __tablename__ = "exports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL")
    )
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    options_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class OwnerAccount(Base):
    __tablename__ = "owner_account"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AutomationClient(Base):
    __tablename__ = "automation_clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    redirect_uris_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    allowed_scopes_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    target_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    canonical_origin: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class AutomationEnrollmentGrant(Base):
    __tablename__ = "automation_enrollment_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("automation_clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    code_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    redirect_uri: Mapped[str | None] = mapped_column(Text)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="S256"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(String(200), index=True)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    principal_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, default="api_token"
    )
    created_by: Mapped[str | None] = mapped_column(String(200))
    client_id: Mapped[str | None] = mapped_column(
        ForeignKey("automation_clients.id", ondelete="SET NULL"),
        index=True,
    )
    target_instance_id: Mapped[str | None] = mapped_column(String(36))
    canonical_origin: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    traceparent: Mapped[str | None] = mapped_column(String(80))
    principal_subject: Mapped[str] = mapped_column(
        String(200), nullable=False, index=True
    )
    principal_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    action: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(120), index=True)
    plan_digest: Mapped[str | None] = mapped_column(String(128))
    resource_kind: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(160), index=True)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class ApiIdempotency(Base):
    __tablename__ = "api_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "principal_subject",
            "operation_id",
            "idempotency_key",
            name="uq_api_idempotency_principal_operation_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_subject: Mapped[str] = mapped_column(String(200), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="in_progress"
    )
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resource_kind: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(160), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class WorkflowExecutionPlan(Base):
    __tablename__ = "workflow_execution_plans"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_id,
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    principal_subject: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        index=True,
    )
    target_instance_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    plan_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    state_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    required_confirmations_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    resulting_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )


class CapabilitySnapshot(Base):
    __tablename__ = "capability_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


Index(
    "idx_segments_revision_timing",
    Segment.revision_id,
    Segment.start_ms,
    Segment.end_ms,
)
Index(
    "idx_jobs_session_kind_created",
    Job.session_id,
    Job.kind,
    Job.created_at.desc(),
    Job.id.desc(),
)
Index(
    "idx_jobs_status_created",
    Job.status,
    Job.created_at,
    Job.id,
)
Index(
    "idx_artifacts_session_role_created",
    Artifact.session_id,
    Artifact.role,
    Artifact.created_at.desc(),
    Artifact.id.desc(),
)
Index(
    "idx_artifact_edges_child_parent",
    ArtifactEdge.child_artifact_id,
    ArtifactEdge.parent_artifact_id,
)
Index(
    "idx_audio_takes_active_status_segment_created",
    AudioTake.is_active,
    AudioTake.status,
    AudioTake.generation_segment_id,
    AudioTake.created_at.desc(),
    AudioTake.id.desc(),
)
Index(
    "idx_session_sources_current_updated",
    SessionSource.session_id,
    SessionSource.is_current.desc(),
    SessionSource.updated_at.desc(),
    SessionSource.id,
)
Index(
    "idx_usage_events_session_stage_created",
    UsageEvent.session_id,
    UsageEvent.stage,
    UsageEvent.created_at.desc(),
    UsageEvent.id.desc(),
)
Index(
    "idx_output_assemblies_session_created",
    OutputAssembly.session_id,
    OutputAssembly.created_at.desc(),
    OutputAssembly.id.desc(),
)
