"""Preserve paragraph boundaries in generation plans."""

from alembic import op
import sqlalchemy as sa


revision = "0008_generation_paragraph_boundaries"
down_revision = "0007_generation_optimization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("generation_segments", "generation_segment_revisions"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "paragraph_break_after" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(
                    sa.Column("paragraph_break_after", sa.Boolean(), nullable=False, server_default=sa.false())
                )

    # Older plans did not retain the explicit boundary flag. A long saved
    # pause is the best available conservative signal for paragraph endings.
    connection.execute(
        sa.text(
            "UPDATE generation_segments SET paragraph_break_after = 1 "
            "WHERE silence_after_ms >= 500 OR node_kind IN ('heading', 'chapter_marker')"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE generation_segment_revisions SET paragraph_break_after = 1 "
            "WHERE silence_after_ms >= 500 OR node_kind IN ('heading', 'chapter_marker')"
        )
    )


def downgrade() -> None:
    for table in ("generation_segment_revisions", "generation_segments"):
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        if "paragraph_break_after" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("paragraph_break_after")
