"""Add immutable per-session sound plan versions and cues."""

import sqlalchemy as sa
from alembic import op

revision = "0056_project_sound_plans"
down_revision = "0055_sound_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("project_sound_plans"):
        op.create_table(
            "project_sound_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "speech_plan_revision_id",
                sa.String(36),
                sa.ForeignKey("generation_plan_revisions.id", ondelete="SET NULL"),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("session_id", "revision", name="uq_project_sound_plan_revision"),
            sa.CheckConstraint("revision >= 1", name="ck_project_sound_plan_revision_positive"),
        )
        op.create_index(
            "ix_project_sound_plans_session_revision",
            "project_sound_plans",
            ["session_id", "revision"],
        )
    if not inspector.has_table("project_sound_cues"):
        op.create_table(
            "project_sound_cues",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("logical_id", sa.String(36), nullable=False),
            sa.Column(
                "plan_id",
                sa.String(36),
                sa.ForeignKey("project_sound_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "variant_id",
                sa.String(36),
                sa.ForeignKey("sound_variants.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("anchor_json", sa.JSON(), nullable=False),
            sa.Column("playback_json", sa.JSON(), nullable=False),
            sa.Column("included", sa.Boolean(), nullable=False),
            sa.Column("placement_status", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("plan_id", "logical_id", name="uq_project_sound_cue_logical"),
            sa.CheckConstraint(
                "placement_status IN ('ready', 'needs_placement')",
                name="ck_project_sound_cue_placement",
            ),
        )
        op.create_index(
            "ix_project_sound_cues_plan_logical", "project_sound_cues", ["plan_id", "logical_id"]
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("project_sound_cues"):
        op.drop_table("project_sound_cues")
    if inspector.has_table("project_sound_plans"):
        op.drop_table("project_sound_plans")
