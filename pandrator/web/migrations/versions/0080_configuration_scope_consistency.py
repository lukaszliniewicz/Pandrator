"""Keep configuration and pronunciation rows within their stored workspace."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0080_configuration_scope_consistency"
down_revision = "0079_execution_scope_consistency"
branch_labels = None
depends_on = None

_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "providers": {"id", "workspace_id"},
    "stored_credentials": {"key", "workspace_id"},
    "workspace_settings": {"key", "workspace_id"},
    "workspace_settings_history": {"id", "key", "workspace_id"},
    "pronunciation_entries": {
        "id",
        "workspace_id",
        "project_id",
        "scope",
        "session_id",
    },
    "projects": {"id", "workspace_id"},
    "sessions": {"id", "project_id"},
    "workspaces": {"id"},
}

_TARGET_FOREIGN_KEYS: dict[str, set[tuple[tuple[str, ...], str, tuple[str, ...], str]]] = {
    "providers": {
        (("workspace_id",), "workspaces", ("id",), "RESTRICT"),
    },
    "stored_credentials": {
        (("workspace_id",), "workspaces", ("id",), "RESTRICT"),
    },
    "workspace_settings": {
        (("workspace_id",), "workspaces", ("id",), "RESTRICT"),
    },
    "workspace_settings_history": {
        (("workspace_id",), "workspaces", ("id",), "RESTRICT"),
    },
    "pronunciation_entries": {
        (("workspace_id",), "workspaces", ("id",), "RESTRICT"),
        (("project_id",), "projects", ("id",), "CASCADE"),
        (("session_id",), "sessions", ("id",), "CASCADE"),
    },
}

_WORKSPACE_ROOT_COLUMNS = (
    ("providers", "workspace_id"),
    ("stored_credentials", "workspace_id"),
    ("workspace_settings", "workspace_id"),
    ("workspace_settings_history", "workspace_id"),
    ("pronunciation_entries", "workspace_id"),
)

_PREFLIGHT_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "providers has a missing workspace root",
        """SELECT provider.id, provider.workspace_id
FROM providers AS provider
LEFT JOIN workspaces AS workspace ON workspace.id IS provider.workspace_id
WHERE provider.workspace_id IS NULL OR workspace.id IS NULL
LIMIT 1""",
    ),
    (
        "stored_credentials has a missing workspace root",
        """SELECT credential.key, credential.workspace_id
FROM stored_credentials AS credential
LEFT JOIN workspaces AS workspace ON workspace.id IS credential.workspace_id
WHERE credential.workspace_id IS NULL OR workspace.id IS NULL
LIMIT 1""",
    ),
    (
        "workspace_settings has a missing workspace root",
        """SELECT setting.key, setting.workspace_id
FROM workspace_settings AS setting
LEFT JOIN workspaces AS workspace ON workspace.id IS setting.workspace_id
WHERE setting.workspace_id IS NULL OR workspace.id IS NULL
LIMIT 1""",
    ),
    (
        "workspace_settings_history has a missing workspace root",
        """SELECT history.id, history.key, history.workspace_id
FROM workspace_settings_history AS history
LEFT JOIN workspaces AS workspace ON workspace.id IS history.workspace_id
WHERE history.workspace_id IS NULL OR workspace.id IS NULL
LIMIT 1""",
    ),
    (
        "pronunciation_entries has an invalid scope or inconsistent roots",
        """SELECT entry.id, entry.scope, entry.workspace_id, entry.project_id, entry.session_id
FROM pronunciation_entries AS entry
LEFT JOIN workspaces AS workspace ON workspace.id IS entry.workspace_id
LEFT JOIN projects AS project ON project.id IS entry.project_id
LEFT JOIN sessions AS member ON member.id IS entry.session_id
WHERE workspace.id IS NULL
   OR entry.scope IS NOT 'global' AND entry.scope IS NOT 'session'
   OR (entry.scope IS 'global' AND (
       entry.project_id IS NOT NULL OR entry.session_id IS NOT NULL
   ))
   OR (entry.scope IS 'session' AND (
       entry.project_id IS NULL
       OR entry.session_id IS NULL
       OR project.id IS NULL
       OR member.id IS NULL
       OR member.project_id IS NOT entry.project_id
       OR project.workspace_id IS NOT entry.workspace_id
   ))
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

