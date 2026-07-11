"""Add durable XTTS training records."""

from alembic import op
import sqlalchemy as sa


revision = "0002_training_runs"
down_revision = "0001_web_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "training_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "training_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("voice_id", sa.String(length=36), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("source_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("output_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["output_artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voice_id"], ["voices.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(op.f("ix_training_runs_status"), "training_runs", ["status"], unique=False)
    op.create_index(op.f("ix_training_runs_voice_id"), "training_runs", ["voice_id"], unique=False)


def downgrade() -> None:
    if "training_runs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(op.f("ix_training_runs_voice_id"), table_name="training_runs")
    op.drop_index(op.f("ix_training_runs_status"), table_name="training_runs")
    op.drop_table("training_runs")
