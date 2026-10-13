"""Add ordered project folders and canonical session memberships."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_project_folders"
down_revision = "0057_segment_lineage_child_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("project_folders"):
        op.create_table(
            "project_folders",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column(
                "parent_id",
                sa.String(36),
                sa.ForeignKey("project_folders.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    inspector = sa.inspect(op.get_bind())
    if "ix_project_folders_parent_id" not in {
        index["name"] for index in inspector.get_indexes("project_folders")
    }:
        op.create_index(
            "ix_project_folders_parent_id", "project_folders", ["parent_id"]
        )
    if not inspector.has_table("project_folder_memberships"):
        op.create_table(
            "project_folder_memberships",
            sa.Column(
                "folder_id",
                sa.String(36),
                sa.ForeignKey("project_folders.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "project_session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.CheckConstraint(
                "position >= 0", name="ck_project_folder_membership_position"
            ),
        )
    inspector = sa.inspect(op.get_bind())
    if "ix_project_folder_memberships_project_session_id" not in {
        index["name"]
        for index in inspector.get_indexes("project_folder_memberships")
    }:
        op.create_index(
            "ix_project_folder_memberships_project_session_id",
            "project_folder_memberships",
            ["project_session_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("project_folder_memberships"):
        indexes = {
            index["name"]
            for index in inspector.get_indexes("project_folder_memberships")
        }
        if "ix_project_folder_memberships_project_session_id" in indexes:
            op.drop_index(
                "ix_project_folder_memberships_project_session_id",
                table_name="project_folder_memberships",
            )
        op.drop_table("project_folder_memberships")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("project_folders"):
        indexes = {
            index["name"] for index in inspector.get_indexes("project_folders")
        }
        if "ix_project_folders_parent_id" in indexes:
            op.drop_index("ix_project_folders_parent_id", table_name="project_folders")
        op.drop_table("project_folders")
