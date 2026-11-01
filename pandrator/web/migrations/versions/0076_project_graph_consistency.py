"""Enforce consistency across the project, session, translation, and folder graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0076_project_graph_consistency"
down_revision = "0075_mandatory_ownership"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_project_graph_projects_insert",
        "projects",
        """CREATE TRIGGER pandrator_project_graph_projects_insert
BEFORE INSERT ON projects
WHEN NEW.source_session_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM sessions AS source
    WHERE source.id = NEW.source_session_id
      AND source.project_id = NEW.id
 )
BEGIN
    SELECT RAISE(ABORT, 'project source must belong to project');
END""",
    ),
    (
        "pandrator_project_graph_projects_update",
        "projects",
        """CREATE TRIGGER pandrator_project_graph_projects_update
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
END""",
    ),
    (
        "pandrator_project_graph_sessions_update",
        "sessions",
        """CREATE TRIGGER pandrator_project_graph_sessions_update
BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'session project is immutable');
END""",
    ),
    (
        "pandrator_project_graph_translation_projects_insert",
        "translation_projects",
        """CREATE TRIGGER pandrator_project_graph_translation_projects_insert
BEFORE INSERT ON translation_projects
WHEN NOT EXISTS (
    SELECT 1
    FROM projects AS project
    JOIN sessions AS source ON source.id = project.source_session_id
    WHERE project.id = NEW.project_root_id
      AND project.source_session_id IS NEW.source_session_id
      AND source.project_id IS NEW.project_root_id
)
BEGIN
    SELECT RAISE(ABORT, 'translation project root and source do not match');
END""",
    ),
    (
        "pandrator_project_graph_translation_projects_update",
        "translation_projects",
        """CREATE TRIGGER pandrator_project_graph_translation_projects_update
BEFORE UPDATE OF project_root_id, source_session_id ON translation_projects
WHEN NEW.project_root_id IS NOT OLD.project_root_id
  OR NEW.source_session_id IS NOT OLD.source_session_id
BEGIN
    SELECT RAISE(ABORT, 'translation project root and source are immutable');
END""",
    ),
    (
        "pandrator_project_graph_translation_project_branches_insert",
        "translation_project_branches",
        """CREATE TRIGGER pandrator_project_graph_translation_project_branches_insert
BEFORE INSERT ON translation_project_branches
WHEN NOT EXISTS (
    SELECT 1
    FROM translation_projects AS translation
    JOIN projects AS project ON project.id = translation.project_root_id
    JOIN sessions AS branch ON branch.id = NEW.session_id
    WHERE translation.id = NEW.project_id
      AND branch.project_id IS project.id
      AND branch.id IS NOT translation.source_session_id
)
BEGIN
    SELECT RAISE(ABORT, 'translation branch does not belong to its project root');
END""",
    ),
    (
        "pandrator_project_graph_translation_project_branches_update",
        "translation_project_branches",
        """CREATE TRIGGER pandrator_project_graph_translation_project_branches_update
BEFORE UPDATE OF project_id, session_id ON translation_project_branches
WHEN NEW.project_id IS NOT OLD.project_id
  OR NEW.session_id IS NOT OLD.session_id
BEGIN
    SELECT RAISE(ABORT, 'translation branch project and session are immutable');
END""",
    ),
    (
        "pandrator_project_graph_project_folders_insert",
        "project_folders",
        """CREATE TRIGGER pandrator_project_graph_project_folders_insert
BEFORE INSERT ON project_folders
BEGIN
    SELECT RAISE(ABORT, 'project folder hierarchy contains a cycle')
    WHERE NEW.parent_id IS NOT NULL
      AND EXISTS (
          WITH RECURSIVE ancestors(id) AS (
              SELECT NEW.parent_id
              UNION
              SELECT parent.parent_id
              FROM project_folders AS parent
              JOIN ancestors ON parent.id = ancestors.id
              WHERE parent.parent_id IS NOT NULL
          )
          SELECT 1 FROM ancestors WHERE id IS NEW.id
      );

    SELECT RAISE(ABORT, 'project folder parent must share its workspace')
    WHERE NEW.parent_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM project_folders AS parent
          WHERE parent.id = NEW.parent_id
            AND parent.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_project_graph_project_folders_update",
        "project_folders",
        """CREATE TRIGGER pandrator_project_graph_project_folders_update
BEFORE UPDATE OF workspace_id, parent_id ON project_folders
BEGIN
    SELECT RAISE(ABORT, 'project folder workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'project folder hierarchy contains a cycle')
    WHERE NEW.parent_id IS NOT NULL
      AND EXISTS (
          WITH RECURSIVE ancestors(id) AS (
              SELECT NEW.parent_id
              UNION
              SELECT parent.parent_id
              FROM project_folders AS parent
              JOIN ancestors ON parent.id = ancestors.id
              WHERE parent.parent_id IS NOT NULL
          )
          SELECT 1 FROM ancestors WHERE id IS NEW.id
      );

    SELECT RAISE(ABORT, 'project folder parent must share its workspace')
    WHERE NEW.parent_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM project_folders AS parent
          WHERE parent.id = NEW.parent_id
            AND parent.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_project_graph_project_folder_memberships_insert",
        "project_folder_memberships",
        """CREATE TRIGGER pandrator_project_graph_project_folder_memberships_insert
