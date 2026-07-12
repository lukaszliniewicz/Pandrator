"""Track durable generation output assembly jobs and failures."""

from alembic import op
import sqlalchemy as sa


revision = "0004_output_assembly_jobs"
down_revision = "0003_parity_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("output_assemblies")}
    with op.batch_alter_table("output_assemblies") as batch:
        if "job_id" not in columns:
            batch.add_column(sa.Column("job_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_output_assemblies_job_id", "jobs", ["job_id"], ["id"], ondelete="SET NULL")
            batch.create_index("ix_output_assemblies_job_id", ["job_id"])
        if "settings_hash" not in columns:
            batch.add_column(sa.Column("settings_hash", sa.String(length=128), nullable=True))
        if "error_message" not in columns:
            batch.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        if "updated_at" not in columns:
            batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()))
    training_columns = {column["name"] for column in inspector.get_columns("training_runs")}
    if "source_text_artifact_id" not in training_columns:
        with op.batch_alter_table("training_runs") as batch:
            batch.add_column(sa.Column("source_text_artifact_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key(
                "fk_training_runs_source_text_artifact_id_artifacts",
                "artifacts",
                ["source_text_artifact_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    training_columns = {column["name"] for column in inspector.get_columns("training_runs")}
    if "source_text_artifact_id" in training_columns:
        foreign_keys = [
            item.get("name")
            for item in inspector.get_foreign_keys("training_runs")
            if item.get("constrained_columns") == ["source_text_artifact_id"] and item.get("name")
        ]
        with op.batch_alter_table("training_runs") as batch:
            for name in foreign_keys:
                batch.drop_constraint(name, type_="foreignkey")
            batch.drop_column("source_text_artifact_id")
    output_foreign_keys = [
        item.get("name")
        for item in inspector.get_foreign_keys("output_assemblies")
        if item.get("constrained_columns") == ["job_id"] and item.get("name")
    ]
    with op.batch_alter_table("output_assemblies") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("error_message")
        batch.drop_column("settings_hash")
        batch.drop_index("ix_output_assemblies_job_id")
        for name in output_foreign_keys:
            batch.drop_constraint(name, type_="foreignkey")
        batch.drop_column("job_id")