_REQUIRED_PARENT_TRIGGERS = (
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

_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_configuration_scope_providers_update",
        "providers",
        """CREATE TRIGGER pandrator_configuration_scope_providers_update
BEFORE UPDATE OF workspace_id ON providers
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'provider workspace is immutable');
END""",
    ),
    (
        "pandrator_configuration_scope_stored_credentials_update",
        "stored_credentials",
        """CREATE TRIGGER pandrator_configuration_scope_stored_credentials_update
BEFORE UPDATE OF workspace_id ON stored_credentials
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'stored credential workspace is immutable');
END""",
    ),
    (
        "pandrator_configuration_scope_workspace_settings_update",
        "workspace_settings",
        """CREATE TRIGGER pandrator_configuration_scope_workspace_settings_update
BEFORE UPDATE OF workspace_id ON workspace_settings
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'workspace setting workspace is immutable');
END""",
    ),
    (
        "pandrator_configuration_scope_workspace_settings_history_update",
        "workspace_settings_history",
        """CREATE TRIGGER pandrator_configuration_scope_workspace_settings_history_update
BEFORE UPDATE OF workspace_id ON workspace_settings_history
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'workspace setting history workspace is immutable');
END""",
    ),
    (
        "pandrator_configuration_scope_pronunciation_entries_insert",
        "pronunciation_entries",
        """CREATE TRIGGER pandrator_configuration_scope_pronunciation_entries_insert
BEFORE INSERT ON pronunciation_entries
BEGIN
    SELECT RAISE(ABORT, 'pronunciation workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'pronunciation scope is invalid')
    WHERE NEW.scope IS NOT 'global' AND NEW.scope IS NOT 'session';

    SELECT RAISE(ABORT, 'global pronunciation cannot have project or session roots')
    WHERE NEW.scope IS 'global'
      AND (NEW.project_id IS NOT NULL OR NEW.session_id IS NOT NULL);

    SELECT RAISE(ABORT, 'session pronunciation roots are inconsistent')
    WHERE NEW.scope IS 'session'
      AND (
          NEW.project_id IS NULL
          OR NEW.session_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id IS member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END""",
    ),
    (
        "pandrator_configuration_scope_pronunciation_entries_update",
        "pronunciation_entries",
        """CREATE TRIGGER pandrator_configuration_scope_pronunciation_entries_update
BEFORE UPDATE OF workspace_id, project_id, session_id, scope ON pronunciation_entries
BEGIN
    SELECT RAISE(ABORT, 'pronunciation workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'pronunciation workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'pronunciation scope is invalid')
    WHERE NEW.scope IS NOT 'global' AND NEW.scope IS NOT 'session';

    SELECT RAISE(ABORT, 'global pronunciation cannot have project or session roots')
    WHERE NEW.scope IS 'global'
      AND (NEW.project_id IS NOT NULL OR NEW.session_id IS NOT NULL);

    SELECT RAISE(ABORT, 'session pronunciation roots are inconsistent')
    WHERE NEW.scope IS 'session'
      AND (
          NEW.project_id IS NULL
          OR NEW.session_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id IS member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END""",
    ),
)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> tuple[tuple[tuple[str, ...], str, tuple[str, ...], str], ...]:
    return tuple(
        (
            tuple(foreign_key.get("constrained_columns") or ()),
            str(foreign_key.get("referred_table") or ""),
            tuple(foreign_key.get("referred_columns") or ()),
            str((foreign_key.get("options") or {}).get("ondelete") or "").upper(),
        )
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
    )


def _validate_column(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    *,
    length: int,
    nullable: bool,
    require_no_default: bool = True,
) -> None:
    column = _columns(bind, table_name)[column_name]
    if (
        not isinstance(column.get("type"), sa.String)
        or getattr(column["type"], "length", None) != length
        or bool(column.get("nullable")) != nullable
        or (require_no_default and column.get("default") is not None)
    ):
        raise RuntimeError(
            "Cannot enforce configuration scope consistency; "
            f"{table_name}.{column_name} has an unexpected shape."
        )


