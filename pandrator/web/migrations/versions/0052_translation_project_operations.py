"""Durable selected-language operation previews and child receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0052_translation_project_operations"
down_revision = "0051_translation_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("translation_project_operations"):
        op.create_table(
            "translation_project_operations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("translation_projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("principal_subject", sa.String(255), nullable=False),
            sa.Column("target_instance_id", sa.String(255), nullable=False),
            sa.Column("action", sa.String(24), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("preview_digest", sa.String(64), nullable=False),
            sa.Column("preview_json", sa.JSON(), nullable=False),
            sa.Column("children_json", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_translation_project_operations_project_id",
            "translation_project_operations",
            ["project_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_translation_project_operations_project_id", table_name="translation_project_operations"
    )
    op.drop_table("translation_project_operations")
