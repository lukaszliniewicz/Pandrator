"""Keep speech-plan review approval separate from immutable plan content."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_speech_plan_reviews"
down_revision = "0045_generation_run_output_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("speech_plan_reviews"):
        return
    op.create_table(
        "speech_plan_reviews",
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("generation_plan_revisions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("speech_plan_reviews")
