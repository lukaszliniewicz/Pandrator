"""Add bounded research, pronunciation library, and structured speech plans."""

from alembic import op
import sqlalchemy as sa


revision = "0016_agentic_research_and_speech_plans"
down_revision = "0015_generation_segment_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    agent_columns = {
        column["name"] for column in sa.inspect(connection).get_columns("agent_runs")
    }
    if "kind" not in agent_columns:
        with op.batch_alter_table("agent_runs") as batch:
            batch.add_column(
                sa.Column(
                    "kind",
                    sa.String(length=80),
                    nullable=False,
                    server_default="source_cleaning",
                )
            )
            batch.create_index("ix_agent_runs_kind", ["kind"], unique=False)

    for table in ("generation_segments", "generation_segment_revisions"):
        columns = {
            column["name"] for column in sa.inspect(connection).get_columns(table)
        }
        if "speech_plan_json" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(
                    sa.Column(
                        "speech_plan_json",
                        sa.JSON(),
                        nullable=False,
                        server_default=sa.text("'{}'"),
                    )
                )

    tables = set(sa.inspect(connection).get_table_names())
    if "research_cache_entries" not in tables:
        op.create_table(
            "research_cache_entries",
            sa.Column("cache_key", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("operation", sa.String(length=40), nullable=False),
            sa.Column("request_json", sa.JSON(), nullable=False),
            sa.Column("response_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("cache_key"),
        )
        op.create_index(
            "ix_research_cache_entries_provider",
            "research_cache_entries",
            ["provider"],
            unique=False,
        )
        op.create_index(
            "ix_research_cache_entries_operation",
            "research_cache_entries",
            ["operation"],
            unique=False,
        )
        op.create_index(
            "ix_research_cache_entries_expires_at",
            "research_cache_entries",
            ["expires_at"],
            unique=False,
        )

    if "pronunciation_entries" not in tables:
        op.create_table(
            "pronunciation_entries",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.Column("source_form", sa.Text(), nullable=False),
            sa.Column("normalized_form", sa.String(length=512), nullable=False),
            sa.Column("language", sa.String(length=40), nullable=False),
            sa.Column("phonetic", sa.Text(), nullable=False),
            sa.Column("alphabet", sa.String(length=32), nullable=False),
            sa.Column("backend", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["sessions.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "scope",
                "session_id",
                "normalized_form",
                "language",
                "backend",
                name="uq_pronunciation_entry_identity",
            ),
        )
        for column in (
            "scope",
            "session_id",
            "normalized_form",
            "language",
            "backend",
            "status",
        ):
            op.create_index(
                f"ix_pronunciation_entries_{column}",
                "pronunciation_entries",
                [column],
                unique=False,
            )


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "pronunciation_entries" in tables:
        op.drop_table("pronunciation_entries")
    if "research_cache_entries" in tables:
        op.drop_table("research_cache_entries")

    for table in ("generation_segment_revisions", "generation_segments"):
        columns = {
            column["name"] for column in sa.inspect(connection).get_columns(table)
        }
        if "speech_plan_json" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("speech_plan_json")

    agent_columns = {
        column["name"] for column in sa.inspect(connection).get_columns("agent_runs")
    }
    if "kind" in agent_columns:
        with op.batch_alter_table("agent_runs") as batch:
            batch.drop_index("ix_agent_runs_kind")
            batch.drop_column("kind")
