"""Add durable subtitle-evidence requests and bounded STT provenance."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_subtitle_evidence"
down_revision = "0035_generation_resume_ownership"
branch_labels = None
depends_on = None


def _indexes(connection: sa.Connection) -> set[str]:
    return {
        str(item.get("name"))
        for item in sa.inspect(connection).get_indexes("subtitle_evidence")
    }


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "subtitle_evidence" not in tables:
        op.create_table(
            "subtitle_evidence",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(length=36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "source_revision_id",
                sa.String(length=36),
                sa.ForeignKey("document_revisions.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "source_segment_id",
                sa.String(length=36),
                sa.ForeignKey("segments.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("cue_id", sa.Integer(), nullable=False),
            sa.Column("start_ms", sa.Integer(), nullable=False),
            sa.Column("end_ms", sa.Integer(), nullable=False),
            sa.Column("clip_start_ms", sa.Integer(), nullable=False),
            sa.Column("clip_end_ms", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("routes_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column(
                "job_id",
                sa.String(length=36),
                sa.ForeignKey("jobs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "clip_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("candidates_json", sa.JSON(), nullable=False),
            sa.Column("resolution_json", sa.JSON(), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.UniqueConstraint("job_id", name="uq_subtitle_evidence_job_id"),
        )

    existing = _indexes(connection)
    for name, column in (
        ("ix_subtitle_evidence_session_id", "session_id"),
        ("ix_subtitle_evidence_source_artifact_id", "source_artifact_id"),
        ("ix_subtitle_evidence_source_revision_id", "source_revision_id"),
        ("ix_subtitle_evidence_source_segment_id", "source_segment_id"),
        ("ix_subtitle_evidence_status", "status"),
        ("ix_subtitle_evidence_job_id", "job_id"),
        ("ix_subtitle_evidence_clip_artifact_id", "clip_artifact_id"),
    ):
        if name not in existing:
            op.create_index(name, "subtitle_evidence", [column], unique=False)


def downgrade() -> None:
    connection = op.get_bind()
    if "subtitle_evidence" not in sa.inspect(connection).get_table_names():
        return
    for name in (
        "ix_subtitle_evidence_session_id",
        "ix_subtitle_evidence_source_artifact_id",
        "ix_subtitle_evidence_source_revision_id",
        "ix_subtitle_evidence_source_segment_id",
        "ix_subtitle_evidence_status",
        "ix_subtitle_evidence_job_id",
        "ix_subtitle_evidence_clip_artifact_id",
    ):
        if name in _indexes(connection):
            op.drop_index(name, table_name="subtitle_evidence")
    op.drop_table("subtitle_evidence")
