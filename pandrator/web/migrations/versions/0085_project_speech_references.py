"""Persist selected speech-reference metadata for project sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0085_project_speech_references"
down_revision = "0084_job_external_effects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_speech_references",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("slot_key", sa.String(length=512), nullable=False),
        sa.Column("selector_json", sa.JSON(), nullable=False),
        sa.Column("selector_hash", sa.String(length=64), nullable=False),
        sa.Column("binding_json", sa.JSON(), nullable=False),
        sa.Column("input_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("input_content_hash", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1", name="ck_project_speech_references_revision"
        ),
        sa.CheckConstraint(
            "(input_artifact_id IS NULL AND input_content_hash IS NULL) "
            "OR (input_artifact_id IS NOT NULL AND input_content_hash IS NOT NULL)",
            name="ck_project_speech_references_input_pin_pair",
        ),
        sa.CheckConstraint(
            "length(selector_hash) = 64",
            name="ck_project_speech_references_selector_hash_length",
        ),
        sa.CheckConstraint(
            "input_content_hash IS NULL OR length(input_content_hash) = 64",
            name="ck_project_speech_references_input_hash_length",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["input_artifact_id"], ["artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("session_id", "slot_key"),
    )
    op.create_index(
        "ix_project_speech_references_input_artifact_id",
        "project_speech_references",
        ["input_artifact_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM project_speech_references LIMIT 1")
    ).first():
        raise RuntimeError("Cannot discard saved project speech references.")
    op.drop_index(
        "ix_project_speech_references_input_artifact_id",
        table_name="project_speech_references",
    )
    op.drop_table("project_speech_references")
