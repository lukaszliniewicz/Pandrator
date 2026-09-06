"""Persist immutable media-edit plans and revisions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_media_edit_plans"
down_revision = "0040_generation_plan_provenance"
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
    if "media_edit_plans" not in tables:
        op.create_table(
            "media_edit_plans",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(length=36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("active_revision_id", sa.String(length=36)),
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
            sa.UniqueConstraint("session_id", name="uq_media_edit_plan_session"),
        )
    if "media_edit_plan_revisions" not in tables:
        op.create_table(
            "media_edit_plan_revisions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "plan_id",
                sa.String(length=36),
                sa.ForeignKey("media_edit_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "parent_revision_id",
                sa.String(length=36),
                sa.ForeignKey("media_edit_plan_revisions.id", ondelete="SET NULL"),
            ),
            sa.Column("revision_number", sa.Integer(), nullable=False),
            sa.Column(
                "source_media_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "editorial_transcript_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "timing_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
            ),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("keep_ranges_json", sa.JSON(), nullable=False),
            sa.Column("cues_json", sa.JSON(), nullable=False),
            sa.Column("evidence_json", sa.JSON(), nullable=False),
            sa.Column("operation_json", sa.JSON(), nullable=False),
            sa.Column(
                "reviewed", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("content_hash", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.UniqueConstraint(
                "plan_id", "revision_number", name="uq_media_edit_plan_revision"
            ),
        )

    existing = _indexes(connection, "media_edit_plans")
    if "ix_media_edit_plans_session_id" not in existing:
        op.create_index(
            "ix_media_edit_plans_session_id",
            "media_edit_plans",
            ["session_id"],
            unique=False,
        )
    existing = _indexes(connection, "media_edit_plan_revisions")
    for name, column in (
        ("ix_media_edit_plan_revisions_plan_id", "plan_id"),
        ("ix_media_edit_plan_revisions_parent_revision_id", "parent_revision_id"),
        (
            "ix_media_edit_plan_revisions_source_media_artifact_id",
            "source_media_artifact_id",
        ),
        (
            "ix_media_edit_plan_revisions_editorial_transcript_artifact_id",
            "editorial_transcript_artifact_id",
        ),
        ("ix_media_edit_plan_revisions_timing_artifact_id", "timing_artifact_id"),
    ):
        if name not in existing:
            op.create_index(name, "media_edit_plan_revisions", [column], unique=False)


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "media_edit_plan_revisions" in tables:
        op.drop_table("media_edit_plan_revisions")
    if "media_edit_plans" in tables:
        op.drop_table("media_edit_plans")
