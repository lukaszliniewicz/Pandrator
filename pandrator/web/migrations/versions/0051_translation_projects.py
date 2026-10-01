"""Pin corrected source snapshots to independent language sessions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_translation_projects"
down_revision = "0050_session_purges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("translation_projects"):
        op.create_table(
            "translation_projects",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "source_session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="RESTRICT"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "checkpoint_artifact_id",
                sa.String(36),
                sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("source_content_hash", sa.String(128), nullable=False),
            sa.Column("source_language", sa.String(40), nullable=False),
            sa.Column("source_media_edit_revision_id", sa.String(36), nullable=True),
            sa.Column("source_media_edit_content_hash", sa.String(128), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("translation_project_branches"):
        op.create_table(
            "translation_project_branches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("translation_projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("target_language", sa.String(40), nullable=False),
            sa.Column(
                "source_checkpoint_artifact_id",
                sa.String(36),
                sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("source_content_hash", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "project_id", "target_language", name="uq_translation_project_language"
            ),
        )
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("translation_project_branches")}
    if "ix_translation_project_branches_project_id" not in indexes:
        op.create_index(
            "ix_translation_project_branches_project_id",
            "translation_project_branches",
            ["project_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_translation_project_branches_project_id", table_name="translation_project_branches"
    )
    op.drop_table("translation_project_branches")
    op.drop_table("translation_projects")
