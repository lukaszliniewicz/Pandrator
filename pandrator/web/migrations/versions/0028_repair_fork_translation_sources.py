"""Repair legacy fork translation-source references.

Revision ID: 0028_repair_fork_translation_sources
Revises: 0027_durable_agentic_runs
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0028_repair_fork_translation_sources"
down_revision = "0027_durable_agentic_runs"
branch_labels = None
depends_on = None


def _json_object(value: Any) -> dict[str, Any] | None:
    """Return a detached JSON object without letting malformed legacy JSON abort.

    Raw SQL intentionally bypasses SQLAlchemy's JSON result processor here:
    older databases can contain malformed or scalar JSON in these columns.
    """

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _tables_and_columns(bind) -> tuple[set[str], dict[str, set[str]]]:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    columns = {
        table: {str(column["name"]) for column in inspector.get_columns(table)}
        for table in {
            "artifacts",
            "session_settings",
            "session_sources",
            "source_assets",
        }
        & tables
    }
    return tables, columns


def upgrade() -> None:
    """Map foreign fork settings to local clones, or remove only that key.

    The migration is deliberately data-only and idempotent.  A malformed row,
    partially restored database, or absent older table is left untouched rather
    than preventing an otherwise valid schema upgrade.
    """

    bind = op.get_bind()
    tables, columns = _tables_and_columns(bind)
    required = {
        "session_settings": {"session_id", "section", "value_json"},
        "artifacts": {"id", "session_id", "relative_path", "metadata_json"},
    }
    if any(
        table not in tables or not fields <= columns.get(table, set())
        for table, fields in required.items()
    ):
        return

    settings_rows = bind.execute(
        sa.text(
            "SELECT session_id, value_json FROM session_settings "
            "WHERE section = :section"
        ),
        {"section": "translation"},
    ).mappings()
    update_setting = sa.text(
        "UPDATE session_settings SET value_json = :value_json "
        "WHERE session_id = :session_id AND section = :section"
    ).bindparams(sa.bindparam("value_json", type_=sa.JSON()))
    find_artifact = sa.text(
        "SELECT session_id, relative_path FROM artifacts WHERE id = :artifact_id"
    )
    attachment_columns = {
        "session_sources": {"session_id", "source_asset_id"},
        "source_assets": {"id", "artifact_id"},
    }
    can_check_attachments = all(
        table in tables and fields <= columns.get(table, set())
        for table, fields in attachment_columns.items()
    )
    find_attachment = sa.text(
        "SELECT 1 FROM session_sources "
        "JOIN source_assets ON source_assets.id = session_sources.source_asset_id "
        "WHERE session_sources.session_id = :session_id "
        "AND source_assets.artifact_id = :artifact_id LIMIT 1"
    )
    local_artifacts = sa.text(
        "SELECT id, metadata_json FROM artifacts "
        "WHERE session_id = :session_id ORDER BY id"
    )

    for row in settings_rows:
        settings = _json_object(row["value_json"])
        if settings is None:
            continue
        foreign_id = str(settings.get("source_artifact_id") or "")
        if not foreign_id:
            continue
        source = (
            bind.execute(find_artifact, {"artifact_id": foreign_id}).mappings().first()
        )
        session_id = row["session_id"]
        source_session_id = source["session_id"] if source is not None else None
        if source is not None and (
            source_session_id is None or str(source_session_id) == str(session_id)
        ):
            continue
        if (
            source is not None
            and str(source["relative_path"] or "").lower().endswith(".srt")
            and can_check_attachments
        ):
            attached = bind.execute(
                find_attachment,
                {"session_id": session_id, "artifact_id": foreign_id},
            ).first()
            if attached is not None:
                # Source-library attachments are intentionally cross-session
                # and remain valid translation inputs.
                continue

        if source is None:
            repaired = dict(settings)
            repaired.pop("source_artifact_id", None)
            bind.execute(
                update_setting,
                {
                    "session_id": session_id,
                    "section": "translation",
                    "value_json": repaired,
                },
            )
            continue

        replacement_id = next(
            (
                candidate["id"]
                for candidate in bind.execute(
                    local_artifacts, {"session_id": session_id}
                ).mappings()
                if (
                    (metadata := _json_object(candidate["metadata_json"])) is not None
                    and str(metadata.get("forked_from_artifact_id") or "") == foreign_id
                )
            ),
            None,
        )
        repaired = dict(settings)
        if replacement_id:
            repaired["source_artifact_id"] = replacement_id
        else:
            repaired.pop("source_artifact_id", None)
        bind.execute(
            update_setting,
            {
                "session_id": session_id,
                "section": "translation",
                "value_json": repaired,
            },
        )


def downgrade() -> None:
    # This data repair is intentionally irreversible: never recreate a
    # cross-session source reference while downgrading the schema.
    pass
