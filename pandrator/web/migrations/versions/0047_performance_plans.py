"""Contextual performance sidecars and resumable leased analysis batches."""

from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision = "0047_performance_plans"
down_revision = "0046_speech_plan_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("performance_plans"):
        op.create_table(
            "performance_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "plan_revision_id",
                sa.String(36),
                sa.ForeignKey("generation_plan_revisions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("base_signature", sa.String(128), nullable=False),
            sa.Column("settings_json", sa.JSON(), nullable=False),
            sa.Column("units_json", sa.JSON(), nullable=False),
            sa.Column("manual_annotations_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.String(36)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("adopted_at", sa.DateTime(timezone=True)),
        )
        for key in ("session_id", "plan_revision_id", "status"):
            op.create_index(f"ix_performance_plans_{key}", "performance_plans", [key])
    if not sa.inspect(op.get_bind()).has_table("performance_batches"):
        op.create_table(
            "performance_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "performance_plan_id",
                sa.String(36),
                sa.ForeignKey("performance_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("segment_ids_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("lease_token", sa.String(64)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("annotations_json", sa.JSON(), nullable=False),
            sa.Column("response_hash", sa.String(128)),
            sa.Column("usage_json", sa.JSON(), nullable=False),
            sa.UniqueConstraint(
                "performance_plan_id", "ordinal", name="uq_performance_batch_ordinal"
            ),
        )
        for key in ("performance_plan_id", "status"):
            op.create_index(
                f"ix_performance_batches_{key}", "performance_batches", [key]
            )


def downgrade() -> None:
    op.drop_table("performance_batches")
    op.drop_table("performance_plans")
