"""Persist completed resumable-upload results for safe replay.

Revision ID: 0034_replayable_upload_results
Revises: 0033_speech_optimization_dispatch
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_replayable_upload_results"
down_revision = "0033_speech_optimization_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("upload_sessions")}
    if "result_json" not in columns:
        with op.batch_alter_table("upload_sessions") as batch:
            batch.add_column(sa.Column("result_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("upload_sessions")}
    if "result_json" in columns:
        with op.batch_alter_table("upload_sessions") as batch:
            batch.drop_column("result_json")
