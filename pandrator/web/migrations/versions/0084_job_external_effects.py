"""Retain provider side-effect receipts independently of job completion."""

import sqlalchemy as sa
from alembic import op

revision = "0084_job_external_effects"
down_revision = "0083_workspace_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("external_effect_json", sa.JSON(none_as_null=True), nullable=True)
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM jobs WHERE external_effect_json IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError("Cannot discard retained provider effect receipts.")
    # Native SQLite DROP COLUMN preserves the ownership triggers on jobs. Batch
    # recreation would silently discard those triggers.
    op.drop_column("jobs", "external_effect_json")
