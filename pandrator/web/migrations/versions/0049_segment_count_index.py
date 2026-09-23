"""Cover completed-segment progress counts by plan revision."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_segment_count_index"
down_revision = "0048_voice_collections"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_generation_segments_revision_removed_status"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("generation_segments") and not any(
        index["name"] == INDEX_NAME
        for index in inspector.get_indexes("generation_segments")
    ):
        op.create_index(
            INDEX_NAME,
            "generation_segments",
            ["plan_revision_id", "removed", "status"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("generation_segments") and any(
        index["name"] == INDEX_NAME
        for index in inspector.get_indexes("generation_segments")
    ):
        op.drop_index(INDEX_NAME, table_name="generation_segments")
