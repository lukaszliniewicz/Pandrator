"""Add explicit per-stage artifact selections."""

from alembic import op
import sqlalchemy as sa


revision = "0013_stage_artifact_selections"
down_revision = "0012_inactive_provider_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_stage_selections" in tables:
        return
    op.create_table(
        "session_stage_selections",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("stage_key", sa.String(length=80), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("session_id", "stage_key"),
    )
    op.create_index(
        "ix_session_stage_selections_artifact_id",
        "session_stage_selections",
        ["artifact_id"],
        unique=False,
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "session_stage_selections" in tables:
        op.drop_table("session_stage_selections")
