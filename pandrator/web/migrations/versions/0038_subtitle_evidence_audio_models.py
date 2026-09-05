"""Store selected audio-capable LLM witnesses on subtitle evidence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_subtitle_evidence_audio_models"
down_revision = "0037_provider_model_modalities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "subtitle_evidence" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("subtitle_evidence")}
    if "audio_model_ids_json" not in columns:
        with op.batch_alter_table("subtitle_evidence") as batch:
            batch.add_column(
                sa.Column(
                    "audio_model_ids_json",
                    sa.JSON(),
                    nullable=False,
                    server_default="[]",
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "subtitle_evidence" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("subtitle_evidence")}
    if "audio_model_ids_json" in columns:
        with op.batch_alter_table("subtitle_evidence") as batch:
            batch.drop_column("audio_model_ids_json")
