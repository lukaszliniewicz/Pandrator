"""Helpers for tests that pin an older migration revision.

Two different needs appear in the ownership lanes:

* Migration-transition tests create a genuine predecessor schema and then
  upgrade it. Those fixtures must insert only columns that exist at the pinned
  revision, so ``insert_job`` and ``insert_upload_session`` write the
  revision-compatible column set explicitly. They never alter the schema.
* A few stored-corruption tests downgrade a head database, read the legacy
  rows through the current ORM, and never upgrade again. The ORM selects every
  mapped column, including columns added by later migrations, so
  ``prepare_read_only_pinned_schema`` restores only the allowlisted additive
  columns needed to read those rows.

Never call ``prepare_read_only_pinned_schema`` before an Alembic upgrade: the
later migrations own those columns and would try to add them again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pandrator.web.models import new_id, utcnow

# Additive, nullable or defaulted columns that arrived after the pinned
# revisions used for read-only legacy-corruption tests. They carry no ownership
# constraint and are safe to restore only because those lanes never upgrade
# again. Keep this list explicit and append-only: each entry names the migration
# that introduced it, and removing one is only correct after every pinned lane
# stops reading that mapped model. A missing entry must fail loudly here, not
# surface later as an opaque SQLite error.
LATER_ORM_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # 0084_job_external_effects
    ("jobs", "external_effect_json", "JSON"),
    # 0087_public_project_links
    ("upload_sessions", "attach_to_session", "BOOLEAN NOT NULL DEFAULT 1"),
)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_name(model: Any) -> str:
    table = getattr(model, "__table__", model)
    name = getattr(table, "name", None)
    if not isinstance(name, str) or not name:
        raise TypeError("A mapped model or table with a name is required.")
    return str(name)


def _mapped_columns(model: Any) -> set[str]:
    table = getattr(model, "__table__", model)
    columns = getattr(table, "columns", None)
    if columns is None:
        raise TypeError("A mapped model or table is required.")
    return {column.name for column in columns}


def insert_job(
    session: Session,
    *,
    kind: str,
    workspace_id: str,
    job_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
    payload_json: Mapping[str, Any] | None = None,
) -> str:
    """Insert a job using only columns present since revision 0074."""

    identifier = job_id or new_id()
    now = utcnow()
    session.execute(
        text(
            """INSERT INTO jobs (
                id, kind, workspace_id, project_id, session_id, workflow_run_id,
                status, payload_json, resource_keys_json, progress,
                lease_generation, attempts, max_attempts, created_at, updated_at
            ) VALUES (
                :id, :kind, :workspace_id, :project_id, :session_id,
                :workflow_run_id, 'queued', :payload_json, '[]', 0.0,
                0, 0, 1, :created_at, :updated_at
            )"""
        ),
        {
            "id": identifier,
            "kind": kind,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "session_id": session_id,
            "workflow_run_id": workflow_run_id,
            "payload_json": json.dumps(dict(payload_json or {})),
            "created_at": now,
            "updated_at": now,
        },
    )
    return identifier


def insert_upload_session(
    session: Session,
    *,
    workspace_id: str,
    filename: str,
    size_bytes: int,
    chunk_size: int,
    chunk_count: int,
    temporary_relative_path: str,
    expires_at: Any,
    upload_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Insert an upload session using only columns present since revision 0074."""

    identifier = upload_id or new_id()
    now = utcnow()
    session.execute(
        text(
            """INSERT INTO upload_sessions (
                id, session_id, workspace_id, project_id, filename, size_bytes,
                chunk_size, chunk_count, received_json, temporary_relative_path,
                state, created_at, updated_at, expires_at
            ) VALUES (
                :id, :session_id, :workspace_id, :project_id, :filename,
                :size_bytes, :chunk_size, :chunk_count, '{}',
                :temporary_relative_path, 'open', :created_at, :updated_at,
                :expires_at
            )"""
        ),
        {
            "id": identifier,
            "session_id": session_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "filename": filename,
            "size_bytes": size_bytes,
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
            "temporary_relative_path": temporary_relative_path,
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
        },
    )
    return identifier


def prepare_read_only_pinned_schema(path: Path, models: Iterable[Any]) -> None:
    """Add allowlisted later columns and verify a read-only pinned schema.

    Only use this after a downgrade for a test that will not upgrade again.
    ``models`` must contain every mapped model the test will read. The check
    covers all mapped columns because SQLAlchemy selects the full entity shape
    even when the test only touches a subset. This helper never advances the
    pinned revision; restoring a column is not a substitute for an upgrade and
    must not be used before one.
    """

    required: dict[str, set[str]] = {}
    for model in models:
        name = _table_name(model)
        required.setdefault(name, set()).update(_mapped_columns(model))

    with sqlite3.connect(path) as connection:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table_name, column_name, definition in LATER_ORM_COLUMNS:
            if table_name not in required or table_name not in existing_tables:
                continue
            present = {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_quoted(table_name)})"
                )
            }
            if column_name not in present:
                connection.execute(
                    f"ALTER TABLE {_quoted(table_name)} "
                    f"ADD COLUMN {_quoted(column_name)} {definition}"
                )

        for table_name, expected in sorted(required.items()):
            if table_name not in existing_tables:
                raise AssertionError(
                    f"Pinned schema is missing mapped table {table_name!r}; "
                    "the fixture revision and model set disagree."
                )
            present = {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_quoted(table_name)})"
                )
            }
            missing = sorted(expected - present)
            if missing:
                raise AssertionError(
                    f"Current ORM model table {table_name!r} expects "
                    f"column(s) {', '.join(missing)} that the pinned schema "
                    "does not provide. Add the additive column to "
                    "tests/legacy_schema_support.py:LATER_ORM_COLUMNS with its "
                    "migration revision, or seed the fixture with raw SQL."
                )


__all__ = [
    "LATER_ORM_COLUMNS",
    "insert_job",
    "insert_upload_session",
    "prepare_read_only_pinned_schema",
]