def _validate_schema(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    missing_tables = set(_REQUIRED_COLUMNS) - tables
    if missing_tables:
        raise RuntimeError(
            "Cannot enforce configuration scope consistency; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )

    for table_name, expected_columns in _REQUIRED_COLUMNS.items():
        missing_columns = expected_columns - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                "Cannot enforce configuration scope consistency; "
                f"{table_name} is missing required columns: " + ", ".join(sorted(missing_columns))
            )

    for table_name, expected_foreign_keys in _TARGET_FOREIGN_KEYS.items():
        actual_foreign_keys = _foreign_key_signatures(bind, table_name)
        if (
            len(actual_foreign_keys) != len(expected_foreign_keys)
            or set(actual_foreign_keys) != expected_foreign_keys
        ):
            raise RuntimeError(
                "Cannot enforce configuration scope consistency; "
                f"{table_name} has an unexpected foreign-key shape."
            )

    for table_name, column_name in _WORKSPACE_ROOT_COLUMNS:
        _validate_column(
            bind,
            table_name,
            column_name,
            length=36,
            nullable=False,
        )

    _validate_column(
        bind,
        "pronunciation_entries",
        "project_id",
        length=36,
        nullable=True,
    )
    _validate_column(
        bind,
        "pronunciation_entries",
        "session_id",
        length=36,
        nullable=True,
    )
    _validate_column(
        bind,
        "pronunciation_entries",
        "scope",
        length=16,
        nullable=False,
        require_no_default=False,
    )

    for table_name, column_name in (
        ("projects", "workspace_id"),
        ("sessions", "project_id"),
    ):
        _validate_column(
            bind,
            table_name,
            column_name,
            length=36,
            nullable=False,
        )

    project_workspace_foreign_keys = [
        signature
        for signature in _foreign_key_signatures(bind, "projects")
        if signature[0] == ("workspace_id",)
    ]
    if project_workspace_foreign_keys != [(("workspace_id",), "workspaces", ("id",), "RESTRICT")]:
        raise RuntimeError(
            "Cannot enforce configuration scope consistency; projects.workspace_id has an unexpected foreign key."
        )
    session_project_foreign_keys = [
        signature
        for signature in _foreign_key_signatures(bind, "sessions")
        if signature[0] == ("project_id",)
    ]
    if session_project_foreign_keys != [(("project_id",), "projects", ("id",), "RESTRICT")]:
        raise RuntimeError(
            "Cannot enforce configuration scope consistency; sessions.project_id has an unexpected foreign key."
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
                "Cannot enforce configuration scope consistency; "
                f"required parent trigger {name} is missing or altered."
            )


def _validate_data(bind: sa.Connection) -> None:
    for message, query in _PREFLIGHT_QUERIES:
        row = bind.exec_driver_sql(query).first()
        if row is not None:
            anchor = ", ".join("NULL" if value is None else str(value) for value in row)
            raise RuntimeError(
                f"Cannot enforce configuration scope consistency; {message} (row: {anchor})."
            )


def _reject_name_collisions(bind: sa.Connection) -> None:
    for name, _table_name, _sql in _TRIGGERS:
        row = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot enforce configuration scope consistency; "
                f"owned trigger name {name} is already in use."
            )


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Configuration scope consistency migration is supported only on SQLite.")
    return bind


def _expected_owned_triggers(bind: sa.Connection) -> dict[str, tuple[str, str]]:
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
            "Cannot downgrade configuration scope consistency; "
            "an owned trigger is missing or altered."
        )
    return actual


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
    expected = _expected_owned_triggers(bind)
    preparer = bind.dialect.identifier_preparer
    for name, _table_name, _sql in reversed(_TRIGGERS):
        if name not in expected:
            raise RuntimeError(
                "Cannot downgrade configuration scope consistency; "
                "an owned trigger is missing or altered."
            )
        op.execute(f"DROP TRIGGER {preparer.quote(name)}")
