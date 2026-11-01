"""Scope stored credentials to an owning workspace."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0070_credential_namespaces"
down_revision = "0069_pronunciation_scope"
branch_labels = None
depends_on = None

_TABLE = "stored_credentials"
_WORKSPACE_COLUMN = "workspace_id"
_REVISION_COLUMN = "revision"
_WORKSPACE_FK = "fk_stored_credentials_workspace_id_workspaces"
_PRIMARY_KEY_NAMING_CONVENTION = {"pk": "pk_%(table_name)s"}

_OLD_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "key": (sa.String, 255, False, True),
    "label": (sa.String, 160, False, False),
    "secret_value": (sa.Text, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
}
_TARGET_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "workspace_id": (sa.String, 36, False, True),
    "key": (sa.String, 255, False, True),
    "label": (sa.String, 160, False, False),
    "secret_value": (sa.Text, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
    "revision": (sa.Integer, None, False, False),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {
        _TABLE: set(_OLD_COLUMNS),
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
    }
    missing_tables = set(required) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot migrate stored credentials; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected_columns in required.items():
        missing_columns = expected_columns - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot migrate stored credentials; {table_name} is missing required columns."
            )


def _validate_no_inbound_foreign_keys(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    for table_name in sorted(_table_names(bind) - {_TABLE}):
        if any(
            foreign_key["referred_table"] == _TABLE
            for foreign_key in inspector.get_foreign_keys(table_name)
        ):
            raise RuntimeError(
                "Cannot migrate stored credentials; another table has an unexpected foreign key to the credential store."
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


def _timestamp_default(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    normalized = raw
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if normalized.casefold() != "current_timestamp":
        raise RuntimeError(
            "Cannot migrate stored credentials; timestamp defaults have an unexpected definition."
        )
    return raw


def _validate_timestamp_defaults(columns: Mapping[str, Mapping[str, Any]]) -> None:
    created_default = _timestamp_default(columns["created_at"].get("default"))
    updated_default = _timestamp_default(columns["updated_at"].get("default"))
    if (created_default is None) != (updated_default is None):
        raise RuntimeError(
            "Cannot migrate stored credentials; timestamp defaults are inconsistent."
        )


def _validate_column_shape(
    columns: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]],
    *,
    target: bool,
) -> None:
    if set(columns) != set(expected):
        raise RuntimeError(
            "Cannot migrate stored credentials; the credential table has an unexpected column shape."
        )
    for name, (type_class, length, nullable, primary_key) in expected.items():
        column = columns[name]
        default = column.get("default")
        if name in {"created_at", "updated_at"}:
            _timestamp_default(default)
        elif target and name == _REVISION_COLUMN:
            if default is None or str(default).strip() not in {"1", "(1)"}:
                raise RuntimeError(
                    "Cannot migrate stored credentials; revision must have SQL default 1."
                )
        elif default is not None:
            raise RuntimeError(
                f"Cannot migrate stored credentials; {_TABLE}.{name} has an unexpected SQL default."
            )
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or bool(column.get("primary_key", False)) != primary_key
        ):
            raise RuntimeError(
                f"Cannot migrate stored credentials; {_TABLE}.{name} has an unexpected definition."
            )
    _validate_timestamp_defaults(columns)


def _validate_table_constraints(bind: sa.Connection, *, target: bool) -> None:
    expected_primary_key = (_WORKSPACE_COLUMN, "key") if target else ("key",)
    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != expected_primary_key:
        raise RuntimeError(
            "Cannot migrate stored credentials; the credential table has an unexpected primary key."
        )
    if sa.inspect(bind).get_unique_constraints(_TABLE) or sa.inspect(bind).get_check_constraints(
        _TABLE
    ):
        raise RuntimeError(
            "Cannot migrate stored credentials; the credential table has unexpected constraints."
        )
    expected_foreign_keys = (
        {((_WORKSPACE_COLUMN,), "workspaces", ("id",), "RESTRICT", _WORKSPACE_FK)}
        if target
        else set()
    )
    if _foreign_key_signatures(bind) != expected_foreign_keys:
        raise RuntimeError(
            "Cannot migrate stored credentials; the credential table has an unexpected foreign-key shape."
        )


def _primary_key_name(bind: sa.Connection) -> str:
    name = sa.inspect(bind).get_pk_constraint(_TABLE).get("name")
    return str(name) if name else f"pk_{_TABLE}"


def _validate_schema(bind: sa.Connection, *, target: bool) -> None:
    columns = _columns(bind, _TABLE)
    expected = _TARGET_COLUMNS if target else _OLD_COLUMNS
    _validate_column_shape(columns, expected, target=target)
    _validate_table_constraints(bind, target=target)


def _index_definitions(bind: sa.Connection) -> dict[str, str]:
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"
        ),
        {"table_name": _TABLE},
    ).all()
    return {str(name): str(sql) for name, sql in rows}


def _drop_index_definitions(bind: sa.Connection, definitions: Mapping[str, str]) -> None:
    preparer = bind.dialect.identifier_preparer
    for name in definitions:
        bind.exec_driver_sql(f"DROP INDEX IF EXISTS {preparer.quote(name)}")


def _restore_index_definitions(bind: sa.Connection, definitions: Mapping[str, str]) -> None:
    preparer = bind.dialect.identifier_preparer
    for name, sql in definitions.items():
        bind.exec_driver_sql(f"DROP INDEX IF EXISTS {preparer.quote(name)}")
        bind.exec_driver_sql(sql)


def _stored_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Cannot migrate stored credentials; the seeded {label} is malformed.")
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
            "Cannot migrate stored credentials; the seeded owner is missing, ambiguous, inactive, or not an instance admin."
        )
    owner_id = _stored_id(owners[0]["id"], label="owner ID")
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
            "Cannot migrate stored credentials; the seeded personal workspace is missing, ambiguous, or inactive."
        )
    workspace_id = _stored_id(workspaces[0]["id"], label="personal workspace ID")
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
            "Cannot migrate stored credentials; the seeded owner workspace membership is missing or invalid."
        )
    return workspace_id


def _validate_empty_target(bind: sa.Connection) -> None:
    if bind.execute(sa.text("SELECT key FROM stored_credentials LIMIT 1")).first() is not None:
        raise RuntimeError(
            "Cannot migrate stored credentials; an early target table must be empty."
        )


def _validate_foreign_key_rows(bind: sa.Connection) -> None:
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check('stored_credentials')").first()
    if violations is not None:
        raise RuntimeError(
            "Cannot migrate stored credentials; the credential table has an invalid workspace reference."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)

    columns = _columns(bind, _TABLE)
    target_columns = {_WORKSPACE_COLUMN, _REVISION_COLUMN}
    present_target = target_columns & set(columns)
    workspace_id = _initial_personal_workspace_id(bind)
    if present_target == target_columns:
        _validate_schema(bind, target=True)
        _validate_empty_target(bind)
        _validate_foreign_key_rows(bind)
        return
    if present_target:
        raise RuntimeError(
            "Cannot migrate stored credentials; only part of the target columns exists."
        )

    _validate_schema(bind, target=False)
    old_indexes = _index_definitions(bind)
    _drop_index_definitions(bind, old_indexes)

    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.add_column(sa.Column(_WORKSPACE_COLUMN, sa.String(36), nullable=True))
        batch.add_column(
            sa.Column(
                _REVISION_COLUMN,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )

    bind.execute(
        sa.text(
            "UPDATE stored_credentials SET workspace_id = :workspace_id WHERE workspace_id IS NULL"
        ),
        {"workspace_id": workspace_id},
    )
    if bind.execute(
        sa.text("SELECT 1 FROM stored_credentials WHERE workspace_id IS NULL LIMIT 1")
    ).first():
        raise RuntimeError(
            "Cannot migrate stored credentials; a legacy credential could not be assigned to the seeded workspace."
        )

    primary_key_name = _primary_key_name(bind)
    with op.batch_alter_table(
        _TABLE,
        recreate="always",
        naming_convention=_PRIMARY_KEY_NAMING_CONVENTION,
    ) as batch:
        batch.alter_column(
            _WORKSPACE_COLUMN,
            existing_type=sa.String(36),
            existing_nullable=True,
            nullable=False,
        )
        batch.drop_constraint(primary_key_name, type_="primary")
        batch.create_primary_key(primary_key_name, [_WORKSPACE_COLUMN, "key"])
        batch.create_foreign_key(
            _WORKSPACE_FK,
            "workspaces",
            [_WORKSPACE_COLUMN],
            ["id"],
            ondelete="RESTRICT",
        )

    _restore_index_definitions(bind, old_indexes)
    _validate_schema(bind, target=True)
    _validate_foreign_key_rows(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)
    _validate_schema(bind, target=True)
    workspace_id = _initial_personal_workspace_id(bind)
    if bind.execute(
        sa.text("SELECT 1 FROM stored_credentials WHERE workspace_id != :workspace_id LIMIT 1"),
        {"workspace_id": workspace_id},
    ).first():
        raise RuntimeError(
            "Cannot downgrade stored credentials; non-initial workspace credentials must be migrated first."
        )
    _validate_foreign_key_rows(bind)

    old_indexes = _index_definitions(bind)
    _drop_index_definitions(bind, old_indexes)
    primary_key_name = _primary_key_name(bind)
    with op.batch_alter_table(
        _TABLE,
        recreate="always",
        naming_convention=_PRIMARY_KEY_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_constraint(primary_key_name, type_="primary")
        batch.create_primary_key(primary_key_name, ["key"])
        batch.drop_column(_WORKSPACE_COLUMN)
        batch.drop_column(_REVISION_COLUMN)

    _restore_index_definitions(bind, old_indexes)
    _validate_schema(bind, target=False)
