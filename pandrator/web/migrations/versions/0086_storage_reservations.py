"""Lease-fenced estimates for concurrent output storage admission."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0086_storage_reservations"
down_revision = "0085_project_speech_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_reservations",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("filesystem_key", sa.String(length=128), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reserved_bytes >= 0", name="ck_storage_reservations_bytes"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "filesystem_key"),
    )


def downgrade() -> None:
    # The migration environment holds BEGIN IMMEDIATE before this lease read.
    statement = sa.text(
        "SELECT 1 FROM storage_reservations AS r JOIN jobs AS j ON j.id = r.job_id "
        "WHERE j.status IN ('running', 'cancel_requested') AND j.lease_owner = r.lease_owner "
        "AND j.lease_generation = r.lease_generation AND j.lease_expires_at > :now LIMIT 1"
    ).bindparams(sa.bindparam("now", type_=sa.DateTime(timezone=True)))
    if op.get_bind().execute(statement, {"now": datetime.now(UTC)}).first():
        raise RuntimeError("Cannot discard live storage reservations.")
    op.drop_table("storage_reservations")
