"""Add session-independent sound-library items and variants."""

import sqlalchemy as sa
from alembic import op

revision = "0055_sound_library"
down_revision = "0054_session_interface_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sound_items"):
        op.create_table(
            "sound_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("tags_json", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("archived", sa.Boolean(), nullable=False),
            sa.Column(
                "preferred_variant_id",
                sa.String(36),
                sa.ForeignKey("sound_variants.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_sound_items_archived_created_at_id",
            "sound_items",
            ["archived", "created_at", "id"],
        )

    if not inspector.has_table("sound_variants"):
        op.create_table(
            "sound_variants",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "item_id",
                sa.String(36),
                sa.ForeignKey("sound_items.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column(
                "job_id",
                sa.String(36),
                sa.ForeignKey("jobs.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column(
                "artifact_id",
                sa.String(36),
                sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=True),
            sa.Column("provenance_json", sa.JSON(), nullable=False),
            sa.Column("sample_rate", sa.Integer(), nullable=True),
            sa.Column("channels", sa.Integer(), nullable=True),
            sa.Column("frame_count", sa.Integer(), nullable=True),
            sa.Column("duration_seconds", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_sound_variants_item_created_at_id",
            "sound_variants",
            ["item_id", "created_at", "id"],
        )
        op.create_index(
            "ix_sound_variants_fingerprint_state",
            "sound_variants",
            ["fingerprint", "state"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sound_variants"):
        op.drop_index(
            "ix_sound_variants_fingerprint_state", table_name="sound_variants"
        )
        op.drop_index(
            "ix_sound_variants_item_created_at_id", table_name="sound_variants"
        )
        op.drop_table("sound_variants")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sound_items"):
        op.drop_index(
            "ix_sound_items_archived_created_at_id", table_name="sound_items"
        )
        op.drop_table("sound_items")
