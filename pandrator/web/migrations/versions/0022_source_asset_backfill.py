"""Backfill the source library from legacy per-session source rows.

Revision ID: 0022_source_asset_backfill
Revises: 0021_query_path_indexes
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0022_source_asset_backfill"
down_revision = "0021_query_path_indexes"
branch_labels = None
depends_on = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if not {"sources", "artifacts", "source_assets", "session_sources"}.issubset(tables):
        return

    rows = list(
        connection.execute(
            sa.text(
            """
            SELECT
                sources.id,
                sources.session_id,
                sources.kind,
                sources.display_name,
                sources.artifact_id,
                sources.created_at,
                artifacts.mime_type,
                artifacts.size_bytes,
                artifacts.content_hash
            FROM sources
            JOIN artifacts ON artifacts.id = sources.artifact_id
            WHERE sources.artifact_id IS NOT NULL
            ORDER BY sources.created_at, sources.id
                """
            )
        ).mappings()
    )
    assets_by_artifact = {
        str(row.artifact_id): str(row.id)
        for row in connection.execute(
            sa.text(
                "SELECT id, artifact_id FROM source_assets WHERE artifact_id IS NOT NULL"
            )
        )
    }
    existing_links = {
        (str(row.session_id), str(row.source_asset_id), str(row.role))
        for row in connection.execute(
            sa.text(
                "SELECT session_id, source_asset_id, role FROM session_sources"
            )
        )
    }

    for row in rows:
        artifact_id = str(row["artifact_id"])
        asset_id = assets_by_artifact.get(artifact_id)
        timestamp = str(row["created_at"] or _now())
        if asset_id is None:
            asset_id = str(uuid.uuid4())
            connection.execute(
                sa.text(
                    """
                    INSERT INTO source_assets (
                        id, artifact_id, display_name, kind, mime_type,
                        external_path, size_bytes, content_hash, state,
                        metadata_json, revision, created_at, updated_at
                    ) VALUES (
                        :id, :artifact_id, :display_name, :kind, :mime_type,
                        NULL, :size_bytes, :content_hash, 'current',
                        :metadata_json, 1, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": asset_id,
                    "artifact_id": artifact_id,
                    "display_name": str(row["display_name"] or "Source"),
                    "kind": str(row["kind"] or "file"),
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "content_hash": row["content_hash"],
                    "metadata_json": json.dumps(
                        {"legacy_source_id": str(row["id"])},
                        separators=(",", ":"),
                    ),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            assets_by_artifact[artifact_id] = asset_id

        link_key = (str(row["session_id"]), asset_id, "primary")
        if link_key in existing_links:
            continue
        connection.execute(
            sa.text(
                """
                INSERT INTO session_sources (
                    id, session_id, source_asset_id, role, is_current,
                    revision, created_at, updated_at
                ) VALUES (
                    :id, :session_id, :source_asset_id, 'primary', 1,
                    1, :created_at, :updated_at
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "session_id": str(row["session_id"]),
                "source_asset_id": asset_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        existing_links.add(link_key)


def downgrade() -> None:
    # This is a data promotion into the supported source-library model.
    # Downgrading the schema must not delete user-visible source attachments.
    pass
