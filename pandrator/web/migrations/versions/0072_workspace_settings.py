"""Move non-host settings into the seeded personal workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0072_workspace_settings"
down_revision = "0071_provider_workspace"
branch_labels = None
depends_on = None

_CURRENT = "app_settings"
_HISTORY = "app_settings_history"
_WORKSPACE_CURRENT = "workspace_settings"
_WORKSPACE_HISTORY = "workspace_settings_history"
_HISTORY_INDEX = "ix_workspace_settings_history_workspace_key"
_CURRENT_FK = "fk_workspace_settings_workspace_id_workspaces"
_HISTORY_FK = "fk_workspace_settings_history_workspace_id_workspaces"
_HOST_KEYS = ("system.identity", "session.trash_policy", "web.preferences")
_HOST_KEYS_SQL = "'system.identity', 'session.trash_policy', 'web.preferences'"
_CURRENT_COLUMN_SHAPE: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "key": (sa.String, 120, False, True),
    "value_json": (sa.JSON, None, False, False),
    "revision": (sa.Integer, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
}
_HISTORY_COLUMN_SHAPE: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "id": (sa.Integer, None, False, True),
    "key": (sa.String, 120, False, False),
    "value_json": (sa.JSON, None, False, False),
    "revision": (sa.Integer, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
}
_WORKSPACE_CURRENT_COLUMN_SHAPE = {
    "workspace_id": (sa.String, 36, False, True),
    **_CURRENT_COLUMN_SHAPE,
}
_WORKSPACE_HISTORY_COLUMN_SHAPE = {
    **_HISTORY_COLUMN_SHAPE,
    "workspace_id": (sa.String, 36, False, False),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {
        _CURRENT: set(_CURRENT_COLUMN_SHAPE),
        _HISTORY: set(_HISTORY_COLUMN_SHAPE),
        "users": {"id", "legacy_subject"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
    }
    missing_tables = set(required) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot migrate workspace settings; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected_columns in required.items():
        missing_columns = expected_columns - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot migrate workspace settings; {table_name} is missing required columns."
            )


def _stored_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Cannot migrate workspace settings; the seeded {label} is malformed.")
    return value


def _primary_key(bind: sa.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(sa.inspect(bind).get_pk_constraint(table_name).get("constrained_columns") or ())


def _validate_sqlite_unique_indexes(bind: sa.Connection, table_name: str) -> None:
    if bind.dialect.name != "sqlite":
        return
    quoted_table = bind.dialect.identifier_preparer.quote(table_name)
    indexes = bind.exec_driver_sql(f"PRAGMA index_list({quoted_table})").mappings().all()
    if any(
        bool(index["unique"]) and str(index.get("origin", "")).casefold() != "pk"
        for index in indexes
    ):
        raise RuntimeError(
            f"Cannot migrate workspace settings; {table_name} has an unexpected unique index."
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


def _validate_columns(
    bind: sa.Connection,
    table_name: str,
    expected: Mapping[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]],
) -> None:
    columns = _columns(bind, table_name)
    if set(columns) != set(expected):
        raise RuntimeError(
            f"Cannot migrate workspace settings; {table_name} has an unexpected column shape."
        )
    for name, (type_class, length, nullable, primary_key) in expected.items():
        column = columns[name]
        if column.get("default") is not None:
            raise RuntimeError(
                f"Cannot migrate workspace settings; {table_name}.{name} has an unexpected SQL default."
            )
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or bool(column.get("primary_key", False)) != primary_key
        ):
            raise RuntimeError(
                f"Cannot migrate workspace settings; {table_name}.{name} has an unexpected definition."
            )


def _validate_basic_constraints(
    bind: sa.Connection,
    table_name: str,
    *,
    primary_key: tuple[str, ...],
    foreign_keys: set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]],
) -> None:
    # SQLite's reflection API can skip expression indexes. Inspect its index
    # catalog first so functional uniqueness cannot disappear during adoption.
    _validate_sqlite_unique_indexes(bind, table_name)
    inspector = sa.inspect(bind)
    if _primary_key(bind, table_name) != primary_key:
        raise RuntimeError(
            f"Cannot migrate workspace settings; {table_name} has an unexpected primary key."
        )
    if inspector.get_unique_constraints(table_name) or inspector.get_check_constraints(table_name):
        raise RuntimeError(
            f"Cannot migrate workspace settings; {table_name} has unexpected functional constraints."
        )
    if _foreign_key_signatures(bind, table_name) != foreign_keys:
        raise RuntimeError(
            f"Cannot migrate workspace settings; {table_name} has an unexpected foreign-key shape."
        )
    if any(index.get("unique") for index in inspector.get_indexes(table_name)):
        raise RuntimeError(
            f"Cannot migrate workspace settings; {table_name} has an unexpected unique index."
        )


def _validate_legacy_schema(bind: sa.Connection) -> None:
    _validate_columns(bind, _CURRENT, _CURRENT_COLUMN_SHAPE)
    _validate_columns(bind, _HISTORY, _HISTORY_COLUMN_SHAPE)
    _validate_basic_constraints(bind, _CURRENT, primary_key=("key",), foreign_keys=set())
    _validate_basic_constraints(bind, _HISTORY, primary_key=("id",), foreign_keys=set())

    history_indexes = sa.inspect(bind).get_indexes(_HISTORY)
    if not any(
        not index.get("unique") and tuple(index.get("column_names") or ()) == ("key",)
        for index in history_indexes
    ):
        raise RuntimeError(
            "Cannot migrate workspace settings; the legacy history key index is missing."
        )

    for table_name in sorted(_table_names(bind)):
        if table_name in {_CURRENT, _HISTORY}:
            continue
        if any(
            foreign_key["referred_table"] in {_CURRENT, _HISTORY}
            for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        ):
            raise RuntimeError(
                "Cannot migrate workspace settings; another table references a legacy settings table."
            )
    for table_name in (_CURRENT, _HISTORY):
        if bind.execute(
            sa.text(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table_name LIMIT 1"
            ),
            {"table_name": table_name},
        ).first():
            raise RuntimeError(
                "Cannot migrate workspace settings; legacy settings triggers are not supported."
            )


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
    owners = (
        bind.execute(sa.text("SELECT id FROM users WHERE legacy_subject = 'owner' ORDER BY id"))
        .scalars()
        .all()
    )
    if len(owners) != 1:
        raise RuntimeError(
            "Cannot migrate workspace settings; the seeded owner is missing or ambiguous."
        )
    owner_id = _stored_text(owners[0], label="owner ID")
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
        or workspaces[0]["state"] not in {"active", "archived"}
    ):
        raise RuntimeError(
            "Cannot migrate workspace settings; the seeded personal workspace is missing, ambiguous, or invalid."
        )
    workspace_id = _stored_text(workspaces[0]["id"], label="personal workspace ID")
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
            "Cannot migrate workspace settings; the seeded personal workspace owner membership is invalid."
        )
    return workspace_id


def _validate_workspace_owner_graph(bind: sa.Connection, workspace_ids: Sequence[str]) -> None:
    for workspace_id in sorted(set(workspace_ids)):
        row = (
            bind.execute(
                sa.text(
                    "SELECT id, kind, personal_owner_user_id, state FROM workspaces WHERE id = :id"
                ),
                {"id": workspace_id},
            )
            .mappings()
            .first()
        )
        if row is None or row["state"] not in {"active", "archived"}:
            raise RuntimeError(
                "Cannot migrate workspace settings; a target setting has an invalid workspace reference."
            )
        if row["kind"] == "personal":
            owner_id = row["personal_owner_user_id"]
            if not isinstance(owner_id, str) or not owner_id.strip():
                raise RuntimeError(
                    "Cannot migrate workspace settings; a target personal workspace has no valid owner."
                )
            owner = bind.execute(
                sa.text("SELECT id FROM users WHERE id = :owner_id"),
                {"owner_id": owner_id},
            ).first()
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
            if owner is None or membership != ["owner"]:
                raise RuntimeError(
                    "Cannot migrate workspace settings; a target personal workspace owner graph is invalid."
                )
        elif row["kind"] != "team" or row["personal_owner_user_id"] is not None:
            raise RuntimeError(
                "Cannot migrate workspace settings; a target workspace has an invalid kind or owner."
            )


def _target_workspace_ids(bind: sa.Connection) -> list[str]:
    ids: list[str] = []
    for table_name in (_WORKSPACE_CURRENT, _WORKSPACE_HISTORY):
        ids.extend(
            str(row[0])
            for row in bind.execute(sa.text(f"SELECT DISTINCT workspace_id FROM {table_name}"))
        )
    return ids


def _validate_target_tables(bind: sa.Connection) -> None:
    _validate_columns(bind, _WORKSPACE_CURRENT, _WORKSPACE_CURRENT_COLUMN_SHAPE)
    _validate_columns(bind, _WORKSPACE_HISTORY, _WORKSPACE_HISTORY_COLUMN_SHAPE)
    _validate_basic_constraints(
        bind,
        _WORKSPACE_CURRENT,
        primary_key=("workspace_id", "key"),
        foreign_keys={(("workspace_id",), "workspaces", ("id",), "RESTRICT", _CURRENT_FK)},
    )
    _validate_basic_constraints(
        bind,
        _WORKSPACE_HISTORY,
        primary_key=("id",),
        foreign_keys={(("workspace_id",), "workspaces", ("id",), "RESTRICT", _HISTORY_FK)},
    )
    current_indexes = sa.inspect(bind).get_indexes(_WORKSPACE_CURRENT)
    if current_indexes:
        raise RuntimeError(
            "Cannot migrate workspace settings; the workspace settings table has unexpected indexes."
        )
    indexes = sa.inspect(bind).get_indexes(_WORKSPACE_HISTORY)
    matching = [index for index in indexes if index.get("name") == _HISTORY_INDEX]
    if (
        len(indexes) != 1
        or len(matching) != 1
        or matching[0].get("unique")
        or tuple(matching[0].get("column_names") or ()) != ("workspace_id", "key")
    ):
        raise RuntimeError(
            "Cannot migrate workspace settings; the workspace history index is malformed."
        )
    for table_name in (_WORKSPACE_CURRENT, _WORKSPACE_HISTORY):
        if bind.exec_driver_sql(f"PRAGMA foreign_key_check('{table_name}')").first():
            raise RuntimeError(
                "Cannot migrate workspace settings; a target workspace foreign key is invalid."
            )
        if bind.execute(
            sa.text(
                f"SELECT 1 FROM {table_name} WHERE key COLLATE BINARY IN ({_HOST_KEYS_SQL}) LIMIT 1"
            )
        ).first():
            raise RuntimeError(
                "Cannot migrate workspace settings; host-only keys are present in workspace settings."
            )
        if bind.execute(
            sa.text(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = :table_name LIMIT 1"
            ),
            {"table_name": table_name},
        ).first():
            raise RuntimeError(
                "Cannot migrate workspace settings; workspace settings triggers are not supported."
            )
    _validate_workspace_owner_graph(bind, _target_workspace_ids(bind))


def _target_state(bind: sa.Connection) -> str:
    tables = _table_names(bind)
    present = {table for table in (_WORKSPACE_CURRENT, _WORKSPACE_HISTORY) if table in tables}
    if present and present != {_WORKSPACE_CURRENT, _WORKSPACE_HISTORY}:
        raise RuntimeError(
            "Cannot migrate workspace settings; only one workspace settings table exists."
        )
    if present:
        _validate_target_tables(bind)
        return "present"

    occupied = bind.execute(
        sa.text(
            "SELECT type, name FROM sqlite_master WHERE name IN (:current, :history, :index_name)"
        ),
        {
            "current": _WORKSPACE_CURRENT,
            "history": _WORKSPACE_HISTORY,
            "index_name": _HISTORY_INDEX,
        },
    ).all()
    if occupied:
        raise RuntimeError(
            "Cannot migrate workspace settings; a reserved workspace settings object is malformed."
        )
    return "absent"


def _validate_no_inbound_target_foreign_keys(bind: sa.Connection) -> None:
    for table_name in sorted(_table_names(bind) - {_WORKSPACE_CURRENT, _WORKSPACE_HISTORY}):
        if any(
            foreign_key["referred_table"] in {_WORKSPACE_CURRENT, _WORKSPACE_HISTORY}
            for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        ):
            raise RuntimeError(
                "Cannot migrate workspace settings; another table references a workspace settings table."
            )


def _validate_migrating_collisions(bind: sa.Connection, workspace_id: str) -> None:
    current_collision = bind.execute(
        sa.text(
            "SELECT 1 FROM app_settings AS old "
            "JOIN workspace_settings AS current "
            "ON current.workspace_id = :workspace_id "
            "AND current.key COLLATE BINARY = old.key COLLATE BINARY "
            f"WHERE old.key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL}) LIMIT 1"
        ),
        {"workspace_id": workspace_id},
    ).first()
    if current_collision:
        raise RuntimeError(
            "Cannot migrate workspace settings; a private current key already exists in the seeded workspace."
        )
    history_collision = bind.execute(
        sa.text(
            "SELECT 1 FROM app_settings_history AS old "
            "JOIN workspace_settings_history AS history ON history.id = old.id "
            f"WHERE old.key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL}) LIMIT 1"
        )
    ).first()
    if history_collision:
        raise RuntimeError(
            "Cannot migrate workspace settings; a private history ID already exists in workspace history."
        )


def _private_count(bind: sa.Connection, table_name: str) -> int:
    return int(
        bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table_name} "
                f"WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL})"
            )
        ).scalar_one()
    )


def _host_rows(bind: sa.Connection, table_name: str) -> tuple[tuple[Any, ...], ...]:
    columns = (
        "key, value_json, revision, updated_at"
        if table_name == _CURRENT
        else ("id, key, value_json, revision, created_at")
    )
    return tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                f"SELECT {columns} FROM {table_name} "
                f"WHERE key COLLATE BINARY IN ({_HOST_KEYS_SQL}) "
                f"ORDER BY {columns.split(', ', 1)[0]}"
            )
        )
    )


def _target_current_rows(bind: sa.Connection) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "SELECT workspace_id, key, value_json, revision, updated_at "
                "FROM workspace_settings ORDER BY workspace_id COLLATE BINARY, key COLLATE BINARY"
            )
        )
    )


def _target_history_rows(bind: sa.Connection) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "SELECT id, workspace_id, key, value_json, revision, created_at "
                "FROM workspace_settings_history ORDER BY id"
            )
        )
    )


def _validate_postconditions(
    bind: sa.Connection,
    *,
    expected_current: tuple[tuple[Any, ...], ...],
    expected_history: tuple[tuple[Any, ...], ...],
    host_current: tuple[tuple[Any, ...], ...],
    host_history: tuple[tuple[Any, ...], ...],
) -> None:
    if _private_count(bind, _CURRENT) or _private_count(bind, _HISTORY):
        raise RuntimeError(
            "Cannot migrate workspace settings; private rows remain in the host settings tables."
        )
    if (
        _target_current_rows(bind) != expected_current
        or _target_history_rows(bind) != expected_history
    ):
        raise RuntimeError(
            "Cannot migrate workspace settings; workspace rows changed during transfer."
        )
    if _host_rows(bind, _CURRENT) != host_current or _host_rows(bind, _HISTORY) != host_history:
        raise RuntimeError(
            "Cannot migrate workspace settings; host-owned settings changed during transfer."
        )
    _validate_target_tables(bind)


def _create_target_tables() -> None:
    op.create_table(
        _WORKSPACE_CURRENT,
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=_CURRENT_FK,
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "key"),
    )
    op.create_table(
        _WORKSPACE_HISTORY,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=_HISTORY_FK,
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        _HISTORY_INDEX,
        _WORKSPACE_HISTORY,
        ["workspace_id", "key"],
        unique=False,
    )


def _reject_inbound_to_target_tables(bind: sa.Connection) -> None:
    _validate_no_inbound_target_foreign_keys(bind)


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_legacy_schema(bind)
    workspace_id = _initial_personal_workspace_id(bind)
    target_state = _target_state(bind)
    _validate_no_inbound_target_foreign_keys(bind)
    if target_state == "present":
        _validate_migrating_collisions(bind, workspace_id)

    source_current = tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "SELECT key, value_json, revision, updated_at FROM app_settings "
                f"WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL}) "
                "ORDER BY key COLLATE BINARY"
            )
        )
    )
    source_history = tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "SELECT id, key, value_json, revision, created_at FROM app_settings_history "
                f"WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL}) ORDER BY id"
            )
        )
    )
    target_current_before = _target_current_rows(bind) if target_state == "present" else ()
    target_history_before = _target_history_rows(bind) if target_state == "present" else ()
    expected_current = tuple(
        sorted(
            (*target_current_before, *((workspace_id, *row) for row in source_current)),
            key=lambda row: (str(row[0]), str(row[1])),
        )
    )
    expected_history = tuple(
        sorted(
            (*target_history_before, *((row[0], workspace_id, *row[1:]) for row in source_history)),
            key=lambda row: int(row[0]),
        )
    )
    host_current = _host_rows(bind, _CURRENT)
    host_history = _host_rows(bind, _HISTORY)

    if target_state == "absent":
        _create_target_tables()

    bind.execute(
        sa.text(
            "INSERT INTO workspace_settings "
            "(workspace_id, key, value_json, revision, updated_at) "
            "SELECT :workspace_id, key, value_json, revision, updated_at "
            "FROM app_settings "
            f"WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL})"
        ),
        {"workspace_id": workspace_id},
    )
    bind.execute(
        sa.text(
            "INSERT INTO workspace_settings_history "
            "(id, workspace_id, key, value_json, revision, created_at) "
            "SELECT id, :workspace_id, key, value_json, revision, created_at "
            "FROM app_settings_history "
            f"WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL})"
        ),
        {"workspace_id": workspace_id},
    )
    bind.execute(
        sa.text(f"DELETE FROM app_settings WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL})")
    )
    bind.execute(
        sa.text(
            f"DELETE FROM app_settings_history WHERE key COLLATE BINARY NOT IN ({_HOST_KEYS_SQL})"
        )
    )
    _validate_postconditions(
        bind,
        expected_current=expected_current,
        expected_history=expected_history,
        host_current=host_current,
        host_history=host_history,
    )


def _validate_downgrade_rows(bind: sa.Connection, workspace_id: str) -> None:
    if (
        bind.execute(
            sa.text("SELECT 1 FROM workspace_settings WHERE workspace_id != :workspace_id LIMIT 1"),
            {"workspace_id": workspace_id},
        ).first()
        or bind.execute(
            sa.text(
                "SELECT 1 FROM workspace_settings_history "
                "WHERE workspace_id != :workspace_id LIMIT 1"
            ),
            {"workspace_id": workspace_id},
        ).first()
    ):
        raise RuntimeError(
            "Cannot downgrade workspace settings; non-initial workspace history must be retained."
        )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM app_settings AS old "
            "JOIN workspace_settings AS current "
            "ON current.key COLLATE BINARY = old.key COLLATE BINARY LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "Cannot downgrade workspace settings; a current key collision would require a merge."
        )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM app_settings_history AS old "
            "JOIN workspace_settings_history AS history ON history.id = old.id LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "Cannot downgrade workspace settings; a history ID collision would require renumbering."
        )


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_legacy_schema(bind)
    if not {_WORKSPACE_CURRENT, _WORKSPACE_HISTORY}.issubset(_table_names(bind)):
        raise RuntimeError(
            "Cannot downgrade workspace settings; both workspace settings tables are required."
        )
    _validate_target_tables(bind)
    _reject_inbound_to_target_tables(bind)
    workspace_id = _initial_personal_workspace_id(bind)
    _validate_downgrade_rows(bind, workspace_id)
    host_current = _host_rows(bind, _CURRENT)
    host_history = _host_rows(bind, _HISTORY)

    bind.execute(
        sa.text(
            "INSERT INTO app_settings (key, value_json, revision, updated_at) "
            "SELECT key, value_json, revision, updated_at FROM workspace_settings "
            "WHERE workspace_id = :workspace_id"
        ),
        {"workspace_id": workspace_id},
    )
    bind.execute(
        sa.text(
            "INSERT INTO app_settings_history (id, key, value_json, revision, created_at) "
            "SELECT id, key, value_json, revision, created_at "
            "FROM workspace_settings_history WHERE workspace_id = :workspace_id"
        ),
        {"workspace_id": workspace_id},
    )
    op.drop_index(_HISTORY_INDEX, table_name=_WORKSPACE_HISTORY)
    op.drop_table(_WORKSPACE_HISTORY)
    op.drop_table(_WORKSPACE_CURRENT)
    if _host_rows(bind, _CURRENT) != host_current or _host_rows(bind, _HISTORY) != host_history:
        raise RuntimeError(
            "Cannot downgrade workspace settings; host-owned settings changed during restoration."
        )
    if _table_names(bind) & {_WORKSPACE_CURRENT, _WORKSPACE_HISTORY}:
        raise RuntimeError(
            "Cannot downgrade workspace settings; workspace settings tables remain after restoration."
        )
