"""Associate provider usage with jobs, artifacts, and generation runs."""

from alembic import op
import sqlalchemy as sa


revision = "0014_usage_event_links"
down_revision = "0013_stage_artifact_selections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("usage_events")}
    with op.batch_alter_table("usage_events") as batch:
        if "job_id" not in columns:
            batch.add_column(sa.Column("job_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_usage_events_job_id_jobs", "jobs", ["job_id"], ["id"], ondelete="SET NULL")
            batch.create_index("ix_usage_events_job_id", ["job_id"], unique=False)
        if "artifact_id" not in columns:
            batch.add_column(sa.Column("artifact_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_usage_events_artifact_id_artifacts", "artifacts", ["artifact_id"], ["id"], ondelete="SET NULL")
            batch.create_index("ix_usage_events_artifact_id", ["artifact_id"], unique=False)
        if "generation_run_id" not in columns:
            batch.add_column(sa.Column("generation_run_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key(
                "fk_usage_events_generation_run_id_generation_runs",
                "generation_runs",
                ["generation_run_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index("ix_usage_events_generation_run_id", ["generation_run_id"], unique=False)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("usage_events")}
    with op.batch_alter_table("usage_events") as batch:
        for column in ("generation_run_id", "artifact_id", "job_id"):
            if column in columns:
                batch.drop_index(f"ix_usage_events_{column}")
                batch.drop_column(column)
