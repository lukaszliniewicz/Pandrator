"""Enforce consistent workspace, project, and session roots for media rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0077_media_scope_consistency"
down_revision = "0076_project_graph_consistency"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_media_scope_artifacts_insert",
        "artifacts",
        """CREATE TRIGGER pandrator_media_scope_artifacts_insert
BEFORE INSERT ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'artifact project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'artifact session must belong to its project')
    WHERE NEW.session_id IS NOT NULL
      AND (
          NEW.project_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id = member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END""",
    ),
    (
        "pandrator_media_scope_artifacts_update",
        "artifacts",
        """CREATE TRIGGER pandrator_media_scope_artifacts_update
BEFORE UPDATE OF workspace_id, project_id, session_id ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'artifact project is immutable')
    WHERE NEW.project_id IS NOT OLD.project_id
      AND NOT (
          OLD.project_id IS NOT NULL
          AND NEW.project_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM projects AS project
              WHERE project.id IS OLD.project_id
          )
      );

    SELECT RAISE(ABORT, 'artifact workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'artifact project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'artifact session must belong to its project')
    WHERE NEW.session_id IS NOT NULL
      AND (
          NEW.project_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id = member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END""",
    ),
    (
        "pandrator_media_scope_source_assets_insert",
        "source_assets",
        """CREATE TRIGGER pandrator_media_scope_source_assets_insert
BEFORE INSERT ON source_assets
BEGIN
    SELECT RAISE(ABORT, 'source asset workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'source asset artifact must share its workspace')
    WHERE NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_media_scope_source_assets_update",
        "source_assets",
        """CREATE TRIGGER pandrator_media_scope_source_assets_update
BEFORE UPDATE OF workspace_id, artifact_id ON source_assets
BEGIN
    SELECT RAISE(ABORT, 'source asset workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'source asset workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'source asset artifact must share its workspace')
    WHERE NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_media_scope_upload_sessions_insert",
        "upload_sessions",
        """CREATE TRIGGER pandrator_media_scope_upload_sessions_insert
BEFORE INSERT ON upload_sessions
BEGIN
    SELECT RAISE(ABORT, 'upload workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'upload project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'upload session must belong to its project')
    WHERE NEW.session_id IS NOT NULL
      AND (
          NEW.project_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id = member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END""",
    ),
    (
        "pandrator_media_scope_upload_sessions_update",
        "upload_sessions",
        """CREATE TRIGGER pandrator_media_scope_upload_sessions_update
BEFORE UPDATE OF workspace_id, project_id, session_id ON upload_sessions
BEGIN
    SELECT RAISE(ABORT, 'upload ownership is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.project_id IS NOT OLD.project_id
       OR NEW.session_id IS NOT OLD.session_id;

    SELECT RAISE(ABORT, 'upload workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'upload project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'upload session must belong to its project')
    WHERE NEW.session_id IS NOT NULL
      AND (
          NEW.project_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id = member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END""",
    ),
    (
        "pandrator_media_scope_sources_insert",
        "sources",
        """CREATE TRIGGER pandrator_media_scope_sources_insert
BEFORE INSERT ON sources
WHEN NEW.artifact_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'source artifact must share its session workspace')
    WHERE NOT EXISTS (
        SELECT 1
        FROM sessions AS member
        JOIN projects AS project ON project.id = member.project_id
        JOIN workspaces AS workspace ON workspace.id = project.workspace_id
        JOIN artifacts AS artifact ON artifact.id = NEW.artifact_id
        WHERE member.id IS NEW.session_id
          AND artifact.workspace_id IS workspace.id
    );
END""",
    ),
    (
        "pandrator_media_scope_sources_update",
        "sources",
        """CREATE TRIGGER pandrator_media_scope_sources_update
BEFORE UPDATE OF session_id, artifact_id ON sources
WHEN NEW.artifact_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'source artifact must share its session workspace')
    WHERE NOT EXISTS (
        SELECT 1
        FROM sessions AS member
        JOIN projects AS project ON project.id = member.project_id
        JOIN workspaces AS workspace ON workspace.id = project.workspace_id
        JOIN artifacts AS artifact ON artifact.id = NEW.artifact_id
        WHERE member.id IS NEW.session_id
          AND artifact.workspace_id IS workspace.id
    );
END""",
    ),
    (
        "pandrator_media_scope_session_sources_insert",
        "session_sources",
        """CREATE TRIGGER pandrator_media_scope_session_sources_insert
BEFORE INSERT ON session_sources
BEGIN
    SELECT RAISE(ABORT, 'session source session must resolve to a workspace')
    WHERE NOT EXISTS (
        SELECT 1
        FROM sessions AS member
        JOIN projects AS project ON project.id = member.project_id
        JOIN workspaces AS workspace ON workspace.id = project.workspace_id
        WHERE member.id IS NEW.session_id
    );

    SELECT RAISE(ABORT, 'session source asset must share its session workspace')
    WHERE NOT EXISTS (
        SELECT 1
        FROM sessions AS member
        JOIN projects AS project ON project.id = member.project_id
        JOIN source_assets AS asset ON asset.id = NEW.source_asset_id
        WHERE member.id IS NEW.session_id
          AND asset.workspace_id IS project.workspace_id
    );
END""",
    ),
    (
        "pandrator_media_scope_session_sources_update",
        "session_sources",
        """CREATE TRIGGER pandrator_media_scope_session_sources_update
BEFORE UPDATE OF session_id, source_asset_id ON session_sources
BEGIN
    SELECT RAISE(ABORT, 'session source session must resolve to a workspace')
    WHERE NOT EXISTS (
        SELECT 1
        FROM sessions AS member
        JOIN projects AS project ON project.id = member.project_id
        JOIN workspaces AS workspace ON workspace.id = project.workspace_id
        WHERE member.id IS NEW.session_id
    );

    SELECT RAISE(ABORT, 'session source asset must share its session workspace')
    WHERE NOT EXISTS (
        SELECT 1
        FROM sessions AS member
        JOIN projects AS project ON project.id = member.project_id
        JOIN source_assets AS asset ON asset.id = NEW.source_asset_id
        WHERE member.id IS NEW.session_id
          AND asset.workspace_id IS project.workspace_id
    );
END""",
    ),
)

_REQUIRED_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workspaces", ("id",)),
    ("projects", ("id", "workspace_id")),
    ("sessions", ("id", "project_id")),
    (
        "artifacts",
        ("id", "workspace_id", "project_id", "session_id"),
    ),
    ("source_assets", ("id", "workspace_id", "artifact_id")),
    ("upload_sessions", ("id", "workspace_id", "project_id", "session_id")),
    ("sources", ("id", "session_id", "artifact_id")),
    ("session_sources", ("id", "session_id", "source_asset_id")),
)

_MEDIA_FOREIGN_KEYS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("artifacts", "workspace_id", "workspaces", "RESTRICT", False),
    ("artifacts", "project_id", "projects", "SET NULL", True),
    ("artifacts", "session_id", "sessions", "CASCADE", True),
    ("source_assets", "workspace_id", "workspaces", "RESTRICT", False),
    ("source_assets", "artifact_id", "artifacts", "SET NULL", True),
    ("upload_sessions", "workspace_id", "workspaces", "RESTRICT", False),
    ("upload_sessions", "project_id", "projects", "RESTRICT", True),
    ("upload_sessions", "session_id", "sessions", "CASCADE", True),
    ("sources", "session_id", "sessions", "CASCADE", False),
    ("sources", "artifact_id", "artifacts", "SET NULL", True),
    ("session_sources", "session_id", "sessions", "CASCADE", False),
    ("session_sources", "source_asset_id", "source_assets", "RESTRICT", False),
)

_PARENT_FOREIGN_KEYS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("projects", "workspace_id", "workspaces", "RESTRICT", False),
    ("sessions", "project_id", "projects", "RESTRICT", False),
)

_PREFLIGHT_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "Cannot enforce media scope consistency; artifacts have inconsistent roots.",
        """SELECT 1
FROM artifacts AS artifact
LEFT JOIN workspaces AS workspace ON workspace.id = artifact.workspace_id
LEFT JOIN projects AS project ON project.id = artifact.project_id
LEFT JOIN sessions AS member ON member.id = artifact.session_id
WHERE workspace.id IS NULL
   OR (
       artifact.project_id IS NOT NULL
       AND (
           project.id IS NULL
           OR project.workspace_id IS NOT artifact.workspace_id
       )
   )
   OR (
       artifact.session_id IS NOT NULL
       AND (
           artifact.project_id IS NULL
           OR member.id IS NULL
           OR member.project_id IS NOT artifact.project_id
           OR project.id IS NULL
           OR project.workspace_id IS NOT artifact.workspace_id
       )
   )
LIMIT 1""",
    ),
    (
        "Cannot enforce media scope consistency; source assets have inconsistent roots.",
        """SELECT 1
FROM source_assets AS asset
LEFT JOIN workspaces AS workspace ON workspace.id = asset.workspace_id
LEFT JOIN artifacts AS artifact ON artifact.id = asset.artifact_id
WHERE workspace.id IS NULL
   OR (
       asset.artifact_id IS NOT NULL
       AND (
           artifact.id IS NULL
           OR artifact.workspace_id IS NOT asset.workspace_id
       )
   )
LIMIT 1""",
    ),
    (
        "Cannot enforce media scope consistency; upload sessions have inconsistent roots.",
        """SELECT 1
FROM upload_sessions AS upload
LEFT JOIN workspaces AS workspace ON workspace.id = upload.workspace_id
LEFT JOIN projects AS project ON project.id = upload.project_id
LEFT JOIN sessions AS member ON member.id = upload.session_id
WHERE workspace.id IS NULL
   OR (
       upload.project_id IS NOT NULL
       AND (
           project.id IS NULL
           OR project.workspace_id IS NOT upload.workspace_id
       )
   )
   OR (
       upload.session_id IS NOT NULL
       AND (
           upload.project_id IS NULL
           OR member.id IS NULL
           OR member.project_id IS NOT upload.project_id
           OR project.id IS NULL
           OR project.workspace_id IS NOT upload.workspace_id
       )
   )
LIMIT 1""",
    ),
    (
        "Cannot enforce media scope consistency; sources have inconsistent roots.",
        """SELECT 1
FROM sources AS source
LEFT JOIN sessions AS member ON member.id = source.session_id
LEFT JOIN projects AS project ON project.id = member.project_id
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
LEFT JOIN artifacts AS artifact ON artifact.id = source.artifact_id
WHERE member.id IS NULL
   OR project.id IS NULL
   OR workspace.id IS NULL
   OR (
       source.artifact_id IS NOT NULL
       AND (
           artifact.id IS NULL
           OR artifact.workspace_id IS NOT workspace.id
       )
   )
LIMIT 1""",
    ),
    (
        "Cannot enforce media scope consistency; session sources have inconsistent roots.",
        """SELECT 1
FROM session_sources AS attachment
LEFT JOIN sessions AS member ON member.id = attachment.session_id
LEFT JOIN projects AS project ON project.id = member.project_id
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
LEFT JOIN source_assets AS asset ON asset.id = attachment.source_asset_id
WHERE member.id IS NULL
   OR project.id IS NULL
   OR workspace.id IS NULL
   OR asset.id IS NULL
   OR asset.workspace_id IS NOT workspace.id
LIMIT 1""",
    ),
)

_EXPECTED_PROJECT_TRIGGER_SQL = """CREATE TRIGGER pandrator_project_graph_projects_update
BEFORE UPDATE OF workspace_id, source_session_id ON projects
BEGIN
    SELECT RAISE(ABORT, 'project workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'project source must belong to project')
    WHERE NEW.source_session_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sessions AS source
          WHERE source.id = NEW.source_session_id
            AND source.project_id = NEW.id
      );

    SELECT RAISE(ABORT, 'project source is immutable after attachment')
    WHERE OLD.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NOT OLD.source_session_id;

    SELECT RAISE(ABORT, 'project source cannot be cleared while its session exists')
    WHERE OLD.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NULL
      AND EXISTS (
          SELECT 1 FROM sessions AS source
          WHERE source.id = OLD.source_session_id
      );
END"""

_EXPECTED_SESSION_TRIGGER_SQL = """CREATE TRIGGER pandrator_project_graph_sessions_update
BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'session project is immutable');
END"""

_REQUIRED_PARENT_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_project_graph_projects_update",
        "projects",
        _EXPECTED_PROJECT_TRIGGER_SQL,
    ),
    (
        "pandrator_project_graph_sessions_update",
        "sessions",
        _EXPECTED_SESSION_TRIGGER_SQL,
    ),
)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _has_expected_foreign_key(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    parent_table: str,
    ondelete: str,
) -> bool:
    matches = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        if column_name in (foreign_key.get("constrained_columns") or ())
    ]
    if len(matches) != 1:
        return False
    foreign_key = matches[0]
    options = foreign_key.get("options") or {}
    return (
        tuple(foreign_key.get("constrained_columns") or ()) == (column_name,)
        and foreign_key.get("referred_table") == parent_table
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and str(options.get("ondelete") or "").upper() == ondelete
    )


def _validate_schema(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name, required_columns in _REQUIRED_COLUMNS:
        if table_name not in tables:
            raise RuntimeError(
                "Cannot enforce media scope consistency; a required table is missing."
            )
        columns = _columns(bind, table_name)
        if any(column_name not in columns for column_name in required_columns):
            raise RuntimeError(
                "Cannot enforce media scope consistency; a required column is missing."
            )

    for table_name, column_name, parent_table, ondelete, nullable in (
        _MEDIA_FOREIGN_KEYS + _PARENT_FOREIGN_KEYS
    ):
        column = _columns(bind, table_name)[column_name]
        column_type = column["type"]
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, "length", None) != 36
            or bool(column.get("nullable")) != nullable
            or column.get("default") is not None
            or not _has_expected_foreign_key(bind, table_name, column_name, parent_table, ondelete)
        ):
            raise RuntimeError(
                "Cannot enforce media scope consistency; a media ownership foreign key has an unexpected shape."
            )


def _validate_parent_triggers(bind: sa.Connection) -> None:
    for name, expected_table, expected_sql in _REQUIRED_PARENT_TRIGGERS:
        row = bind.execute(
            sa.text(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = :name COLLATE NOCASE LIMIT 1"
            ),
            {"name": name},
        ).first()
        if (
            row is None
            or str(row[0]) != name
            or str(row[1]) != expected_table
            or str(row[2]) != expected_sql
        ):
            raise RuntimeError(
                "Cannot enforce media scope consistency; a required project graph trigger is missing or altered."
            )


def _validate_data(bind: sa.Connection) -> None:
    for message, query in _PREFLIGHT_QUERIES:
        if bind.exec_driver_sql(query).first() is not None:
            raise RuntimeError(message)


def _reject_name_collisions(bind: sa.Connection) -> None:
    for name, _table_name, _sql in _TRIGGERS:
        row = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot enforce media scope consistency; an owned trigger name is already in use."
            )


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Media scope consistency migration is supported only on SQLite.")
    return bind


def _expected_media_trigger_sql(bind: sa.Connection) -> dict[str, tuple[str, str]]:
    names = tuple(name for name, _table_name, _sql in _TRIGGERS)
    parameters = {f"name_{index}": name for index, name in enumerate(names)}
    placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
    rows = bind.execute(
        sa.text(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            f"WHERE type = 'trigger' AND name IN ({placeholders}) ORDER BY name"
        ),
        parameters,
    ).all()
    actual = {str(name): (str(table_name), str(sql)) for name, table_name, sql in rows}
    expected = {name: (table_name, sql) for name, table_name, sql in _TRIGGERS}
    if actual != expected:
        raise RuntimeError(
            "Cannot downgrade media scope consistency; an owned trigger is missing or altered."
        )
    return expected


def upgrade() -> None:
    bind = _sqlite_bind()
    _validate_schema(bind)
    _validate_parent_triggers(bind)
    _validate_data(bind)
    _reject_name_collisions(bind)
    for _name, _table_name, sql in _TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    bind = _sqlite_bind()
    _expected_media_trigger_sql(bind)
    preparer = bind.dialect.identifier_preparer
    for name, _table_name, _sql in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {preparer.quote(name)}")
