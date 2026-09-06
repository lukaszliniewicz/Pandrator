"""Persist generation-plan revision lineage and speech-block provenance."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_generation_plan_provenance"
down_revision = "0039_subtitle_evidence_media_provenance"
branch_labels = None
depends_on = None


def _columns(connection: sa.Connection, table_name: str) -> set[str]:
    if table_name not in sa.inspect(connection).get_table_names():
        return set()
    return {column["name"] for column in sa.inspect(connection).get_columns(table_name)}


def _indexes(connection: sa.Connection, table_name: str) -> set[str]:
    if table_name not in sa.inspect(connection).get_table_names():
        return set()
    return {
        str(index.get("name"))
        for index in sa.inspect(connection).get_indexes(table_name)
    }


def upgrade() -> None:
    connection = op.get_bind()
    plan_columns = _columns(connection, "generation_plan_revisions")
    if plan_columns:
        with op.batch_alter_table("generation_plan_revisions") as batch:
            if "parent_revision_id" not in plan_columns:
                batch.add_column(sa.Column("parent_revision_id", sa.String(length=36)))
                batch.create_foreign_key(
                    "fk_generation_plan_revisions_parent_revision_id",
                    "generation_plan_revisions",
                    ["parent_revision_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch.create_index(
                    "ix_generation_plan_revisions_parent_revision_id",
                    ["parent_revision_id"],
                    unique=False,
                )
            if "operation_json" not in plan_columns:
                batch.add_column(
                    sa.Column(
                        "operation_json",
                        sa.JSON(),
                        nullable=False,
                        server_default=sa.text("'{}'"),
                    )
                )
        if "parent_revision_id" in plan_columns and (
            "ix_generation_plan_revisions_parent_revision_id"
            not in _indexes(connection, "generation_plan_revisions")
        ):
            with op.batch_alter_table("generation_plan_revisions") as batch:
                batch.create_index(
                    "ix_generation_plan_revisions_parent_revision_id",
                    ["parent_revision_id"],
                    unique=False,
                )
        if "operation_json" in _columns(connection, "generation_plan_revisions"):
            connection.execute(
                sa.text(
                    "UPDATE generation_plan_revisions SET operation_json = :default "
                    "WHERE operation_json IS NULL"
                ),
                {"default": "{}"},
            )
            if any(
                column["name"] == "operation_json" and column["nullable"]
                for column in sa.inspect(connection).get_columns(
                    "generation_plan_revisions"
                )
            ):
                with op.batch_alter_table("generation_plan_revisions") as batch:
                    batch.alter_column(
                        "operation_json",
                        existing_type=sa.JSON(),
                        existing_nullable=True,
                        nullable=False,
                    )

    segment_columns = _columns(connection, "generation_segments")
    if segment_columns and "speech_block_provenance_json" not in segment_columns:
        with op.batch_alter_table("generation_segments") as batch:
            batch.add_column(
                sa.Column(
                    "speech_block_provenance_json",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
    if "speech_block_provenance_json" in _columns(connection, "generation_segments"):
        connection.execute(
            sa.text(
                "UPDATE generation_segments SET speech_block_provenance_json = :default "
                "WHERE speech_block_provenance_json IS NULL"
            ),
            {"default": "{}"},
        )
        if any(
            column["name"] == "speech_block_provenance_json" and column["nullable"]
            for column in sa.inspect(connection).get_columns("generation_segments")
        ):
            with op.batch_alter_table("generation_segments") as batch:
                batch.alter_column(
                    "speech_block_provenance_json",
                    existing_type=sa.JSON(),
                    existing_nullable=True,
                    nullable=False,
                )

    revision_columns = _columns(connection, "generation_segment_revisions")
    if revision_columns:
        with op.batch_alter_table("generation_segment_revisions") as batch:
            if "ordinal" not in revision_columns:
                batch.add_column(sa.Column("ordinal", sa.Integer(), nullable=True))
            if "source_segment_ids_json" not in revision_columns:
                batch.add_column(
                    sa.Column(
                        "source_segment_ids_json",
                        sa.JSON(),
                        nullable=False,
                        server_default=sa.text("'[]'"),
                    )
                )
            if "speech_block_provenance_json" not in revision_columns:
                batch.add_column(
                    sa.Column(
                        "speech_block_provenance_json",
                        sa.JSON(),
                        nullable=False,
                        server_default=sa.text("'{}'"),
                    )
                )
        revision_columns = _columns(connection, "generation_segment_revisions")
        for name, default in (
            ("source_segment_ids_json", "[]"),
            ("speech_block_provenance_json", "{}"),
        ):
            if name not in revision_columns:
                continue
            connection.execute(
                sa.text(
                    f"UPDATE generation_segment_revisions SET {name} = :default "
                    f"WHERE {name} IS NULL"
                ),
                {"default": default},
            )
            if any(
                column["name"] == name and column["nullable"]
                for column in sa.inspect(connection).get_columns(
                    "generation_segment_revisions"
                )
            ):
                with op.batch_alter_table("generation_segment_revisions") as batch:
                    batch.alter_column(
                        name,
                        existing_type=sa.JSON(),
                        existing_nullable=True,
                        nullable=False,
                    )


def downgrade() -> None:
    connection = op.get_bind()
    revision_columns = _columns(connection, "generation_segment_revisions")
    if revision_columns:
        with op.batch_alter_table("generation_segment_revisions") as batch:
            for name in (
                "speech_block_provenance_json",
                "source_segment_ids_json",
                "ordinal",
            ):
                if name in revision_columns:
                    batch.drop_column(name)

    segment_columns = _columns(connection, "generation_segments")
    if segment_columns and "speech_block_provenance_json" in segment_columns:
        with op.batch_alter_table("generation_segments") as batch:
            batch.drop_column("speech_block_provenance_json")

    plan_columns = _columns(connection, "generation_plan_revisions")
    if plan_columns:
        with op.batch_alter_table("generation_plan_revisions") as batch:
            if "operation_json" in plan_columns:
                batch.drop_column("operation_json")
            if "parent_revision_id" in plan_columns:
                batch.drop_index("ix_generation_plan_revisions_parent_revision_id")
                batch.drop_column("parent_revision_id")
