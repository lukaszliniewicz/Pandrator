"""Private expiring transcription jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0043_quick_transcriptions"
down_revision = "0042_media_edit_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "quick_transcriptions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "quick_transcriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("source_suffix", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_bytes", sa.Integer(), nullable=False),
        sa.Column("next_chunk_index", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_quick_transcriptions_owner_subject",
        "quick_transcriptions",
        ["owner_subject"],
    )
    op.create_index(
        "ix_quick_transcriptions_expires_at", "quick_transcriptions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("quick_transcriptions")
