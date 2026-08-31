"""Add passive speech-optimisation dispatch runs.

Revision ID: 0033_speech_optimization_dispatch
Revises: 0032_source_cleaning_dispatch
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_speech_optimization_dispatch"
down_revision = "0032_source_cleaning_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "speech_optimization_dispatch_runs" not in tables:
        op.create_table(
            "speech_optimization_dispatch_runs",
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
                "source_state",
                sa.String(length=24),
                nullable=False,
                server_default="current",
            ),
            sa.Column("source_content_hash", sa.String(length=128), nullable=False),
            sa.Column("source_format", sa.String(length=16), nullable=False),
            sa.Column(
                "language", sa.String(length=40), nullable=False, server_default="auto"
            ),
            sa.Column("voice_language", sa.String(length=40), nullable=True),
            sa.Column("tts_service", sa.String(length=80), nullable=True),
            sa.Column("settings_json", sa.JSON(), nullable=False),
            sa.Column("selection_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "output_head_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "result_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "result_revision_id",
                sa.String(length=36),
                sa.ForeignKey("document_revisions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "status", sa.String(length=24), nullable=False, server_default="ready"
            ),
            sa.Column("batch_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "completed_batch_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("error_code", sa.String(length=120), nullable=True),
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
        )
        for name, column in (
            ("ix_speech_optimization_dispatch_runs_session_id", "session_id"),
            (
                "ix_speech_optimization_dispatch_runs_source_artifact_id",
                "source_artifact_id",
            ),
            (
                "ix_speech_optimization_dispatch_runs_output_head_artifact_id",
                "output_head_artifact_id",
            ),
            (
                "ix_speech_optimization_dispatch_runs_result_artifact_id",
                "result_artifact_id",
            ),
            (
                "ix_speech_optimization_dispatch_runs_result_revision_id",
                "result_revision_id",
            ),
            ("ix_speech_optimization_dispatch_runs_status", "status"),
            ("ix_speech_optimization_dispatch_runs_created_at", "created_at"),
            ("ix_speech_optimization_dispatch_runs_updated_at", "updated_at"),
        ):
            op.create_index(name, "speech_optimization_dispatch_runs", [column])

    tables = set(sa.inspect(connection).get_table_names())
    if "speech_optimization_dispatch_batches" not in tables:
        op.create_table(
            "speech_optimization_dispatch_batches",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "dispatch_run_id",
                sa.String(length=36),
                sa.ForeignKey(
                    "speech_optimization_dispatch_runs.id", ondelete="CASCADE"
                ),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("input_json", sa.JSON(), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "status", sa.String(length=24), nullable=False, server_default="ready"
            ),
            sa.Column("lease_token", sa.String(length=160), nullable=True),
            sa.Column("claim_key", sa.String(length=200), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("normalized_output_json", sa.JSON(), nullable=True),
            sa.Column("output_hash", sa.String(length=64), nullable=True),
            sa.Column("submission_key", sa.String(length=200), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint(
                "dispatch_run_id",
                "ordinal",
                name="uq_speech_optimization_dispatch_batch_ordinal",
            ),
        )
        for name, column in (
            (
                "ix_speech_optimization_dispatch_batches_dispatch_run_id",
                "dispatch_run_id",
            ),
            ("ix_speech_optimization_dispatch_batches_status", "status"),
            (
                "ix_speech_optimization_dispatch_batches_lease_expires_at",
                "lease_expires_at",
            ),
        ):
            op.create_index(name, "speech_optimization_dispatch_batches", [column])


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "speech_optimization_dispatch_batches" in tables:
        op.drop_table("speech_optimization_dispatch_batches")
    if "speech_optimization_dispatch_runs" in tables:
        op.drop_table("speech_optimization_dispatch_runs")