BEFORE INSERT ON project_folder_memberships
WHEN NOT EXISTS (
    SELECT 1
    FROM project_folders AS folder
    JOIN projects AS project ON project.id = NEW.project_id
    JOIN sessions AS source ON source.id = NEW.project_session_id
    WHERE folder.id = NEW.folder_id
      AND folder.workspace_id IS project.workspace_id
      AND project.source_session_id IS NOT NULL
      AND NEW.project_session_id IS project.source_session_id
      AND source.project_id IS project.id
)
BEGIN
    SELECT RAISE(ABORT, 'project folder membership roots do not match');
END""",
    ),
    (
        "pandrator_project_graph_project_folder_memberships_update",
        "project_folder_memberships",
        """CREATE TRIGGER pandrator_project_graph_project_folder_memberships_update
BEFORE UPDATE OF folder_id, project_id, project_session_id ON project_folder_memberships
WHEN NOT EXISTS (
    SELECT 1
    FROM project_folders AS folder
    JOIN projects AS project ON project.id = NEW.project_id
    JOIN sessions AS source ON source.id = NEW.project_session_id
    WHERE folder.id = NEW.folder_id
      AND folder.workspace_id IS project.workspace_id
      AND project.source_session_id IS NOT NULL
      AND NEW.project_session_id IS project.source_session_id
      AND source.project_id IS project.id
)
BEGIN
    SELECT RAISE(ABORT, 'project folder membership roots do not match');
END""",
    ),
)

_REQUIRED_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workspaces", ("id",)),
    ("projects", ("id", "workspace_id", "source_session_id")),
    ("sessions", ("id", "project_id")),
    ("translation_projects", ("id", "project_root_id", "source_session_id")),
    ("translation_project_branches", ("id", "project_id", "session_id")),
    ("project_folders", ("id", "workspace_id", "parent_id")),
    (
        "project_folder_memberships",
        ("folder_id", "project_session_id", "project_id"),
    ),
)

_REQUIRED_ROOT_FKS: tuple[tuple[str, str, str, str], ...] = (
    ("projects", "workspace_id", "workspaces", "RESTRICT"),
    ("sessions", "project_id", "projects", "RESTRICT"),
    ("translation_projects", "project_root_id", "projects", "RESTRICT"),
    ("project_folders", "workspace_id", "workspaces", "RESTRICT"),
    ("project_folder_memberships", "project_id", "projects", "CASCADE"),
)

_PREFLIGHT_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "Cannot enforce project graph consistency; projects contain invalid workspace or source roots.",
        """SELECT 1
FROM projects AS project
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
WHERE workspace.id IS NULL
   OR (
       project.source_session_id IS NULL
       AND EXISTS (
           SELECT 1 FROM sessions AS member
           WHERE member.project_id = project.id
       )
   )
   OR (
       project.source_session_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM sessions AS source
           WHERE source.id = project.source_session_id
             AND source.project_id = project.id
       )
   )
LIMIT 1""",
    ),
    (
        "Cannot enforce project graph consistency; sessions reference missing projects.",
        """SELECT 1
FROM sessions AS member
LEFT JOIN projects AS project ON project.id = member.project_id
WHERE project.id IS NULL
LIMIT 1""",
    ),
    (
        "Cannot enforce project graph consistency; translation projects have inconsistent roots.",
        """SELECT 1
FROM translation_projects AS translation
LEFT JOIN projects AS project ON project.id = translation.project_root_id
LEFT JOIN sessions AS source ON source.id = translation.source_session_id
WHERE project.id IS NULL
   OR source.id IS NULL
   OR project.source_session_id IS NOT translation.source_session_id
   OR source.project_id IS NOT translation.project_root_id
LIMIT 1""",
    ),
    (
        "Cannot enforce project graph consistency; translation branches have inconsistent roots.",
        """SELECT 1
