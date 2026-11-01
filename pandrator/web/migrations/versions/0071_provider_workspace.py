"""Add workspace ownership to provider catalog entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0071_provider_workspace"
down_revision = "0070_credential_namespaces"
branch_labels = None
depends_on = None

_TABLE = "providers"
_MODEL_TABLE = "provider_models"
_WORKSPACE_COLUMN = "workspace_id"
_WORKSPACE_FK = "fk_providers_workspace_id_workspaces"
_WORKSPACE_INDEX = "ix_providers_workspace_id"
_OLD_IDENTITY_CONSTRAINT = "uq_provider_identity"
_SCOPED_IDENTITY_CONSTRAINT = "uq_provider_workspace_identity"
_OLD_IDENTITY_COLUMNS = ("kind", "provider_key", "label")
_SCOPED_IDENTITY_COLUMNS = (
    _WORKSPACE_COLUMN,
    "kind",
    "provider_key",
    "label",
)
_UNIQUE_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}

_COLUMN_SHAPE: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "id": (sa.String, 36, False, True),
    "kind": (sa.String, 32, False, False),
    "provider_key": (sa.String, 80, False, False),
    "label": (sa.String, 160, False, False),
    "enabled": (sa.Boolean, None, False, False),
    "base_url": (sa.Text, None, True, False),
    "secret_ref": (sa.String, 255, True, False),
    "options_json": (sa.JSON, None, False, False),
    "revision": (sa.Integer, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
}
_TARGET_COLUMN_SHAPE = {
    **_COLUMN_SHAPE,
    _WORKSPACE_COLUMN: (sa.String, 36, True, False),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required_columns = {
        _TABLE: set(_COLUMN_SHAPE),
        _MODEL_TABLE: {"id", "provider_id", "model_id"},
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
    }
    missing_tables = set(required_columns) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot migrate provider ownership; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected_columns in required_columns.items():
        missing_columns = expected_columns - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot migrate provider ownership; {table_name} is missing required columns."
            )


def _validate_columns(bind: sa.Connection, *, target: bool) -> None:
    expected = _TARGET_COLUMN_SHAPE if target else _COLUMN_SHAPE
    columns = _columns(bind, _TABLE)
    if set(columns) != set(expected):
        raise RuntimeError(
            "Cannot migrate provider ownership; the provider table has an unexpected column shape."
        )
    for name, (type_class, length, nullable, primary_key) in expected.items():
        column = columns[name]
        if column.get("default") is not None:
            raise RuntimeError(
                f"Cannot migrate provider ownership; providers.{name} has an unexpected SQL default."
            )
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or bool(column.get("primary_key", False)) != primary_key
        ):
            raise RuntimeError(
                f"Cannot migrate provider ownership; providers.{name} has an unexpected definition."
            )


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]]:
    return {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete", "").upper(),
            foreign_key.get("name"),
        )
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
    }


def _validate_provider_foreign_keys(bind: sa.Connection, *, target: bool) -> None:
    expected = (
        {((_WORKSPACE_COLUMN,), "workspaces", ("id",), "RESTRICT", _WORKSPACE_FK)}
        if target
        else set()
    )
    if _foreign_key_signatures(bind, _TABLE) != expected:
        raise RuntimeError(
            "Cannot migrate provider ownership; the provider table has an unexpected foreign-key shape."
        )

    inbound: list[tuple[str, tuple[str, ...], str, tuple[str, ...], str]] = []
    inspector = sa.inspect(bind)
    for table_name in sorted(_table_names(bind) - {_TABLE}):
        for foreign_key in inspector.get_foreign_keys(table_name):
            if foreign_key["referred_table"] != _TABLE:
                continue
            inbound.append(
                (
                    table_name,
                    tuple(foreign_key["constrained_columns"]),
                    _TABLE,
                    tuple(foreign_key["referred_columns"]),
                    (foreign_key.get("options") or {}).get("ondelete", "").upper(),
                )
            )
    if inbound != [(_MODEL_TABLE, ("provider_id",), _TABLE, ("id",), "CASCADE")]:
        raise RuntimeError(
            "Cannot migrate provider ownership; the provider model parent relation is missing or an unrecognized table references providers."
        )


def _unique_constraints(bind: sa.Connection) -> list[Mapping[str, Any]]:
    return list(sa.inspect(bind).get_unique_constraints(_TABLE))


def _unique_indexes(bind: sa.Connection) -> list[Mapping[str, Any]]:
    return [index for index in sa.inspect(bind).get_indexes(_TABLE) if index["unique"]]


def _identity_constraint(bind: sa.Connection, *, target: bool) -> tuple[str, str | None]:
    constraints = _unique_constraints(bind)
    indexes = _unique_indexes(bind)
    if target:
        scoped = [
            constraint
            for constraint in constraints
            if tuple(constraint.get("column_names") or ()) == _SCOPED_IDENTITY_COLUMNS
        ]
        old = [
            constraint
            for constraint in constraints
            if tuple(constraint.get("column_names") or ()) == _OLD_IDENTITY_COLUMNS
        ]
        old_indexes = [
            index
            for index in indexes
            if tuple(index.get("column_names") or ()) == _OLD_IDENTITY_COLUMNS
        ]
        scoped_indexes = [
            index
            for index in indexes
            if tuple(index.get("column_names") or ()) == _SCOPED_IDENTITY_COLUMNS
        ]
        if (
            len(scoped) != 1
            or scoped[0].get("name") != _SCOPED_IDENTITY_CONSTRAINT
            or old
            or old_indexes
            or scoped_indexes
        ):
            raise RuntimeError(
                "Cannot migrate provider ownership; the scoped provider identity constraint is malformed."
            )
        return "constraint", _SCOPED_IDENTITY_CONSTRAINT

    matches: list[tuple[str, str | None]] = [
        ("constraint", constraint.get("name"))
        for constraint in constraints
        if tuple(constraint.get("column_names") or ()) == _OLD_IDENTITY_COLUMNS
    ]
    matches.extend(
        ("index", index.get("name"))
        for index in indexes
        if tuple(index.get("column_names") or ()) == _OLD_IDENTITY_COLUMNS
    )
    if len(matches) != 1:
        raise RuntimeError(
            "Cannot migrate provider ownership; the legacy provider identity uniqueness is missing or ambiguous."
        )
    return matches[0]


def _validate_workspace_index(bind: sa.Connection) -> None:
    indexes = sa.inspect(bind).get_indexes(_TABLE)
    named = [index for index in indexes if index.get("name") == _WORKSPACE_INDEX]
    if (
        len(named) != 1
        or named[0]["unique"]
        or tuple(named[0].get("column_names") or ()) != (_WORKSPACE_COLUMN,)
    ):
        raise RuntimeError(
            "Cannot migrate provider ownership; the provider workspace index is malformed."
        )


def _validate_schema(bind: sa.Connection, *, target: bool) -> None:
    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != ("id",):
        raise RuntimeError(
            "Cannot migrate provider ownership; the provider table has an unexpected primary key."
        )
    _validate_columns(bind, target=target)
    _validate_provider_foreign_keys(bind, target=target)
    _identity_constraint(bind, target=target)
    if target:
        _validate_workspace_index(bind)
    elif any(
        index.get("name") == _WORKSPACE_INDEX for index in sa.inspect(bind).get_indexes(_TABLE)
    ):
        raise RuntimeError(
            "Cannot migrate provider ownership; the legacy provider table uses a reserved workspace index name."
        )


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
        raise RuntimeError(f"Cannot migrate provider ownership; the seeded {label} is malformed.")
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
            "Cannot migrate provider ownership; the seeded owner is missing, ambiguous, inactive, or not an instance admin."
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
            "Cannot migrate provider ownership; the seeded personal workspace is missing, ambiguous, or inactive."
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
            "Cannot migrate provider ownership; the seeded owner workspace membership is missing or invalid."
        )
    return workspace_id


def _has_duplicate_identity(bind: sa.Connection) -> bool:
    return (
        bind.execute(
            sa.text(
                "SELECT 1 FROM providers GROUP BY kind, provider_key, label "
                "HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        is not None
    )


def _validate_model_graph(bind: sa.Connection) -> None:
    if bind.execute(
        sa.text(
            "SELECT 1 FROM provider_models AS models "
            "LEFT JOIN providers AS providers ON providers.id = models.provider_id "
            "WHERE providers.id IS NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "Cannot migrate provider ownership; a provider model has no provider parent."
        )
    if bind.exec_driver_sql("PRAGMA foreign_key_check('provider_models')").first():
        raise RuntimeError(
            "Cannot migrate provider ownership; the provider model parent graph is invalid."
        )


def _validate_legacy_identity_data(bind: sa.Connection) -> None:
    if _has_duplicate_identity(bind):
        raise RuntimeError(
            "Cannot migrate provider ownership; legacy provider identities contain duplicates."
        )


def _validate_null_target_owners(bind: sa.Connection) -> None:
    if bind.execute(
        sa.text("SELECT 1 FROM providers WHERE workspace_id IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError(
            "Cannot migrate provider ownership; early provider workspace values must all be NULL."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_model_graph(bind)
    workspace_id = _initial_personal_workspace_id(bind)

    if _WORKSPACE_COLUMN in _columns(bind, _TABLE):
        _validate_schema(bind, target=True)
        _validate_null_target_owners(bind)
        _validate_legacy_identity_data(bind)
        bind.execute(
            sa.text("UPDATE providers SET workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        return

    _validate_schema(bind, target=False)
    _validate_legacy_identity_data(bind)
    identity_kind, identity_name = _identity_constraint(bind, target=False)
    old_indexes = _index_definitions(bind)
    indexes_to_restore = dict(old_indexes)
    if identity_kind == "index" and identity_name is not None:
        indexes_to_restore.pop(identity_name, None)
    _drop_index_definitions(bind, old_indexes)

    with op.batch_alter_table(
        _TABLE,
        recreate="always",
        naming_convention=_UNIQUE_NAMING_CONVENTION,
    ) as batch:
        batch.add_column(sa.Column(_WORKSPACE_COLUMN, sa.String(36), nullable=True))
        if identity_kind == "constraint":
            batch.drop_constraint(
                identity_name or "uq_providers_kind",
                type_="unique",
            )
        batch.create_unique_constraint(
            _SCOPED_IDENTITY_CONSTRAINT,
            list(_SCOPED_IDENTITY_COLUMNS),
        )
        batch.create_foreign_key(
            _WORKSPACE_FK,
            "workspaces",
            [_WORKSPACE_COLUMN],
            ["id"],
            ondelete="RESTRICT",
        )

    bind.execute(
        sa.text("UPDATE providers SET workspace_id = :workspace_id"),
        {"workspace_id": workspace_id},
    )
    op.create_index(_WORKSPACE_INDEX, _TABLE, [_WORKSPACE_COLUMN])
    _restore_index_definitions(bind, indexes_to_restore)
    _validate_schema(bind, target=True)
    if _has_duplicate_identity(bind):
        raise RuntimeError(
            "Cannot migrate provider ownership; scoped provider identities contain duplicates."
        )


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_model_graph(bind)
    _validate_schema(bind, target=True)
    workspace_id = _initial_personal_workspace_id(bind)
    if bind.execute(
        sa.text(
            "SELECT 1 FROM providers "
            "WHERE workspace_id IS NULL OR workspace_id != :workspace_id LIMIT 1"
        ),
        {"workspace_id": workspace_id},
    ).first():
        raise RuntimeError(
            "Cannot downgrade provider ownership; NULL or non-initial workspace providers must be migrated first."
        )
    if _has_duplicate_identity(bind):
        raise RuntimeError(
            "Cannot downgrade provider ownership; duplicate provider identities cannot be merged."
        )

    old_indexes = _index_definitions(bind)
    index_columns = {
        str(index["name"]): set(index.get("column_names") or ())
        for index in sa.inspect(bind).get_indexes(_TABLE)
        if index.get("name") is not None
    }
    indexes_to_restore = {
        name: sql
        for name, sql in old_indexes.items()
        if _WORKSPACE_COLUMN not in index_columns.get(name, set())
    }
    _drop_index_definitions(bind, old_indexes)

    with op.batch_alter_table(
        _TABLE,
        recreate="always",
        naming_convention=_UNIQUE_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(_SCOPED_IDENTITY_CONSTRAINT, type_="unique")
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_column(_WORKSPACE_COLUMN)
        batch.create_unique_constraint(
            _OLD_IDENTITY_CONSTRAINT,
            list(_OLD_IDENTITY_COLUMNS),
        )

    _restore_index_definitions(bind, indexes_to_restore)
    _validate_schema(bind, target=False)
