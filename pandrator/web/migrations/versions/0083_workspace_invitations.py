"""Account-targeted workspace invitations and guarded solo downgrade."""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0083_workspace_invitations"
down_revision = "0082_private_transcription_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "invited_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("inviter_auth_revision", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_workspace_invitations_role"),
        sa.CheckConstraint(
            "state IN ('pending', 'accepted', 'revoked', 'declined', 'expired')",
            name="ck_workspace_invitations_state",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_workspace_invitations_revision"),
        sa.CheckConstraint(
            "inviter_auth_revision >= 1", name="ck_workspace_invitations_inviter_auth_revision"
        ),
    )
    op.create_index(
        "ix_workspace_invitations_user_created", "workspace_invitations", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_workspace_invitations_workspace_state",
        "workspace_invitations",
        ["workspace_id", "state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM workspace_invitations LIMIT 1")).first() is not None:
        raise RuntimeError("Cannot downgrade workspace invitations while invitation records exist.")
    raw = bind.execute(
        sa.text("SELECT value_json FROM app_settings WHERE key = 'auth.installation'")
    ).scalar_one_or_none()
    if raw is not None:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(value, dict) and value.get("mode") == "collaboration":
            raise RuntimeError(
                "Cannot downgrade workspace invitations while collaboration is enabled."
            )
    op.drop_index("ix_workspace_invitations_workspace_state", table_name="workspace_invitations")
    op.drop_index("ix_workspace_invitations_user_created", table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
