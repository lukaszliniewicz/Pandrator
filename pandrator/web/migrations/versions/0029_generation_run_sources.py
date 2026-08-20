"""Track the immutable source run used for targeted regeneration.

Revision ID: 0029_generation_run_sources
Revises: 0028_repair_fork_translation_sources
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_generation_run_sources"
down_revision = "0028_repair_fork_translation_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("generation_runs")
    }
    if "source_generation_run_id" in columns:
        return
    with op.batch_alter_table("generation_runs") as batch:
        batch.add_column(sa.Column("source_generation_run_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_generation_runs_source_generation_run_id",
            "generation_runs",
            ["source_generation_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_generation_runs_source_generation_run_id",
            ["source_generation_run_id"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("generation_runs")
    }
    if "source_generation_run_id" not in columns:
        return
    with op.batch_alter_table("generation_runs") as batch:
        batch.drop_index("ix_generation_runs_source_generation_run_id")
        batch.drop_column("source_generation_run_id")
