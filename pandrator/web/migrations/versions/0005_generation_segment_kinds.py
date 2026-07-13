"""Preserve chapter and typed-node identity in generation plans."""

from alembic import op
import sqlalchemy as sa


revision = "0005_generation_segment_kinds"
down_revision = "0004_output_assembly_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("generation_segments", "generation_segment_revisions"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "node_kind" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("node_kind", sa.String(length=40), nullable=False, server_default="paragraph"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("generation_segment_revisions", "generation_segments"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "node_kind" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("node_kind")
