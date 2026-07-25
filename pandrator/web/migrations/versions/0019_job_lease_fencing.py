"""Fence worker and resource mutations by lease generation.

Revision ID: 0019_job_lease_fencing
Revises: 0018_generation_segment_speaker
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_job_lease_fencing"
down_revision = "0018_generation_segment_speaker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    jobs_columns = {column["name"] for column in sa.inspect(connection).get_columns("jobs")}
    if "lease_generation" not in jobs_columns:
        with op.batch_alter_table("jobs") as batch:
            batch.add_column(
                sa.Column(
                    "lease_generation",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
    if "available_at" not in jobs_columns:
        with op.batch_alter_table("jobs") as batch:
            batch.add_column(sa.Column("available_at", sa.DateTime(timezone=True)))
    jobs_indexes = {index["name"] for index in sa.inspect(connection).get_indexes("jobs")}
    if "ix_jobs_available_at" not in jobs_indexes:
        op.create_index("ix_jobs_available_at", "jobs", ["available_at"])

    resource_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("resource_claims")
    }
    if "lease_generation" not in resource_columns:
        with op.batch_alter_table("resource_claims") as batch:
            batch.add_column(
                sa.Column(
                    "lease_generation",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    resource_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("resource_claims")
    }
    if "lease_generation" in resource_columns:
        with op.batch_alter_table("resource_claims") as batch:
            batch.drop_column("lease_generation")

    jobs_columns = {column["name"] for column in sa.inspect(connection).get_columns("jobs")}
    jobs_indexes = {index["name"] for index in sa.inspect(connection).get_indexes("jobs")}
    if "ix_jobs_available_at" in jobs_indexes:
        op.drop_index("ix_jobs_available_at", table_name="jobs")
    if "available_at" in jobs_columns:
        with op.batch_alter_table("jobs") as batch:
            batch.drop_column("available_at")
    if "lease_generation" in jobs_columns:
        with op.batch_alter_table("jobs") as batch:
            batch.drop_column("lease_generation")
