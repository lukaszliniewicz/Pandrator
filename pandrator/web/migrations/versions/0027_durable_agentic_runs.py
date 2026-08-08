"""Persist resumable agentic work and structured knowledge.

Revision ID: 0027_durable_agentic_runs
Revises: 0026_generation_alignment_groups
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_durable_agentic_runs"
down_revision = "0026_generation_alignment_groups"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def _indexes(table: str) -> set[str]:
    return {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table)
    }


def _unique_constraints(table: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if constraint.get("name")
    }


def upgrade() -> None:
    provider_columns = _columns("provider_models")
    with op.batch_alter_table("provider_models") as batch:
        if "context_window_tokens" not in provider_columns:
            batch.add_column(
                sa.Column(
                    "context_window_tokens",
                    sa.Integer(),
                    nullable=False,
                    server_default="262144",
                )
            )
        if "max_output_tokens" not in provider_columns:
            batch.add_column(
                sa.Column("max_output_tokens", sa.Integer(), nullable=True)
            )

    run_columns = _columns("agent_runs")
    with op.batch_alter_table("agent_runs") as batch:
        if "source_content_hash" not in run_columns:
            batch.add_column(sa.Column("source_content_hash", sa.String(length=128)))
        if "settings_hash" not in run_columns:
            batch.add_column(sa.Column("settings_hash", sa.String(length=128)))
        if "checkpoint_revision" not in run_columns:
            batch.add_column(
                sa.Column(
                    "checkpoint_revision",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "error_message" not in run_columns:
            batch.add_column(sa.Column("error_message", sa.Text()))
    for name, column in (
        ("ix_agent_runs_source_content_hash", "source_content_hash"),
        ("ix_agent_runs_settings_hash", "settings_hash"),
    ):
        if name not in _indexes("agent_runs"):
            op.create_index(name, "agent_runs", [column], unique=False)

    step_columns = _columns("agent_steps")
    with op.batch_alter_table("agent_steps") as batch:
        if "unit_key" not in step_columns:
            batch.add_column(sa.Column("unit_key", sa.String(length=160)))
        if "input_hash" not in step_columns:
            batch.add_column(sa.Column("input_hash", sa.String(length=128)))
        if "updated_at" not in step_columns:
            batch.add_column(
                sa.Column(
                    "updated_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.current_timestamp(),
                )
            )
    if "uq_agent_step_unit_key" not in _unique_constraints("agent_steps"):
        with op.batch_alter_table("agent_steps") as batch:
            batch.create_unique_constraint(
                "uq_agent_step_unit_key", ["agent_run_id", "unit_key"]
            )

    usage_columns = _columns("usage_events")
    with op.batch_alter_table("usage_events") as batch:
        if "agent_run_id" not in usage_columns:
            batch.add_column(sa.Column("agent_run_id", sa.String(length=36)))
            batch.create_foreign_key(
                "fk_usage_events_agent_run_id",
                "agent_runs",
                ["agent_run_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "request_key" not in usage_columns:
            batch.add_column(sa.Column("request_key", sa.String(length=200)))
    if "ix_usage_events_agent_run_id" not in _indexes("usage_events"):
        op.create_index(
            "ix_usage_events_agent_run_id",
            "usage_events",
            ["agent_run_id"],
            unique=False,
        )
    if "uq_usage_event_agent_request" not in _unique_constraints("usage_events"):
        with op.batch_alter_table("usage_events") as batch:
            batch.create_unique_constraint(
                "uq_usage_event_agent_request", ["agent_run_id", "request_key"]
            )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_ledgers" not in tables:
        op.create_table(
            "knowledge_ledgers",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(length=36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column(
                "source_language",
                sa.String(length=40),
                nullable=False,
                server_default="auto",
            ),
            sa.Column(
                "target_language",
                sa.String(length=40),
                nullable=False,
                server_default="",
            ),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.UniqueConstraint(
                "session_id",
                "kind",
                "source_language",
                "target_language",
                name="uq_knowledge_ledger_scope",
            ),
        )
        op.create_index(
            "ix_knowledge_ledgers_session_id",
            "knowledge_ledgers",
            ["session_id"],
            unique=False,
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_ledgers" in tables:
        op.drop_index(
            "ix_knowledge_ledgers_session_id",
            table_name="knowledge_ledgers",
        )
        op.drop_table("knowledge_ledgers")

    if "uq_usage_event_agent_request" in _unique_constraints("usage_events"):
        with op.batch_alter_table("usage_events") as batch:
            batch.drop_constraint("uq_usage_event_agent_request", type_="unique")
    if "ix_usage_events_agent_run_id" in _indexes("usage_events"):
        op.drop_index("ix_usage_events_agent_run_id", table_name="usage_events")
    usage_columns = _columns("usage_events")
    with op.batch_alter_table("usage_events") as batch:
        if "request_key" in usage_columns:
            batch.drop_column("request_key")
        if "agent_run_id" in usage_columns:
            batch.drop_column("agent_run_id")

    if "uq_agent_step_unit_key" in _unique_constraints("agent_steps"):
        with op.batch_alter_table("agent_steps") as batch:
            batch.drop_constraint("uq_agent_step_unit_key", type_="unique")
    step_columns = _columns("agent_steps")
    with op.batch_alter_table("agent_steps") as batch:
        for column in ("updated_at", "input_hash", "unit_key"):
            if column in step_columns:
                batch.drop_column(column)

    for name in (
        "ix_agent_runs_settings_hash",
        "ix_agent_runs_source_content_hash",
    ):
        if name in _indexes("agent_runs"):
            op.drop_index(name, table_name="agent_runs")
    run_columns = _columns("agent_runs")
    with op.batch_alter_table("agent_runs") as batch:
        for column in (
            "error_message",
            "checkpoint_revision",
            "settings_hash",
            "source_content_hash",
        ):
            if column in run_columns:
                batch.drop_column(column)

    provider_columns = _columns("provider_models")
    with op.batch_alter_table("provider_models") as batch:
        for column in ("max_output_tokens", "context_window_tokens"):
            if column in provider_columns:
                batch.drop_column(column)
