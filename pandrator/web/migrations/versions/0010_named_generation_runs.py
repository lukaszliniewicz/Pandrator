"""Add stable, human-readable generation run identity and take ownership."""

from alembic import op
import sqlalchemy as sa


revision = "0010_named_generation_runs"
down_revision = "0009_stored_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    run_columns = {column["name"] for column in inspector.get_columns("generation_runs")}
    if "sequence_number" not in run_columns:
        with op.batch_alter_table("generation_runs") as batch:
            batch.add_column(sa.Column("sequence_number", sa.Integer(), nullable=False, server_default="1"))
    if "operation" not in run_columns:
        with op.batch_alter_table("generation_runs") as batch:
            batch.add_column(sa.Column("operation", sa.String(length=32), nullable=False, server_default="generate"))

    # Preserve chronological order for existing sessions. SQLite supports the
    # correlated COUNT used here on every version supported by Pandrator.
    connection.execute(
        sa.text(
            "UPDATE generation_runs AS current SET sequence_number = ("
            "SELECT COUNT(*) FROM generation_runs AS earlier "
            "WHERE earlier.session_id = current.session_id AND ("
            "earlier.created_at < current.created_at OR "
            "(earlier.created_at = current.created_at AND earlier.id <= current.id)))"
        )
    )
    indexes = {item["name"] for item in sa.inspect(connection).get_indexes("generation_runs")}
    if "uq_generation_run_sequence" not in indexes:
        op.create_index(
            "uq_generation_run_sequence",
            "generation_runs",
            ["session_id", "sequence_number"],
            unique=True,
        )

    take_columns = {column["name"] for column in sa.inspect(connection).get_columns("audio_takes")}
    if "generation_run_id" not in take_columns:
        with op.batch_alter_table("audio_takes") as batch:
            batch.add_column(sa.Column("generation_run_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key(
                "fk_audio_takes_generation_run_id",
                "generation_runs",
                ["generation_run_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch.create_index("ix_audio_takes_generation_run_id", ["generation_run_id"])


def downgrade() -> None:
    take_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("audio_takes")}
    if "generation_run_id" in take_columns:
        with op.batch_alter_table("audio_takes") as batch:
            batch.drop_index("ix_audio_takes_generation_run_id")
            batch.drop_column("generation_run_id")
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("generation_runs")}
    if "uq_generation_run_sequence" in indexes:
        op.drop_index("uq_generation_run_sequence", table_name="generation_runs")
    run_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("generation_runs")}
    with op.batch_alter_table("generation_runs") as batch:
        if "operation" in run_columns:
            batch.drop_column("operation")
        if "sequence_number" in run_columns:
            batch.drop_column("sequence_number")
