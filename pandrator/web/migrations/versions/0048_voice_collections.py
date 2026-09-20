"""Store voice collections and provider-catalog overrides."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_voice_collections"
down_revision = "0047_performance_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("voice_collections"):
        op.create_table(
            "voice_collections",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("voice_collection_members"):
        op.create_table(
            "voice_collection_members",
            sa.Column(
                "collection_id",
                sa.String(36),
                sa.ForeignKey("voice_collections.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("voice_key", sa.String(1024), primary_key=True),
            sa.Column("voice_ref_json", sa.JSON(), nullable=False),
            sa.Column(
                "managed_voice_id",
                sa.String(36),
                sa.ForeignKey("voices.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("voice_catalog_overrides"):
        op.create_table(
            "voice_catalog_overrides",
            sa.Column("voice_key", sa.String(1024), primary_key=True),
            sa.Column("voice_ref_json", sa.JSON(), nullable=False),
            sa.Column("name", sa.String(255), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("voice_category", sa.String(20), nullable=True),
            sa.Column("profile_json", sa.JSON(), nullable=True),
            sa.Column(
                "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("voice_collection_members"):
        op.drop_table("voice_collection_members")
    if inspector.has_table("voice_collections"):
        op.drop_table("voice_collections")
    if inspector.has_table("voice_catalog_overrides"):
        op.drop_table("voice_catalog_overrides")
