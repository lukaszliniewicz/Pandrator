"""Add immutable workflow execution plans.

Revision ID: 0025_workflow_execution_plans
Revises: 0024_api_idempotency
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pandrator.web.models import Base

revision = "0025_workflow_execution_plans"
down_revision = "0024_api_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if (
        "workflow_execution_plans"
        not in sa.inspect(connection).get_table_names()
    ):
        Base.metadata.tables["workflow_execution_plans"].create(
            bind=connection
        )


def downgrade() -> None:
    connection = op.get_bind()
    if (
        "workflow_execution_plans"
        in sa.inspect(connection).get_table_names()
    ):
        op.drop_table("workflow_execution_plans")
