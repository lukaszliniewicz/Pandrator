"""Add durable session purge journal and per-session trash deadlines."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_session_purges"
down_revision = "0049_segment_count_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    indexes = {index["name"] for index in inspector.get_indexes("sessions")}
    if "purge_after" not in columns or "ix_sessions_purge_after" not in indexes:
        with op.batch_alter_table("sessions") as batch:
            if "purge_after" not in columns:
                batch.add_column(sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
            if "ix_sessions_purge_after" not in indexes:
                batch.create_index("ix_sessions_purge_after", ["purge_after"])
    if inspector.has_table("session_purges"):
        return
    op.create_table(
        "session_purges",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("impact_token", sa.String(64), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_purges")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_index("ix_sessions_purge_after")
        batch.drop_column("purge_after")
