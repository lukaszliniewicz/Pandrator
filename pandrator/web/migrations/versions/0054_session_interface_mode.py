"""Store the per-session interface mode."""

import sqlalchemy as sa
from alembic import op

revision = "0054_session_interface_mode"
down_revision = "0053_generation_audio_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("sessions")
    }
    if "interface_mode" not in columns:
        op.add_column(
            "sessions",
            sa.Column(
                "interface_mode",
                sa.String(16),
                nullable=False,
                server_default="advanced",
            ),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("sessions")
    }
    if "interface_mode" in columns:
        op.drop_column("sessions", "interface_mode")
