"""Preserve structured speaker provenance on generation segments."""

from alembic import op
import sqlalchemy as sa


revision = "0018_generation_segment_speaker"
down_revision = "0017_job_progress_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("generation_segments", "generation_segment_revisions"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "speaker" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("speaker", sa.String(length=160), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("generation_segment_revisions", "generation_segments"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "speaker" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("speaker")
