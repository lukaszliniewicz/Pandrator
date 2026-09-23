"""Make newly added provider models opt-in without changing existing choices."""

import sqlalchemy as sa
from alembic import op

revision = "0012_inactive_provider_models"
down_revision = "0011_active_provider_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_models") as batch:
        batch.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )


def downgrade() -> None:
    with op.batch_alter_table("provider_models") as batch:
        batch.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.true(),
        )
