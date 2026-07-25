"""Add measured indexes for interactive and assembly query paths.

Revision ID: 0021_query_path_indexes
Revises: 0020_capability_snapshots
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql.elements import TextClause


revision = "0021_query_path_indexes"
down_revision = "0020_capability_snapshots"
branch_labels = None
depends_on = None


INDEXES: tuple[tuple[str, str, list[str | TextClause]], ...] = (
    (
        "idx_jobs_session_kind_created",
        "jobs",
        ["session_id", "kind", sa.text("created_at DESC"), sa.text("id DESC")],
    ),
    (
        "idx_jobs_status_created",
        "jobs",
        ["status", "created_at", "id"],
    ),
    (
        "idx_artifacts_session_role_created",
        "artifacts",
        ["session_id", "role", sa.text("created_at DESC"), sa.text("id DESC")],
    ),
    (
        "idx_artifact_edges_child_parent",
        "artifact_edges",
        ["child_artifact_id", "parent_artifact_id"],
    ),
    (
        "idx_audio_takes_active_status_segment_created",
        "audio_takes",
        [
            "is_active",
            "status",
            "generation_segment_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    ),
    (
        "idx_session_sources_current_updated",
        "session_sources",
        [
            "session_id",
            sa.text("is_current DESC"),
            sa.text("updated_at DESC"),
            "id",
        ],
    ),
    (
        "idx_usage_events_session_stage_created",
        "usage_events",
        [
            "session_id",
            "stage",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    ),
    (
        "idx_output_assemblies_session_created",
        "output_assemblies",
        [
            "session_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    ),
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_by_table = {
        table_name: {
            index["name"]
            for index in inspector.get_indexes(table_name)
        }
        for _index_name, table_name, _columns in INDEXES
    }
    for index_name, table_name, columns in INDEXES:
        if index_name not in existing_by_table[table_name]:
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_by_table = {
        table_name: {
            index["name"]
            for index in inspector.get_indexes(table_name)
        }
        for _index_name, table_name, _columns in INDEXES
    }
    for index_name, table_name, _columns in reversed(INDEXES):
        if index_name in existing_by_table[table_name]:
            op.drop_index(index_name, table_name=table_name)
