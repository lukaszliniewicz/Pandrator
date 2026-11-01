"""Adopt workspace and project ownership for pronunciation entries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0069_pronunciation_scope"
down_revision = "0068_usage_ownership"
branch_labels = None
depends_on = None

_TABLE = "pronunciation_entries"
_SCOPE_COLUMNS = ("workspace_id", "project_id")
_WORKSPACE_FK = "fk_pronunciation_entries_workspace_id_workspaces"
_PROJECT_FK = "fk_pronunciation_entries_project_id_projects"
_WORKSPACE_INDEX = "ix_pronunciation_entries_workspace_id"
_PROJECT_INDEX = "ix_pronunciation_entries_project_id"
_GLOBAL_INDEX = "uq_pronunciation_entries_workspace_global"
_GLOBAL_INDEX_COLUMNS = ("workspace_id", "normalized_form", "language", "backend")

# Migration 0016 created these fields without SQL defaults. Python-side model
# defaults never appeared in the historical table definition.
_OLD_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "id": (sa.String, 36, False, True),
    "scope": (sa.String, 16, False, False),
    "session_id": (sa.String, 36, True, False),
    "source_form": (sa.Text, None, False, False),
    "normalized_form": (sa.String, 512, False, False),
    "language": (sa.String, 40, False, False),
    "phonetic": (sa.Text, None, False, False),
    "alphabet": (sa.String, 32, False, False),
    "backend": (sa.String, 80, False, False),
    "status": (sa.String, 24, False, False),
    "source": (sa.String, 40, False, False),
    "notes": (sa.Text, None, True, False),
    "metadata_json": (sa.JSON, None, False, False),
    "revision": (sa.Integer, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
}
_OLD_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_pronunciation_entries_scope": ("scope",),
    "ix_pronunciation_entries_session_id": ("session_id",),
    "ix_pronunciation_entries_normalized_form": ("normalized_form",),
    "ix_pronunciation_entries_language": ("language",),
    "ix_pronunciation_entries_backend": ("backend",),
    "ix_pronunciation_entries_status": ("status",),
}
_OLD_UNIQUE = (
    "uq_pronunciation_entry_identity",
    ("scope", "session_id", "normalized_form", "language", "backend"),
)
_OLD_FOREIGN_KEYS: set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]] = {
    (("session_id",), "sessions", ("id",), "CASCADE", None),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {
        _TABLE: set(_OLD_COLUMNS),
        "projects": {"id", "workspace_id", "source_session_id"},
        "sessions": {"id", "project_id"},
        "translation_projects": {"id", "source_session_id", "project_root_id"},
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
    }
    missing_tables = set(required) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected_columns in required.items():
        missing_columns = expected_columns - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; {table_name} is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_key_signatures(
    bind: sa.Connection,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]]:
    return {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete", "").upper(),
            foreign_key.get("name"),
        )
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
    }


def _validate_no_inbound_foreign_keys(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    for table_name in sorted(_table_names(bind) - {_TABLE}):
        if any(
            foreign_key["referred_table"] == _TABLE
            for foreign_key in inspector.get_foreign_keys(table_name)
        ):
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; an unexpected table references {_TABLE!r}."
            )


def _index_definitions(
    bind: sa.Connection, *, exclude_names: set[str] | None = None
) -> dict[str, str]:
    excluded = exclude_names or set()
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"
        ),
        {"table_name": _TABLE},
    ).all()
    return {str(name): str(sql) for name, sql in rows if str(name) not in excluded}


def _restore_index_definitions(bind: sa.Connection, definitions: Mapping[str, str]) -> None:
    preparer = bind.dialect.identifier_preparer
    for name, sql in definitions.items():
        bind.exec_driver_sql(f"DROP INDEX IF EXISTS {preparer.quote(name)}")
        bind.exec_driver_sql(sql)


def _validate_global_unique_index(bind: sa.Connection, indexes: Sequence[Any]) -> None:
    matches = [index for index in indexes if index.get("name") == _GLOBAL_INDEX]
    if (
        len(matches) != 1
        or matches[0].get("column_names") != list(_GLOBAL_INDEX_COLUMNS)
        or not matches[0].get("unique")
    ):
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; the workspace-global unique index is malformed."
        )

    sql = _index_definitions(bind).get(_GLOBAL_INDEX, "")
    where = re.search(r"\bWHERE\b(.*)$", sql, re.IGNORECASE | re.DOTALL)
    if where is None:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; the workspace-global index is not partial."
        )
    predicate_parts = re.split(r"('(?:''|[^'])*')", where.group(1))
    predicate = "".join(
        part if part.startswith("'") else re.sub(r'[\s"`()\[\]]+', "", part).lower()
        for part in predicate_parts
    )
    if predicate not in {
        "scope='global'andsession_idisnull",
        "session_idisnullandscope='global'",
    }:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; the workspace-global index predicate is malformed."
        )


def _validate_schema(bind: sa.Connection, *, target: bool) -> None:
    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if target and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; only part of the target columns exists."
        )
    if not target and present_scope:
        raise RuntimeError("Cannot adopt pronunciation ownership; scope columns already exist.")

    expected_columns = set(_OLD_COLUMNS) | (set(_SCOPE_COLUMNS) if target else set())
    if set(columns) != expected_columns:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; pronunciation_entries has an unexpected column shape."
        )
    for name, (type_class, length, nullable, primary_key) in _OLD_COLUMNS.items():
        column = columns[name]
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or column.get("default") is not None
            or column.get("primary_key", False) != primary_key
        ):
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; {_TABLE}.{name} has an unexpected definition."
            )
    if target:
        for name in _SCOPE_COLUMNS:
            column = columns[name]
            if (
                not isinstance(column["type"], sa.String)
                or column["type"].length != 36
                or not column["nullable"]
                or column.get("default") is not None
                or column.get("primary_key")
            ):
                raise RuntimeError(
                    f"Cannot adopt pronunciation ownership; {_TABLE}.{name} has an unexpected definition."
                )

    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != ("id",):
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; pronunciation_entries.id is no longer the primary key."
        )
    unique_constraints = {
        (constraint.get("name"), tuple(constraint.get("column_names") or ()))
        for constraint in sa.inspect(bind).get_unique_constraints(_TABLE)
    }
    if unique_constraints != {_OLD_UNIQUE}:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; the session identity constraint changed."
        )
    if sa.inspect(bind).get_check_constraints(_TABLE):
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; pronunciation_entries has unexpected checks."
        )

    expected_foreign_keys = set(_OLD_FOREIGN_KEYS)
    if target:
        expected_foreign_keys.update(
            {
                (("workspace_id",), "workspaces", ("id",), "RESTRICT", _WORKSPACE_FK),
                (("project_id",), "projects", ("id",), "CASCADE", _PROJECT_FK),
            }
        )
    if _foreign_key_signatures(bind) != expected_foreign_keys:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; pronunciation_entries has an unexpected foreign-key shape."
        )

    indexes = sa.inspect(bind).get_indexes(_TABLE)
    required_indexes = dict(_OLD_INDEXES)
    if target:
        required_indexes.update(
            {
                _WORKSPACE_INDEX: ("workspace_id",),
                _PROJECT_INDEX: ("project_id",),
            }
        )
    elif any(
        index.get("name") in {_WORKSPACE_INDEX, _PROJECT_INDEX, _GLOBAL_INDEX} for index in indexes
    ):
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; target indexes exist without target columns."
        )

    for index_name, index_columns in required_indexes.items():
        matches = [index for index in indexes if index.get("name") == index_name]
        if (
            len(matches) != 1
            or matches[0].get("column_names") != list(index_columns)
            or matches[0].get("unique")
        ):
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; index {index_name!r} is missing or malformed."
            )
    if target:
        _validate_global_unique_index(bind, indexes)


def _stored_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"Cannot adopt pronunciation ownership; {label} is missing or malformed."
        )
    return value


def _workspace_state(bind: sa.Connection, workspace_id: str) -> str:
    workspace_id = _stored_id(workspace_id, label="workspace ID")
    row = (
        bind.execute(
            sa.text("SELECT state FROM workspaces WHERE id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["state"] not in {"active", "archived"}:
        raise RuntimeError(
            f"Cannot adopt pronunciation ownership; workspace {workspace_id!r} is missing or invalid."
        )
    return str(row["state"])


def _validate_translation_roots(
    bind: sa.Connection, *, project_id: str, source_session_id: str | None
) -> None:
    if source_session_id is None:
        rows = (
            bind.execute(
                sa.text(
                    "SELECT id, source_session_id, project_root_id FROM translation_projects "
                    "WHERE project_root_id = :project_id ORDER BY id"
                ),
                {"project_id": project_id},
            )
            .mappings()
            .all()
        )
    else:
        rows = (
            bind.execute(
                sa.text(
                    "SELECT id, source_session_id, project_root_id FROM translation_projects "
                    "WHERE project_root_id = :project_id OR source_session_id = :source_session_id "
                    "ORDER BY id"
                ),
                {"project_id": project_id, "source_session_id": source_session_id},
            )
            .mappings()
            .all()
        )
    for row in rows:
        translation_id = _stored_id(row["id"], label="translation project ID")
        translation_source = _stored_id(
            row["source_session_id"],
            label=f"translation project {translation_id!r} source session ID",
        )
        source = (
            bind.execute(
                sa.text("SELECT project_id FROM sessions WHERE id = :session_id"),
                {"session_id": translation_source},
            )
            .mappings()
            .first()
        )
        if source is None or source["project_id"] != project_id:
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; project {project_id!r} conflicts with a translation source root."
            )
        if row["project_root_id"] is not None and row["project_root_id"] != project_id:
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; project {project_id!r} conflicts with a translation root."
            )


def _project_owner(bind: sa.Connection, project_id: str) -> tuple[str, str]:
    project_id = _stored_id(project_id, label="project ID")
    row = (
        bind.execute(
            sa.text("SELECT workspace_id, source_session_id FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"Cannot adopt pronunciation ownership; project {project_id!r} is missing."
        )
    workspace_id = _stored_id(row["workspace_id"], label=f"project {project_id!r} workspace ID")
    _workspace_state(bind, workspace_id)

    source_session_id: str | None = None
    if row["source_session_id"] is not None:
        source_session_id = _stored_id(
            row["source_session_id"], label=f"project {project_id!r} source session ID"
        )
        source = (
            bind.execute(
                sa.text("SELECT project_id FROM sessions WHERE id = :session_id"),
                {"session_id": source_session_id},
            )
            .mappings()
            .first()
        )
        if source is None or source["project_id"] != project_id:
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; project {project_id!r} has an invalid source root."
            )
    else:
        remaining = bind.execute(
            sa.text("SELECT COUNT(*) FROM sessions WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
        if int(remaining) != 0:
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; project {project_id!r} has sessions but no source session."
            )

    _validate_translation_roots(
        bind,
        project_id=project_id,
        source_session_id=source_session_id,
    )
    return workspace_id, project_id


def _session_owner(bind: sa.Connection, session_id: str) -> tuple[str, str]:
    session_id = _stored_id(session_id, label="session ID")
    row = (
        bind.execute(
            sa.text("SELECT project_id FROM sessions WHERE id = :session_id"),
            {"session_id": session_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"Cannot adopt pronunciation ownership; session {session_id!r} is missing."
        )
    project_id = _stored_id(row["project_id"], label=f"session {session_id!r} project ID")
    return _project_owner(bind, project_id)


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
    owners = (
        bind.execute(
            sa.text(
                "SELECT id, is_active, is_instance_admin FROM users "
                "WHERE legacy_subject = 'owner' ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    if (
        len(owners) != 1
        or not bool(owners[0]["is_active"])
        or not bool(owners[0]["is_instance_admin"])
    ):
        raise RuntimeError(
            "Cannot adopt global pronunciation entries; seeded owner is missing, ambiguous, inactive, or not an instance admin."
        )
    owner_id = _stored_id(owners[0]["id"], label="seeded owner ID")
    workspaces = (
        bind.execute(
            sa.text(
                "SELECT id, state FROM workspaces "
                "WHERE kind = 'personal' AND personal_owner_user_id = :owner_id ORDER BY id"
            ),
            {"owner_id": owner_id},
        )
        .mappings()
        .all()
    )
    if len(workspaces) != 1 or workspaces[0]["state"] != "active":
        raise RuntimeError(
            "Cannot adopt global pronunciation entries; seeded personal workspace is missing, ambiguous, or inactive."
        )
    workspace_id = _stored_id(workspaces[0]["id"], label="seeded personal workspace ID")
    membership = (
        bind.execute(
            sa.text(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND user_id = :owner_id ORDER BY role"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )
        .scalars()
        .all()
    )
    if membership != ["owner"]:
        raise RuntimeError(
            "Cannot adopt global pronunciation entries; the seeded owner workspace membership is missing or invalid."
        )
    return workspace_id


def _load_plan(
    bind: sa.Connection, *, fallback_workspace_id: str
) -> dict[str, tuple[str, str | None]]:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, scope, session_id, normalized_form, language, backend "
                "FROM pronunciation_entries ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    plan: dict[str, tuple[str, str | None]] = {}
    global_keys: dict[tuple[Any, Any, Any, Any], str] = {}
    for row in rows:
        entry_id = _stored_id(row["id"], label="pronunciation entry ID")
        scope = row["scope"]
        session_value = row["session_id"]
        if scope == "global":
            if session_value is not None:
                raise RuntimeError(
                    f"Cannot adopt pronunciation ownership; global entry {entry_id!r} has a session ID."
                )
            owner = (fallback_workspace_id, None)
            key = (
                owner[0],
                row["normalized_form"],
                row["language"],
                row["backend"],
            )
            previous = global_keys.get(key)
            if previous is not None:
                raise RuntimeError(
                    "Cannot adopt pronunciation ownership; duplicate global entries "
                    f"{previous!r} and {entry_id!r} share one workspace identity."
                )
            global_keys[key] = entry_id
        elif scope == "session":
            if session_value is None:
                raise RuntimeError(
                    f"Cannot adopt pronunciation ownership; session entry {entry_id!r} has no session ID."
                )
            session_id = _stored_id(
                session_value, label=f"pronunciation entry {entry_id!r} session ID"
            )
            owner = _session_owner(bind, session_id)
        else:
            raise RuntimeError(
                f"Cannot adopt pronunciation ownership; entry {entry_id!r} has invalid scope {scope!r}."
            )
        if entry_id in plan:
            raise RuntimeError("Cannot adopt pronunciation ownership; entry IDs are not unique.")
        plan[entry_id] = owner
    return plan


def _validate_empty_target(bind: sa.Connection) -> None:
    row = bind.execute(
        sa.text(
            "SELECT id FROM pronunciation_entries "
            "WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL ORDER BY id LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; an early target entry already has owner "
            f"values on row {row[0]!r}."
        )


def _adopt_schema(bind: sa.Connection, old_indexes: Mapping[str, str]) -> None:
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            _WORKSPACE_FK,
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            _PROJECT_FK,
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )
    _restore_index_definitions(bind, old_indexes)
    op.create_index(_WORKSPACE_INDEX, _TABLE, ["workspace_id"], unique=False)
    op.create_index(_PROJECT_INDEX, _TABLE, ["project_id"], unique=False)
    op.create_index(
        _GLOBAL_INDEX,
        _TABLE,
        list(_GLOBAL_INDEX_COLUMNS),
        unique=True,
        sqlite_where=sa.text("scope = 'global' AND session_id IS NULL"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)

    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if present_scope == set(_SCOPE_COLUMNS):
        _validate_schema(bind, target=True)
        _validate_empty_target(bind)
        early_target = True
    elif present_scope:
        raise RuntimeError(
            "Cannot adopt pronunciation ownership; only part of the target columns exists."
        )
    else:
        _validate_schema(bind, target=False)
        early_target = False

    fallback_workspace_id = _initial_personal_workspace_id(bind)
    plan = _load_plan(bind, fallback_workspace_id=fallback_workspace_id)
    old_indexes = {} if early_target else _index_definitions(bind)
    if not early_target:
        _adopt_schema(bind, old_indexes)
    _validate_schema(bind, target=True)

    for entry_id, (workspace_id, project_id) in plan.items():
        bind.execute(
            sa.text(
                "UPDATE pronunciation_entries SET workspace_id = :workspace_id, "
                "project_id = :project_id WHERE id = :entry_id"
            ),
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "entry_id": entry_id,
            },
        )

    _validate_schema(bind, target=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)
    _validate_schema(bind, target=True)
    old_indexes = _index_definitions(
        bind,
        exclude_names={_WORKSPACE_INDEX, _PROJECT_INDEX, _GLOBAL_INDEX},
    )
    op.drop_index(_GLOBAL_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_constraint(_PROJECT_FK, type_="foreignkey")
        batch.drop_index(_WORKSPACE_INDEX)
        batch.drop_index(_PROJECT_INDEX)
        batch.drop_column("workspace_id")
        batch.drop_column("project_id")
    _restore_index_definitions(bind, old_indexes)
    _validate_schema(bind, target=False)
