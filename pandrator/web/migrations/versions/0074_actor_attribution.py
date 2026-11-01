"""Add private sidecars for durable job and audit actor attribution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0074_actor_attribution"
down_revision = "0073_account_authentication"
branch_labels = None
depends_on = None

_JOB_ACTORS = "job_actors"
_AUDIT_ACTORS = "audit_actors"
_ACTOR_TABLES = (_JOB_ACTORS, _AUDIT_ACTORS)
_ACTOR_INDEXES = {
    _JOB_ACTORS: "ix_job_actors_user_id",
    _AUDIT_ACTORS: "ix_audit_actors_user_id",
}
_ACTOR_CHECKS = {
    _JOB_ACTORS: "ck_job_actors_user_auth_pair",
    _AUDIT_ACTORS: "ck_audit_actors_user_auth_pair",
}
_ACTOR_USER_FKS = {
    _JOB_ACTORS: "fk_job_actors_user_id_users",
    _AUDIT_ACTORS: "fk_audit_actors_user_id_users",
}
_ACTOR_PARENT_FKS = {
    _JOB_ACTORS: ("job_id", "jobs", "fk_job_actors_job_id_jobs", "CASCADE"),
    _AUDIT_ACTORS: (
        "audit_event_id",
        "audit_events",
        "fk_audit_actors_audit_event_id_audit_events",
        "CASCADE",
    ),
}
_COLUMN_SHAPES: dict[
    str, dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]]
] = {
    _JOB_ACTORS: {
        "job_id": (sa.String, 36, False, True),
        "user_id": (sa.String, 36, True, False),
        "auth_revision": (sa.Integer, None, True, False),
        "context_json": (sa.JSON, None, False, False),
        "created_at": (sa.DateTime, None, False, False),
    },
    _AUDIT_ACTORS: {
        "audit_event_id": (sa.Integer, None, False, True),
        "user_id": (sa.String, 36, True, False),
        "auth_revision": (sa.Integer, None, True, False),
        "context_json": (sa.JSON, None, False, False),
        "created_at": (sa.DateTime, None, False, False),
    },
}
_SOURCE_KEYS: dict[str, tuple[str, type[sa.types.TypeEngine[Any]], int | None]] = {
    "users": ("id", sa.String, 36),
    "jobs": ("id", sa.String, 36),
    "audit_events": ("id", sa.Integer, None),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _primary_key(bind: sa.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(sa.inspect(bind).get_pk_constraint(table_name).get("constrained_columns") or ())


def _normalize_sql(value: object) -> str:
    return re.sub(r"[\s\"`\[\]]+", "", str(value or "")).casefold()


def _source_key_matches(
    column: Mapping[str, Any], type_class: type[sa.types.TypeEngine[Any]], length: int | None
) -> bool:
    column_type = column["type"]
    if type_class is sa.Integer:
        # SQLite reflection returns its dialect-specific INTEGER subclass for
        # an INTEGER PRIMARY KEY. Keep the visit name exact so BIGINT and
        # SMALLINT source keys do not pass as the same storage shape.
        return (
            isinstance(column_type, sa.Integer)
            and str(getattr(column_type, "__visit_name__", "")).casefold()
            == type_class.__visit_name__.casefold()
        )
    return isinstance(column_type, type_class) and getattr(column_type, "length", None) == length


def _validate_source_keys(bind: sa.Connection) -> None:
    tables = _table_names(bind)
    for table_name, (column_name, type_class, length) in _SOURCE_KEYS.items():
        if table_name not in tables:
            raise RuntimeError(
                f"Cannot migrate actor attribution; required source table {table_name} is missing."
            )
        columns = _columns(bind, table_name)
        if (
            column_name not in columns
            or not _source_key_matches(columns[column_name], type_class, length)
            or _primary_key(bind, table_name) != (column_name,)
        ):
            raise RuntimeError(
                f"Cannot migrate actor attribution; {table_name}.{column_name} has an unexpected key shape."
            )


def _validate_foreign_key_integrity(bind: sa.Connection) -> None:
    if bind.dialect.name != "sqlite":
        return
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "Cannot migrate actor attribution; existing foreign-key integrity is invalid."
        )


def _index_signatures(
    bind: sa.Connection, table_name: str
) -> dict[str, tuple[bool, tuple[str, ...] | None]]:
    if bind.dialect.name == "sqlite":
        quote = bind.dialect.identifier_preparer.quote
        indexes = bind.exec_driver_sql(f"PRAGMA index_list({quote(table_name)})").mappings().all()
        result: dict[str, tuple[bool, tuple[str, ...] | None]] = {}
        for index in indexes:
            if str(index.get("origin", "")) != "c":
                continue
            name = str(index["name"])
            columns = bind.exec_driver_sql(f"PRAGMA index_info({quote(name)})").mappings().all()
            if any(column.get("name") is None for column in columns):
                column_names = None
            else:
                column_names = tuple(
                    str(column["name"]) for column in columns if column.get("name") is not None
                )
            result[name] = (bool(index.get("unique")), column_names)
        return result
    reflected_result: dict[str, tuple[bool, tuple[str, ...] | None]] = {}
    for index in sa.inspect(bind).get_indexes(table_name):
        columns = index.get("column_names")
        column_names = (
            None
            if columns is None or any(column is None for column in columns)
            else tuple(str(column) for column in columns)
        )
        reflected_result[str(index["name"])] = (bool(index.get("unique")), column_names)
    return reflected_result


def _unique_constraint_columns(bind: sa.Connection, table_name: str) -> set[tuple[str, ...]]:
    if bind.dialect.name == "sqlite":
        quote = bind.dialect.identifier_preparer.quote
        indexes = bind.exec_driver_sql(f"PRAGMA index_list({quote(table_name)})").mappings().all()
        result: set[tuple[str, ...]] = set()
        for index in indexes:
            if not bool(index.get("unique")) or str(index.get("origin", "")) != "u":
                continue
            name = str(index["name"])
            columns = bind.exec_driver_sql(f"PRAGMA index_info({quote(name)})").mappings().all()
            if any(column.get("name") is None for column in columns):
                raise RuntimeError(
                    f"Cannot migrate actor attribution; {table_name} has an expression unique constraint."
                )
            result.add(tuple(str(column["name"]) for column in columns))
        return result
    return {
        tuple(constraint.get("column_names") or ())
        for constraint in sa.inspect(bind).get_unique_constraints(table_name)
    }


def _check_signatures(bind: sa.Connection, table_name: str) -> set[tuple[str | None, str]]:
    return {
        (check.get("name"), _normalize_sql(check.get("sqltext")))
        for check in sa.inspect(bind).get_check_constraints(table_name)
    }


def _identifier_pattern(identifier: str) -> str:
    escaped = re.escape(identifier)
    return rf'(?:"{escaped}"|`{escaped}`|\[{escaped}\]|{escaped})'


def _has_named_foreign_key(
    bind: sa.Connection,
    table_name: str,
    constraint_name: str,
    column_name: str,
    referred_table: str,
    referred_column: str,
    ondelete: str,
) -> bool:
    if bind.dialect.name != "sqlite":
        return any(
            foreign_key.get("name") == constraint_name
            for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        )
    row = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table_name},
    ).first()
    if row is None or not row[0]:
        return False
    pattern = (
        rf"\bCONSTRAINT\s+{_identifier_pattern(constraint_name)}\s+"
        rf"FOREIGN\s+KEY\s*\(\s*{_identifier_pattern(column_name)}\s*\)\s*"
        rf"REFERENCES\s+{_identifier_pattern(referred_table)}\s*\(\s*"
        rf"{_identifier_pattern(referred_column)}\s*\)\s+ON\s+DELETE\s+{ondelete}\b"
    )
    return re.search(pattern, str(row[0]), re.IGNORECASE) is not None


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str, str, bool | None, str, str]]:
    signatures = set()
    for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
        options = foreign_key.get("options") or {}
        signatures.add(
            (
                tuple(foreign_key.get("constrained_columns") or ()),
                str(foreign_key.get("referred_table")),
                tuple(foreign_key.get("referred_columns") or ()),
                str(options.get("ondelete") or "").upper(),
                str(options.get("onupdate") or "").upper(),
                options.get("deferrable"),
                str(options.get("initially") or "").upper(),
                str(options.get("match") or "").upper(),
            )
        )
    return signatures


def _validate_named_foreign_key(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    referred_table: str,
    referred_column: str,
    constraint_name: str,
    ondelete: str,
) -> bool:
    return _has_named_foreign_key(
        bind,
        table_name,
        constraint_name,
        column_name,
        referred_table,
        referred_column,
        ondelete,
    )


def _validate_target_table(bind: sa.Connection, table_name: str) -> None:
    columns = _columns(bind, table_name)
    expected_columns = _COLUMN_SHAPES[table_name]
    if set(columns) != set(expected_columns):
        raise RuntimeError(
            f"Cannot migrate actor attribution; {table_name} has an unexpected column shape."
        )
    for name, (type_class, length, nullable, primary_key) in expected_columns.items():
        column = columns[name]
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or bool(column.get("nullable")) != nullable
            or bool(column.get("primary_key", False)) != primary_key
            or column.get("default") is not None
        ):
            raise RuntimeError(
                f"Cannot migrate actor attribution; {table_name}.{name} has an unexpected definition."
            )

    primary_key = "job_id" if table_name == _JOB_ACTORS else "audit_event_id"
    if _primary_key(bind, table_name) != (primary_key,):
        raise RuntimeError(
            f"Cannot migrate actor attribution; {table_name} has an unexpected primary key."
        )

    expected_checks = {
        (
            _ACTOR_CHECKS[table_name],
            "(user_idisnullandauth_revisionisnull)or(user_idisnotnullandauth_revisionisnotnullandauth_revision>=1)",
        )
    }
    if _check_signatures(bind, table_name) != expected_checks:
        raise RuntimeError(f"Cannot migrate actor attribution; {table_name} has unexpected checks.")
    if _unique_constraint_columns(bind, table_name):
        raise RuntimeError(
            f"Cannot migrate actor attribution; {table_name} has unexpected unique constraints."
        )
    if _index_signatures(bind, table_name) != {_ACTOR_INDEXES[table_name]: (False, ("user_id",))}:
        raise RuntimeError(
            f"Cannot migrate actor attribution; {table_name} has unexpected indexes."
        )

    parent_column, parent_table, parent_fk, parent_action = _ACTOR_PARENT_FKS[table_name]
    expected_fks = {
        (("user_id",), "users", ("id",), "RESTRICT", "", None, "", ""),
        ((parent_column,), parent_table, ("id",), parent_action, "", None, "", ""),
    }
    if _foreign_key_signatures(bind, table_name) != expected_fks:
        raise RuntimeError(
            f"Cannot migrate actor attribution; {table_name} has unexpected foreign keys."
        )
    for column_name, referred_table, constraint_name, ondelete in (
        (
            "user_id",
            "users",
            _ACTOR_USER_FKS[table_name],
            "RESTRICT",
        ),
        (parent_column, parent_table, parent_fk, parent_action),
    ):
        if not _validate_named_foreign_key(
            bind,
            table_name,
            column_name,
            referred_table,
            "id",
            constraint_name,
            ondelete,
        ):
            raise RuntimeError(
                f"Cannot migrate actor attribution; {table_name} has an unnamed or malformed foreign key."
            )

    triggers = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table_name LIMIT 1"
        ),
        {"table_name": table_name},
    ).first()
    if triggers:
        raise RuntimeError(
            f"Cannot migrate actor attribution; {table_name} has unexpected triggers."
        )


def _reserved_objects(bind: sa.Connection) -> list[tuple[str, str]]:
    names = (
        *_ACTOR_TABLES,
        *_ACTOR_INDEXES.values(),
    )
    placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
    params = {f"name_{index}": name.casefold() for index, name in enumerate(names)}
    return [
        (str(row[0]), str(row[1]))
        for row in bind.execute(
            sa.text(
                "SELECT type, name FROM sqlite_master WHERE lower(name) IN (" + placeholders + ")"
            ),
            params,
        ).all()
    ]


def _validate_target_tables(bind: sa.Connection, *, allow_absent: bool, require_empty: bool) -> str:
    tables = _table_names(bind)
    present = set(_ACTOR_TABLES) & tables
    if not present:
        if not allow_absent or _reserved_objects(bind):
            raise RuntimeError(
                "Cannot migrate actor attribution; attribution tables are missing or target names are occupied."
            )
        return "absent"
    if present != set(_ACTOR_TABLES):
        raise RuntimeError("Cannot migrate actor attribution; only some attribution tables exist.")
    for table_name in _ACTOR_TABLES:
        _validate_target_table(bind, table_name)
        if require_empty and bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first():
            raise RuntimeError(
                "Cannot migrate actor attribution; early attribution tables must be empty."
            )
    return "present"


def _validate_no_inbound_actor_references(bind: sa.Connection) -> None:
    for table_name in _table_names(bind) - set(_ACTOR_TABLES):
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
            if foreign_key.get("referred_table") in _ACTOR_TABLES:
                raise RuntimeError(
                    "Cannot downgrade actor attribution; another table references an actor sidecar."
                )


def _create_actor_tables() -> None:
    op.create_table(
        _JOB_ACTORS,
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("auth_revision", sa.Integer(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NULL AND auth_revision IS NULL) OR "
            "(user_id IS NOT NULL AND auth_revision IS NOT NULL AND auth_revision >= 1)",
            name=_ACTOR_CHECKS[_JOB_ACTORS],
        ),
        sa.ForeignKeyConstraint(
            ("job_id",),
            ("jobs.id",),
            name=_ACTOR_PARENT_FKS[_JOB_ACTORS][2],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name=_ACTOR_USER_FKS[_JOB_ACTORS],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(_ACTOR_INDEXES[_JOB_ACTORS], _JOB_ACTORS, ["user_id"], unique=False)
    op.create_table(
        _AUDIT_ACTORS,
        sa.Column("audit_event_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("auth_revision", sa.Integer(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NULL AND auth_revision IS NULL) OR "
            "(user_id IS NOT NULL AND auth_revision IS NOT NULL AND auth_revision >= 1)",
            name=_ACTOR_CHECKS[_AUDIT_ACTORS],
        ),
        sa.ForeignKeyConstraint(
            ("audit_event_id",),
            ("audit_events.id",),
            name=_ACTOR_PARENT_FKS[_AUDIT_ACTORS][2],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name=_ACTOR_USER_FKS[_AUDIT_ACTORS],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("audit_event_id"),
    )
    op.create_index(_ACTOR_INDEXES[_AUDIT_ACTORS], _AUDIT_ACTORS, ["user_id"], unique=False)


def upgrade() -> None:
    bind = op.get_bind()
    _validate_source_keys(bind)
    _validate_foreign_key_integrity(bind)
    target_state = _validate_target_tables(bind, allow_absent=True, require_empty=True)
    if target_state == "absent":
        _create_actor_tables()
    _validate_foreign_key_integrity(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _validate_source_keys(bind)
    _validate_foreign_key_integrity(bind)
    _validate_target_tables(bind, allow_absent=False, require_empty=False)
    for table_name in _ACTOR_TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first():
            raise RuntimeError(
                "Cannot downgrade actor attribution; attribution records must be retained."
            )
    _validate_no_inbound_actor_references(bind)
    op.drop_table(_AUDIT_ACTORS)
    op.drop_table(_JOB_ACTORS)
    _validate_foreign_key_integrity(bind)
