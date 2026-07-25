"""Persist stable runtime capability probes.

Revision ID: 0020_capability_snapshots
Revises: 0019_job_lease_fencing
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_capability_snapshots"
down_revision = "0019_job_lease_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "capability_snapshots" not in sa.inspect(connection).get_table_names():
        op.create_table(
            "capability_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    indexes = {
        index["name"]
        for index in sa.inspect(connection).get_indexes("capability_snapshots")
    }
    if "ix_capability_snapshots_created_at" not in indexes:
        op.create_index(
            "ix_capability_snapshots_created_at",
            "capability_snapshots",
            ["created_at"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    if "capability_snapshots" in sa.inspect(connection).get_table_names():
        indexes = {
            index["name"]
            for index in sa.inspect(connection).get_indexes("capability_snapshots")
        }
        if "ix_capability_snapshots_created_at" in indexes:
            op.drop_index(
                "ix_capability_snapshots_created_at",
                table_name="capability_snapshots",
            )
        op.drop_table("capability_snapshots")
