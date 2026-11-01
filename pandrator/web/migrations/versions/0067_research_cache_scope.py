"""Add private workspace and project scope to persistent research cache entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0067_research_cache_scope"
down_revision = "0066_job_ownership"
branch_labels = None
depends_on = None

_TABLE = "research_cache_entries"
_SCOPE_COLUMNS = ("workspace_id", "project_id")
_WORKSPACE_FK = "fk_research_cache_entries_workspace_id_workspaces"
_PROJECT_FK = "fk_research_cache_entries_project_id_projects"
_WORKSPACE_INDEX = "ix_research_cache_entries_workspace_id"
_PROJECT_INDEX = "ix_research_cache_entries_project_id"
_OLD_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "cache_key": (sa.String, 64, False, True),
    "provider": (sa.String, 40, False, False),
    "operation": (sa.String, 40, False, False),
    "request_json": (sa.JSON, None, False, False),
    "response_json": (sa.JSON, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "expires_at": (sa.DateTime, None, False, False),
}
_OLD_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_research_cache_entries_provider": ("provider",),
    "ix_research_cache_entries_operation": ("operation",),
    "ix_research_cache_entries_expires_at": ("expires_at",),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required_columns = {
        _TABLE: set(_OLD_COLUMNS),
        "projects": {"id"},
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
    }
    missing_tables = set(required_columns) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot scope research cache; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected_columns in required_columns.items():
        missing_columns = expected_columns - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot scope research cache; {table_name} is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_key_signatures(
    bind: sa.Connection,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str | None, str | None]]:
    return {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete", "").upper() or None,
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
                "Cannot scope research cache; an unexpected table references "
                f"{_TABLE!r} through a foreign key."
            )


def _validate_scope_column(
    bind: sa.Connection,
    *,
    column_name: str,
    foreign_key_name: str,
    referred_table: str,
    ondelete: str,
    index_name: str,
) -> None:
    column = _columns(bind, _TABLE).get(column_name)
    if (
        column is None
        or not column["nullable"]
        or not isinstance(column["type"], sa.String)
        or column["type"].length != 36
        or column.get("default") is not None
        or column.get("primary_key")
    ):
        raise RuntimeError(
            f"Cannot scope research cache; {_TABLE}.{column_name} has an unexpected definition."
        )

    indexes = [
        index for index in sa.inspect(bind).get_indexes(_TABLE) if index.get("name") == index_name
    ]
    if (
        len(indexes) != 1
        or indexes[0].get("column_names") != [column_name]
        or indexes[0].get("unique")
    ):
        raise RuntimeError(
            f"Cannot scope research cache; {_TABLE}.{column_name} has a malformed ownership index."
        )

    foreign_keys = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
        if column_name in foreign_key["constrained_columns"]
    ]
    if len(foreign_keys) != 1:
        raise RuntimeError(
            f"Cannot scope research cache; {_TABLE}.{column_name} must have one ownership foreign key."
        )
    foreign_key = foreign_keys[0]
    if (
        foreign_key.get("name") != foreign_key_name
        or foreign_key["constrained_columns"] != [column_name]
        or foreign_key["referred_table"] != referred_table
        or foreign_key["referred_columns"] != ["id"]
        or (foreign_key.get("options") or {}).get("ondelete", "").upper() != ondelete
    ):
        raise RuntimeError(
            f"Cannot scope research cache; {_TABLE}.{column_name} has an unexpected foreign key."
        )


def _validate_cache_schema(bind: sa.Connection, *, target: bool) -> None:
    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if target and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot scope research cache; only part of its target schema exists.")
    if not target and present_scope:
        raise RuntimeError("Cannot scope research cache; scope columns already exist.")
    expected_columns = set(_OLD_COLUMNS) | (set(_SCOPE_COLUMNS) if target else set())
    if set(columns) != expected_columns:
        raise RuntimeError("Cannot scope research cache; its columns have an unexpected shape.")

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
                f"Cannot scope research cache; {_TABLE}.{name} has an unexpected definition."
            )

    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != ("cache_key",):
        raise RuntimeError("Cannot scope research cache; cache_key is no longer its primary key.")

    if sa.inspect(bind).get_unique_constraints(_TABLE) or sa.inspect(bind).get_check_constraints(
        _TABLE
    ):
        raise RuntimeError(
            "Cannot scope research cache; the table contains unexpected constraints."
        )

    expected_foreign_keys = (
        {
            (("workspace_id",), "workspaces", ("id",), "RESTRICT", _WORKSPACE_FK),
            (("project_id",), "projects", ("id",), "CASCADE", _PROJECT_FK),
        }
        if target
        else set()
    )
    if _foreign_key_signatures(bind) != expected_foreign_keys:
        raise RuntimeError(
            "Cannot scope research cache; its foreign keys have an unexpected shape."
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
    for index_name, index_columns in required_indexes.items():
        matches = [index for index in indexes if index.get("name") == index_name]
        if (
            len(matches) != 1
            or matches[0].get("column_names") != list(index_columns)
            or matches[0].get("unique")
        ):
            raise RuntimeError(
                f"Cannot scope research cache; index {index_name!r} is missing or malformed."
            )


def _validate_empty_target(bind: sa.Connection) -> None:
    populated = bind.execute(
        sa.text(
            "SELECT cache_key FROM research_cache_entries "
            "WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL "
            "ORDER BY cache_key LIMIT 1"
        )
    ).first()
    if populated is not None:
        raise RuntimeError(
            "Cannot scope research cache; an early target row already contains "
            f"ownership data for key {populated[0]!r}."
        )


def _stored_nonblank_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Cannot scope research cache; {label} is missing or malformed.")
    return value


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
            "Cannot scope legacy research cache; the seeded owner is missing, "
            "ambiguous, inactive, or not an instance admin."
        )
    owner_id = _stored_nonblank_id(owners[0]["id"], label="seeded owner ID")
    workspaces = (
        bind.execute(
            sa.text(
                "SELECT id, kind, state FROM workspaces "
                "WHERE personal_owner_user_id = :owner_id ORDER BY id"
            ),
            {"owner_id": owner_id},
        )
        .mappings()
        .all()
    )
    if (
        len(workspaces) != 1
        or workspaces[0]["kind"] != "personal"
        or workspaces[0]["state"] != "active"
    ):
        raise RuntimeError(
            "Cannot scope legacy research cache; the seeded owner personal "
            "workspace is missing, ambiguous, or inactive."
        )
    workspace_id = _stored_nonblank_id(workspaces[0]["id"], label="seeded personal workspace ID")
    memberships = (
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
    if memberships != ["owner"]:
        raise RuntimeError(
            "Cannot scope legacy research cache; the seeded owner membership is missing or invalid."
        )
    return workspace_id


def _cache_index_definitions(
    bind: sa.Connection, *, excluded_names: set[str] | None = None
) -> dict[str, str]:
    if bind.dialect.name != "sqlite":
        return {}
    excluded_names = excluded_names or set()
    scope_columns = set(_SCOPE_COLUMNS)
    scope_dependent = {
        str(index["name"])
        for index in sa.inspect(bind).get_indexes(_TABLE)
        if index.get("name") is not None and scope_columns & set(index.get("column_names") or ())
    }
    excluded_names = excluded_names | scope_dependent
    rows = bind.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'research_cache_entries' AND sql IS NOT NULL "
        "ORDER BY name"
    ).all()
    return {
        str(name): str(sql) for name, sql in rows if name not in excluded_names and sql is not None
    }


def _restore_cache_index_definitions(bind: sa.Connection, definitions: Mapping[str, str]) -> None:
    if not definitions or bind.dialect.name != "sqlite":
        return
    current = _cache_index_definitions(bind)
    quote = bind.dialect.identifier_preparer.quote
    for name, sql in definitions.items():
        if current.get(name) == sql:
            continue
        if name in current:
            bind.exec_driver_sql(f"DROP INDEX {quote(name)}")
        bind.exec_driver_sql(sql)


def _ensure_scope_schema(bind: sa.Connection, *, existing_scope: bool) -> None:
    if not existing_scope:
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
            batch.add_column(sa.Column("project_id", sa.String(36), nullable=True))
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
            batch.create_index(_WORKSPACE_INDEX, ["workspace_id"])
            batch.create_index(_PROJECT_INDEX, ["project_id"])
    _validate_cache_schema(bind, target=True)


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)
    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if present_scope and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot scope research cache; only part of its target schema exists.")
    existing_scope = bool(present_scope)
    _validate_cache_schema(bind, target=existing_scope)
    if existing_scope:
        _validate_empty_target(bind)

    workspace_id = _initial_personal_workspace_id(bind)
    cache_keys = (
        bind.execute(sa.text("SELECT cache_key FROM research_cache_entries ORDER BY cache_key"))
        .scalars()
        .all()
    )
    old_indexes = {} if existing_scope else _cache_index_definitions(bind)

    _ensure_scope_schema(bind, existing_scope=existing_scope)
    _restore_cache_index_definitions(bind, old_indexes)
    for cache_key in cache_keys:
        result = bind.execute(
            sa.text(
                "UPDATE research_cache_entries SET workspace_id = :workspace_id, project_id = NULL "
                "WHERE cache_key = :cache_key AND workspace_id IS NULL AND project_id IS NULL"
            ),
            {"workspace_id": workspace_id, "cache_key": cache_key},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Cannot scope research cache; an entry changed during ownership migration."
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in _table_names(bind):
        return
    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if not present_scope:
        return
    if present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot remove research-cache scope; only part of the schema exists.")
    _validate_cache_schema(bind, target=True)
    old_indexes = _cache_index_definitions(bind, excluded_names={_WORKSPACE_INDEX, _PROJECT_INDEX})
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_index(_WORKSPACE_INDEX)
        batch.drop_index(_PROJECT_INDEX)
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_constraint(_PROJECT_FK, type_="foreignkey")
        batch.drop_column("workspace_id")
        batch.drop_column("project_id")
    _restore_cache_index_definitions(bind, old_indexes)
    _validate_cache_schema(bind, target=False)
