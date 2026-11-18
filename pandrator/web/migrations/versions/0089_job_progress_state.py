"""Carry structured generation progress beside the human job detail.

Revision ID: 0089_job_progress_state
Revises: 0088_translation_branch_audio_mode
"""

import sqlalchemy as sa
from alembic import op

revision = "0089_job_progress_state"
down_revision = "0088_translation_branch_audio_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("progress_state_json", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    # The column is a live view of one attempt's progress, so a downgrade does
    # not need to refuse while a job is running: dropping it cannot lose a
    # durable result.
    op.drop_column("jobs", "progress_state_json")
