"""Store reviewable per-segment LLM speech optimization."""

from alembic import op
import sqlalchemy as sa


revision = "0007_generation_optimization"
down_revision = "0006_session_languages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    segment_columns = {column["name"] for column in inspector.get_columns("generation_segments")}
    with op.batch_alter_table("generation_segments") as batch:
        if "optimized_text" not in segment_columns:
            batch.add_column(sa.Column("optimized_text", sa.Text(), nullable=True))
        if "optimization_status" not in segment_columns:
            batch.add_column(sa.Column("optimization_status", sa.String(length=32), nullable=False, server_default="not_requested"))
        if "optimization_source_hash" not in segment_columns:
            batch.add_column(sa.Column("optimization_source_hash", sa.String(length=128), nullable=True))
        if "optimization_reviewed" not in segment_columns:
            batch.add_column(sa.Column("optimization_reviewed", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "optimization_model" not in segment_columns:
            batch.add_column(sa.Column("optimization_model", sa.String(length=255), nullable=True))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("generation_segments")}
    if "ix_generation_segments_optimization_status" not in indexes:
        op.create_index("ix_generation_segments_optimization_status", "generation_segments", ["optimization_status"])

    revision_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("generation_segment_revisions")}
    with op.batch_alter_table("generation_segment_revisions") as batch:
        if "optimized_text" not in revision_columns:
            batch.add_column(sa.Column("optimized_text", sa.Text(), nullable=True))
        if "optimization_status" not in revision_columns:
            batch.add_column(sa.Column("optimization_status", sa.String(length=32), nullable=False, server_default="not_requested"))
        if "optimization_reviewed" not in revision_columns:
            batch.add_column(sa.Column("optimization_reviewed", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("generation_segments")}
    if "ix_generation_segments_optimization_status" in indexes:
        op.drop_index("ix_generation_segments_optimization_status", table_name="generation_segments")
    with op.batch_alter_table("generation_segment_revisions") as batch:
        for name in ("optimization_reviewed", "optimization_status", "optimized_text"):
            batch.drop_column(name)
    with op.batch_alter_table("generation_segments") as batch:
        for name in ("optimization_model", "optimization_reviewed", "optimization_source_hash", "optimization_status", "optimized_text"):
            batch.drop_column(name)
