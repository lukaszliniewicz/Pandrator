"""Add the transactional API idempotency ledger.

Revision ID: 0024_api_idempotency
Revises: 0023_scoped_api_principals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pandrator.web.models import Base

revision = "0024_api_idempotency"
down_revision = "0023_scoped_api_principals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "api_idempotency" not in sa.inspect(connection).get_table_names():
        Base.metadata.tables["api_idempotency"].create(bind=connection)


def downgrade() -> None:
    connection = op.get_bind()
    if "api_idempotency" in sa.inspect(connection).get_table_names():
        op.drop_table("api_idempotency")
