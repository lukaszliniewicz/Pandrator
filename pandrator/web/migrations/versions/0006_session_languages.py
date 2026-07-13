"""Store the source and optional target language on each session."""

from alembic import op
import sqlalchemy as sa


revision = "0006_session_languages"
down_revision = "0005_generation_segment_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    with op.batch_alter_table("sessions") as batch:
        if "source_language" not in columns:
            batch.add_column(sa.Column("source_language", sa.String(length=40), nullable=False, server_default="auto"))
        if "target_language" not in columns:
            batch.add_column(sa.Column("target_language", sa.String(length=40), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    with op.batch_alter_table("sessions") as batch:
        if "target_language" in columns:
            batch.drop_column("target_language")
        if "source_language" in columns:
            batch.drop_column("source_language")
