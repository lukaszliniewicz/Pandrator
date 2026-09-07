"""Persist passive whole-recording media-edit dispatch runs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_media_edit_dispatch"
down_revision = "0041_media_edit_plans"
branch_labels = None
depends_on = None


def _indexes(connection: sa.Connection, table_name: str) -> set[str]:
    if table_name not in sa.inspect(connection).get_table_names():
        return set()
    return {
        str(index.get("name"))
        for index in sa.inspect(connection).get_indexes(table_name)
    }


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "media_edit_dispatch_runs" not in tables:
        op.create_table(
            "media_edit_dispatch_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(length=36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_revision_id",
                sa.String(length=36),
                sa.ForeignKey("media_edit_plan_revisions.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("source_revision_number", sa.Integer(), nullable=False),
            sa.Column("source_content_hash", sa.String(length=128), nullable=False),
            sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("settings_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="ready",
            ),
            sa.Column("batch_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "completed_batch_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "result_revision_id",
                sa.String(length=36),
                sa.ForeignKey("media_edit_plan_revisions.id", ondelete="SET NULL"),
            ),
            sa.Column("error_code", sa.String(length=120)),
            sa.Column("error_message", sa.Text()),
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
    if "media_edit_dispatch_batches" not in tables:
        op.create_table(
            "media_edit_dispatch_batches",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "dispatch_run_id",
                sa.String(length=36),
                sa.ForeignKey("media_edit_dispatch_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("input_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="ready",
            ),
            sa.Column("lease_token", sa.String(length=160)),
            sa.Column("claim_key", sa.String(length=200)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("normalized_output_json", sa.JSON()),
            sa.Column("output_hash", sa.String(length=64)),
            sa.Column("submission_key", sa.String(length=200)),
            sa.Column("accepted_at", sa.DateTime(timezone=True)),
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
                name="uq_media_edit_dispatch_batch_ordinal",
            ),
        )
    existing = _indexes(connection, "media_edit_dispatch_runs")
    for name, column in (
        ("ix_media_edit_dispatch_runs_session_id", "session_id"),
        ("ix_media_edit_dispatch_runs_source_revision_id", "source_revision_id"),
        ("ix_media_edit_dispatch_runs_result_revision_id", "result_revision_id"),
        ("ix_media_edit_dispatch_runs_status", "status"),
        ("ix_media_edit_dispatch_runs_created_at", "created_at"),
        ("ix_media_edit_dispatch_runs_updated_at", "updated_at"),
    ):
        if name not in existing:
            op.create_index(name, "media_edit_dispatch_runs", [column], unique=False)
    existing = _indexes(connection, "media_edit_dispatch_batches")
    for name, column in (
        ("ix_media_edit_dispatch_batches_dispatch_run_id", "dispatch_run_id"),
        ("ix_media_edit_dispatch_batches_status", "status"),
        ("ix_media_edit_dispatch_batches_lease_expires_at", "lease_expires_at"),
    ):
        if name not in existing:
            op.create_index(name, "media_edit_dispatch_batches", [column], unique=False)


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "media_edit_dispatch_batches" in tables:
        op.drop_table("media_edit_dispatch_batches")
    if "media_edit_dispatch_runs" in tables:
        op.drop_table("media_edit_dispatch_runs")
