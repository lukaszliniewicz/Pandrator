"""Pin the media artifact used by each subtitle-evidence request."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_subtitle_evidence_media_provenance"
down_revision = "0038_subtitle_evidence_audio_models"
branch_labels = None
depends_on = None


def _indexes(connection: sa.Connection) -> set[str]:
    return {
        str(item.get("name"))
        for item in sa.inspect(connection).get_indexes("subtitle_evidence")
    }


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "subtitle_evidence" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("subtitle_evidence")}
    if "source_media_artifact_id" not in columns:
        # Older, not-yet-run evidence requests cannot be backfilled safely: the
        # primary source may already have changed. Keep those rows nullable so
        # the worker fails closed instead of silently inspecting current media.
        with op.batch_alter_table("subtitle_evidence") as batch:
            batch.add_column(
                sa.Column("source_media_artifact_id", sa.String(length=36))
            )
            batch.create_foreign_key(
                "fk_subtitle_evidence_source_media_artifact_id_artifacts",
                "artifacts",
                ["source_media_artifact_id"],
                ["id"],
                ondelete="RESTRICT",
            )
    index_name = "ix_subtitle_evidence_source_media_artifact_id"
    if index_name not in _indexes(connection):
        op.create_index(
            index_name,
            "subtitle_evidence",
            ["source_media_artifact_id"],
            unique=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "subtitle_evidence" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("subtitle_evidence")}
    if "source_media_artifact_id" not in columns:
        return
    index_name = "ix_subtitle_evidence_source_media_artifact_id"
    if index_name in _indexes(connection):
        op.drop_index(index_name, table_name="subtitle_evidence")
    with op.batch_alter_table("subtitle_evidence") as batch:
        batch.drop_column("source_media_artifact_id")
