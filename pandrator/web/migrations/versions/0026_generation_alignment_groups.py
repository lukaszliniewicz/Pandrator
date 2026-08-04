"""Persist explicit generation-to-timing alignment groups.

Revision ID: 0026_generation_alignment_groups
Revises: 0025_workflow_execution_plans
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_generation_alignment_groups"
down_revision = "0025_workflow_execution_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("generation_segments", "generation_segment_revisions"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "alignment_group" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("alignment_group", sa.String(length=64), nullable=True))
    indexes = {index["name"] for index in sa.inspect(connection).get_indexes("generation_segments")}
    if "ix_generation_segments_alignment_group" not in indexes:
        op.create_index(
            "ix_generation_segments_alignment_group",
            "generation_segments",
            ["alignment_group"],
            unique=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(connection).get_indexes("generation_segments")}
    if "ix_generation_segments_alignment_group" in indexes:
        op.drop_index(
            "ix_generation_segments_alignment_group",
            table_name="generation_segments",
        )
    for table in ("generation_segment_revisions", "generation_segments"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "alignment_group" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("alignment_group")
