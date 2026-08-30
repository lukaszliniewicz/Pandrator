"""Track the durable output role of passive dispatch runs.

Revision ID: 0031_dispatch_output_roles
Revises: 0030_dispatch_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_dispatch_output_roles"
down_revision = "0030_dispatch_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "dispatch_runs" not in set(inspector.get_table_names()):
        return
    columns = {
        str(column["name"])
        for column in inspector.get_columns("dispatch_runs")
    }
    if "output_role" in columns:
        return
    with op.batch_alter_table("dispatch_runs") as batch:
        batch.add_column(
            sa.Column("output_role", sa.String(length=120), nullable=True)
        )
    connection.execute(
        sa.text("UPDATE dispatch_runs SET output_role = kind WHERE output_role IS NULL")
    )
    with op.batch_alter_table("dispatch_runs") as batch:
        batch.alter_column(
            "output_role",
            existing_type=sa.String(length=120),
            nullable=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "dispatch_runs" not in set(inspector.get_table_names()):
        return
    columns = {
        str(column["name"])
        for column in inspector.get_columns("dispatch_runs")
    }
    if "output_role" not in columns:
        return
    with op.batch_alter_table("dispatch_runs") as batch:
        batch.drop_column("output_role")
