"""Track the visible output run that owns grouped regeneration takes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_generation_run_output_owner"
down_revision = "0044_tts_provider_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("generation_runs")}
    if "output_generation_run_id" in columns:
        return
    with op.batch_alter_table("generation_runs") as batch:
        batch.add_column(
            sa.Column("output_generation_run_id", sa.String(length=36), nullable=True)
        )
        batch.create_foreign_key(
            "fk_generation_runs_output_generation_run_id",
            "generation_runs",
            ["output_generation_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index(
            "ix_generation_runs_output_generation_run_id",
            ["output_generation_run_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("generation_runs")}
    if "output_generation_run_id" not in columns:
        return
    with op.batch_alter_table("generation_runs") as batch:
        batch.drop_index("ix_generation_runs_output_generation_run_id")
        batch.drop_column("output_generation_run_id")