FROM translation_project_branches AS branch
LEFT JOIN translation_projects AS translation ON translation.id = branch.project_id
LEFT JOIN projects AS project ON project.id = translation.project_root_id
LEFT JOIN sessions AS member ON member.id = branch.session_id
WHERE translation.id IS NULL
   OR project.id IS NULL
   OR member.id IS NULL
   OR member.project_id IS NOT project.id
   OR branch.session_id IS translation.source_session_id
LIMIT 1""",
    ),
    (
        "Cannot enforce project graph consistency; folders have invalid workspace parents.",
        """SELECT 1
FROM project_folders AS folder
LEFT JOIN workspaces AS workspace ON workspace.id = folder.workspace_id
LEFT JOIN project_folders AS parent ON parent.id = folder.parent_id
WHERE workspace.id IS NULL
   OR (
       folder.parent_id IS NOT NULL
       AND (
           parent.id IS NULL
           OR parent.workspace_id IS NOT folder.workspace_id
       )
   )
LIMIT 1""",
    ),
    (
        "Cannot enforce project graph consistency; folder hierarchy contains a cycle.",
        """WITH RECURSIVE ancestors(folder_id, ancestor_id) AS (
    SELECT id, parent_id
    FROM project_folders
    WHERE parent_id IS NOT NULL
    UNION
    SELECT ancestors.folder_id, parent.parent_id
    FROM ancestors
    JOIN project_folders AS parent ON parent.id = ancestors.ancestor_id
    WHERE parent.parent_id IS NOT NULL
)
SELECT 1
FROM ancestors
WHERE folder_id IS ancestor_id
LIMIT 1""",
    ),
    (
        "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        """SELECT 1
FROM project_folder_memberships AS membership
LEFT JOIN project_folders AS folder ON folder.id = membership.folder_id
LEFT JOIN projects AS project ON project.id = membership.project_id
LEFT JOIN sessions AS source ON source.id = membership.project_session_id
WHERE folder.id IS NULL
   OR project.id IS NULL
   OR source.id IS NULL
   OR folder.workspace_id IS NOT project.workspace_id
   OR project.source_session_id IS NULL
   OR membership.project_session_id IS NOT project.source_session_id
   OR source.project_id IS NOT project.id
LIMIT 1""",
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
                "Cannot enforce project graph consistency; a required table is missing."
            )
        columns = _columns(bind, table_name)
        if any(column_name not in columns for column_name in required_columns):
            raise RuntimeError(
                "Cannot enforce project graph consistency; a required column is missing."
            )

    for table_name, column_name, parent_table, ondelete in _REQUIRED_ROOT_FKS:
        column = _columns(bind, table_name)[column_name]
        column_type = column["type"]
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, "length", None) != 36
            or bool(column.get("nullable"))
            or column.get("default") is not None
            or not _has_expected_foreign_key(bind, table_name, column_name, parent_table, ondelete)
        ):
            raise RuntimeError(
                "Cannot enforce project graph consistency; an ownership root has an unexpected shape."
            )

    source_column = _columns(bind, "projects")["source_session_id"]
    source_type = source_column["type"]
    if (
        not isinstance(source_type, sa.String)
        or getattr(source_type, "length", None) != 36
        or not bool(source_column.get("nullable"))
        or source_column.get("default") is not None
        or not _has_expected_foreign_key(
            bind, "projects", "source_session_id", "sessions", "SET NULL"
        )
    ):
        raise RuntimeError(
            "Cannot enforce project graph consistency; project source has an unexpected shape."
        )


def _reject_name_collisions(bind: sa.Connection) -> None:
    for name, _table_name, _sql in _TRIGGERS:
        row = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot enforce project graph consistency; an owned trigger name is already in use."
            )


def _validate_data(bind: sa.Connection) -> None:
    for message, query in _PREFLIGHT_QUERIES:
        if bind.exec_driver_sql(query).first() is not None:
            raise RuntimeError(message)


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Project graph consistency migration is supported only on SQLite.")
    return bind


def _expected_trigger_sql(bind: sa.Connection) -> dict[str, tuple[str, str]]:
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
            "Cannot downgrade project graph consistency; an owned trigger is missing or altered."
        )
    return expected


def upgrade() -> None:
    bind = _sqlite_bind()
    _validate_schema(bind)
    _validate_data(bind)
    _reject_name_collisions(bind)
    for _name, _table_name, sql in _TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    bind = _sqlite_bind()
    expected = _expected_trigger_sql(bind)
    preparer = bind.dialect.identifier_preparer
    for name, _table_name, _sql in _TRIGGERS:
        if name not in expected:
            raise RuntimeError(
                "Cannot downgrade project graph consistency; an owned trigger is missing or altered."
            )
    for name, _table_name, _sql in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {preparer.quote(name)}")
