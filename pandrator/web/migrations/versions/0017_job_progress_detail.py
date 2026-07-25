"""Persist the latest human-readable job progress detail."""

from alembic import op
import sqlalchemy as sa


revision = "0017_job_progress_detail"
down_revision = "0016_agentic_research_and_speech_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("jobs")}
    if "progress_detail" not in columns:
        with op.batch_alter_table("jobs") as batch:
            batch.add_column(sa.Column("progress_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("jobs")}
    if "progress_detail" in columns:
        with op.batch_alter_table("jobs") as batch:
            batch.drop_column("progress_detail")
