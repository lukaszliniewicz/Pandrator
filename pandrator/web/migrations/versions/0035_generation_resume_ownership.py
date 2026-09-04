"""Track ownership of automatic generation resume.

Revision ID: 0035_generation_resume_ownership
Revises: 0034_replayable_upload_results
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_generation_resume_ownership"
down_revision = "0034_replayable_upload_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("generation_runs")}
    if "resume_source_on_completion" not in columns:
        with op.batch_alter_table("generation_runs") as batch:
            batch.add_column(
                sa.Column(
                    "resume_source_on_completion",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("generation_runs")}
    if "resume_source_on_completion" in columns:
        with op.batch_alter_table("generation_runs") as batch:
            batch.drop_column("resume_source_on_completion")
