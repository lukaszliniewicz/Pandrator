"""Index the child side of segment lineage lookups and cascades."""

import sqlalchemy as sa
from alembic import op

revision = "0057_segment_lineage_child_index"
down_revision = "0056_project_sound_plans"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_segment_lineage_child_segment_id"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("segment_lineage") and not any(
        index["name"] == INDEX_NAME
        for index in inspector.get_indexes("segment_lineage")
    ):
        op.create_index(
            INDEX_NAME,
            "segment_lineage",
            ["child_segment_id"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("segment_lineage") and any(
        index["name"] == INDEX_NAME
        for index in inspector.get_indexes("segment_lineage")
    ):
        op.drop_index(INDEX_NAME, table_name="segment_lineage")
