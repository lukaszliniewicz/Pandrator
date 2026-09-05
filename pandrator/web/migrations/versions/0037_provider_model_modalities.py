"""Store the input and output modalities supported by provider models."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_provider_model_modalities"
down_revision = "0036_subtitle_evidence"
branch_labels = None
depends_on = None

_MODALITY_COLUMNS = ("input_modalities_json", "output_modalities_json")
_DEFAULT_MODALITIES = '["text"]'


def _columns(connection: sa.Connection) -> set[str]:
    inspector = sa.inspect(connection)
    if "provider_models" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("provider_models")}


def upgrade() -> None:
    connection = op.get_bind()
    columns = _columns(connection)
    if not columns:
        return

    missing = [name for name in _MODALITY_COLUMNS if name not in columns]
    if missing:
        with op.batch_alter_table("provider_models") as batch:
            for name in missing:
                batch.add_column(
                    sa.Column(
                        name,
                        sa.JSON(),
                        nullable=False,
                        server_default=sa.text(f"'{_DEFAULT_MODALITIES}'"),
                    )
                )

    # Keep this explicit even though the server default covers newly added
    # columns: it also repairs nullable columns left by a partial migration.
    for name in _MODALITY_COLUMNS:
        connection.execute(
            sa.text(
                f"UPDATE provider_models SET {name} = :default WHERE {name} IS NULL"
            ),
            {"default": _DEFAULT_MODALITIES},
        )

    nullable = {
        column["name"]
        for column in sa.inspect(connection).get_columns("provider_models")
        if column["name"] in _MODALITY_COLUMNS and column["nullable"]
    }
    if nullable:
        with op.batch_alter_table("provider_models") as batch:
            for name in nullable:
                batch.alter_column(
                    name,
                    existing_type=sa.JSON(),
                    existing_nullable=True,
                    nullable=False,
                )


def downgrade() -> None:
    connection = op.get_bind()
    columns = _columns(connection)
    removable = [name for name in _MODALITY_COLUMNS if name in columns]
    if not removable:
        return
    with op.batch_alter_table("provider_models") as batch:
        for name in removable:
            batch.drop_column(name)
