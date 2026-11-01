"""Make established resource ownership roots mandatory."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import SAWarning

revision = "0075_mandatory_ownership"
down_revision = "0074_actor_attribution"
branch_labels = None
depends_on = None

_ROOTS: tuple[tuple[str, str, str, str], ...] = (
    ("providers", "workspace_id", "workspaces", "RESTRICT"),
    ("sessions", "project_id", "projects", "RESTRICT"),
    ("translation_projects", "project_root_id", "projects", "RESTRICT"),
    ("project_folders", "workspace_id", "workspaces", "RESTRICT"),
    ("project_folder_memberships", "project_id", "projects", "CASCADE"),
    ("source_assets", "workspace_id", "workspaces", "RESTRICT"),
    ("jobs", "workspace_id", "workspaces", "RESTRICT"),
    ("artifacts", "workspace_id", "workspaces", "RESTRICT"),
    ("upload_sessions", "workspace_id", "workspaces", "RESTRICT"),
    ("research_cache_entries", "workspace_id", "workspaces", "RESTRICT"),
    ("pronunciation_entries", "workspace_id", "workspaces", "RESTRICT"),
    ("voices", "workspace_id", "workspaces", "RESTRICT"),
    ("voice_samples", "workspace_id", "workspaces", "RESTRICT"),
    ("training_runs", "workspace_id", "workspaces", "RESTRICT"),
    ("usage_events", "workspace_id", "workspaces", "RESTRICT"),
    ("voice_collections", "workspace_id", "workspaces", "RESTRICT"),
    ("voice_collection_members", "workspace_id", "workspaces", "RESTRICT"),
    ("sound_items", "workspace_id", "workspaces", "RESTRICT"),
    ("sound_variants", "workspace_id", "workspaces", "RESTRICT"),
)

_SQLITE_REFLECTION_WARNING = r"Skipped unsupported reflection of expression-based index .*"


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _validate_roots(bind: sa.Connection, *, nullable: bool) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    preparer = bind.dialect.identifier_preparer

    for table_name, column_name, parent_table, ondelete in _ROOTS:
        if table_name not in tables:
            raise RuntimeError(
                f"Cannot enforce mandatory ownership; required root table {table_name} is missing."
            )
        if parent_table not in tables:
            raise RuntimeError(
                f"Cannot enforce mandatory ownership; {table_name}.{column_name} references missing parent table {parent_table}."
            )

        columns = _columns(bind, table_name)
        column = columns.get(column_name)
        if column is None:
            raise RuntimeError(
                f"Cannot enforce mandatory ownership; {table_name}.{column_name} is missing."
            )
        column_type = column["type"]
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, "length", None) != 36
            or bool(column.get("nullable")) is not nullable
            or column.get("default") is not None
        ):
            shape = "mandatory" if not nullable else "nullable predecessor"
            raise RuntimeError(
                f"Cannot enforce mandatory ownership; {table_name}.{column_name} has an unexpected {shape} shape."
            )

        foreign_keys = [
            foreign_key
            for foreign_key in inspector.get_foreign_keys(table_name)
            if column_name in (foreign_key.get("constrained_columns") or ())
        ]
        if len(foreign_keys) != 1:
            raise RuntimeError(
                f"Cannot enforce mandatory ownership; {table_name}.{column_name} has an unexpected foreign key shape."
            )
        foreign_key = foreign_keys[0]
        options = foreign_key.get("options") or {}
        if (
            tuple(foreign_key.get("constrained_columns") or ()) != (column_name,)
            or foreign_key.get("referred_table") != parent_table
            or tuple(foreign_key.get("referred_columns") or ()) != ("id",)
            or str(options.get("ondelete") or "").upper() != ondelete
        ):
            raise RuntimeError(
                f"Cannot enforce mandatory ownership; {table_name}.{column_name} has an unexpected foreign key shape."
            )

        if nullable:
            quoted_table = preparer.quote(table_name)
            quoted_column = preparer.quote(column_name)
            has_null = bind.exec_driver_sql(
                f"SELECT 1 FROM {quoted_table} WHERE {quoted_column} IS NULL LIMIT 1"
            ).first()
            if has_null:
                raise RuntimeError(
                    f"Cannot enforce mandatory ownership; {table_name}.{column_name} contains NULL values."
                )


def _sqlite_owned_sql(bind: sa.Connection) -> tuple[tuple[str, str, str, str], ...]:
    if bind.dialect.name != "sqlite":
        return ()
    parameters = {f"table_{index}": table for index, (table, *_rest) in enumerate(_ROOTS)}
    table_params = ", ".join(f":table_{index}" for index in range(len(_ROOTS)))
    rows = bind.execute(
        sa.text(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL "
            f"AND tbl_name IN ({table_params}) ORDER BY type, name"
        ),
        parameters,
    ).all()
    return tuple((str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows)


def _restore_sqlite_owned_sql(
    bind: sa.Connection,
    saved_objects: tuple[tuple[str, str, str, str], ...],
) -> None:
    preparer = bind.dialect.identifier_preparer
    for object_type, name, table_name, sql in saved_objects:
        existing = bind.execute(
            sa.text("SELECT type, tbl_name, sql FROM sqlite_master WHERE name = :name"),
            {"name": name},
        ).first()
        if existing is not None:
            existing_type, existing_table, existing_sql = (
                str(existing[0]),
                str(existing[1]),
                str(existing[2]) if existing[2] is not None else None,
            )
            if (
                existing_type == object_type
                and existing_table == table_name
                and existing_sql == sql
            ):
                continue
            if existing_type != object_type or existing_table != table_name:
                raise RuntimeError(
                    f"Cannot restore mandatory-ownership schema object {name}; its name is occupied."
                )
            quoted_name = preparer.quote(name)
            if object_type == "index":
                bind.exec_driver_sql(f"DROP INDEX {quoted_name}")
            elif object_type == "trigger":
                bind.exec_driver_sql(f"DROP TRIGGER {quoted_name}")
            else:
                raise RuntimeError(
                    f"Cannot restore mandatory-ownership schema object {name}; its type is unsupported."
                )
        bind.exec_driver_sql(sql)


def _alter_roots(bind: sa.Connection, *, nullable: bool) -> None:
    def alter() -> None:
        for table_name, column_name, _parent_table, _ondelete in _ROOTS:
            reflected_table = sa.Table(
                table_name,
                sa.MetaData(),
                autoload_with=bind,
                resolve_fks=False,
            )
            with op.batch_alter_table(table_name, copy_from=reflected_table) as batch_op:
                batch_op.alter_column(
                    column_name,
                    existing_type=sa.String(length=36),
                    existing_nullable=not nullable,
                    nullable=nullable,
                )

    if bind.dialect.name == "sqlite":
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=_SQLITE_REFLECTION_WARNING,
                category=SAWarning,
            )
            alter()
        return
    alter()


def upgrade() -> None:
    bind = op.get_bind()
    _validate_roots(bind, nullable=True)
    saved_objects = _sqlite_owned_sql(bind)
    _alter_roots(bind, nullable=False)
    _restore_sqlite_owned_sql(bind, saved_objects)


def downgrade() -> None:
    bind = op.get_bind()
    _validate_roots(bind, nullable=False)
    saved_objects = _sqlite_owned_sql(bind)
    _alter_roots(bind, nullable=True)
    _restore_sqlite_owned_sql(bind, saved_objects)
