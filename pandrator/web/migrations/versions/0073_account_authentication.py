"""Add account credentials and revocable browser authentication records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0073_account_authentication"
down_revision = "0072_workspace_settings"
branch_labels = None
depends_on = None

_USER_CREDENTIALS = "user_credentials"
_BROWSER_SESSIONS = "browser_auth_sessions"
_PASSWORD_RESETS = "password_reset_grants"
_AUTH_TABLES = (_USER_CREDENTIALS, _BROWSER_SESSIONS, _PASSWORD_RESETS)
_OWNER_ACCOUNT = "owner_account"
_API_TOKENS = "api_tokens"
_AUTOMATION_CLIENTS = "automation_clients"
_INITIAL_LOGIN = "owner"

_AUTH_TARGET_INDEXES = {
    "ix_browser_auth_sessions_user_id": (_BROWSER_SESSIONS, ("user_id",)),
    "ix_password_reset_grants_user_id": (_PASSWORD_RESETS, ("user_id",)),
}
_LINKAGE = {
    _API_TOKENS: (
        "fk_api_tokens_user_id_users",
        "ck_api_tokens_user_auth_pair",
        "ix_api_tokens_user_id",
    ),
    _AUTOMATION_CLIENTS: (
        "fk_automation_clients_user_id_users",
        "ck_automation_clients_user_auth_pair",
        "ix_automation_clients_user_id",
    ),
}

_USER_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "id": (sa.String, 36, False, True),
    "legacy_subject": (sa.String, 255, True, False),
    "display_name": (sa.String, 255, False, False),
    "is_active": (sa.Boolean, None, False, False),
    "is_instance_admin": (sa.Boolean, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
}
_OWNER_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "singleton_id": (sa.Integer, None, False, True),
    "password_hash": (sa.Text, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "updated_at": (sa.DateTime, None, False, False),
}
_API_TOKEN_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "id": (sa.String, 36, False, True),
    "label": (sa.String, 160, False, False),
    "token_hash": (sa.String, 128, False, False),
    "token_prefix": (sa.String, 16, False, False),
    "subject": (sa.String, 200, True, False),
    "scopes_json": (sa.JSON, None, False, False),
    "expires_at": (sa.DateTime, None, True, False),
    "principal_kind": (sa.String, 40, False, False),
    "created_by": (sa.String, 200, True, False),
    "client_id": (sa.String, 36, True, False),
    "target_instance_id": (sa.String, 36, True, False),
    "canonical_origin": (sa.Text, None, True, False),
    "created_at": (sa.DateTime, None, False, False),
    "last_used_at": (sa.DateTime, None, True, False),
    "revoked_at": (sa.DateTime, None, True, False),
}
_AUTOMATION_CLIENT_COLUMNS: dict[
    str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]
] = {
    "id": (sa.String, 36, False, True),
    "name": (sa.String, 160, False, False),
    "subject": (sa.String, 200, False, False),
    "redirect_uris_json": (sa.JSON, None, False, False),
    "allowed_scopes_json": (sa.JSON, None, False, False),
    "target_instance_id": (sa.String, 36, False, False),
    "canonical_origin": (sa.Text, None, False, False),
    "created_by": (sa.String, 200, False, False),
    "created_at": (sa.DateTime, None, False, False),
    "last_used_at": (sa.DateTime, None, True, False),
    "revoked_at": (sa.DateTime, None, True, False),
}
_AUTH_COLUMN_SHAPES: dict[
    str,
    dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]],
] = {
    _USER_CREDENTIALS: {
        "user_id": (sa.String, 36, False, True),
        "login_name": (sa.String, 64, False, False),
        "password_hash": (sa.Text, None, True, False),
        "auth_revision": (sa.Integer, None, False, False),
        "created_at": (sa.DateTime, None, False, False),
        "updated_at": (sa.DateTime, None, False, False),
    },
    _BROWSER_SESSIONS: {
        "id": (sa.String, 36, False, True),
        "user_id": (sa.String, 36, False, False),
        "token_hash": (sa.String, 64, False, False),
        "auth_revision": (sa.Integer, None, False, False),
        "subject": (sa.String, 200, False, False),
        "principal_kind": (sa.String, 40, False, False),
        "scopes_json": (sa.JSON, None, False, False),
        "client_id": (sa.String, 200, True, False),
        "created_at": (sa.DateTime, None, False, False),
        "last_seen_at": (sa.DateTime, None, False, False),
        "idle_expires_at": (sa.DateTime, None, False, False),
        "expires_at": (sa.DateTime, None, False, False),
        "revoked_at": (sa.DateTime, None, True, False),
    },
    _PASSWORD_RESETS: {
        "id": (sa.String, 36, False, True),
        "user_id": (sa.String, 36, False, False),
        "created_by_user_id": (sa.String, 36, False, False),
        "token_hash": (sa.String, 64, False, False),
        "auth_revision": (sa.Integer, None, False, False),
        "created_at": (sa.DateTime, None, False, False),
        "expires_at": (sa.DateTime, None, False, False),
        "consumed_at": (sa.DateTime, None, True, False),
        "revoked_at": (sa.DateTime, None, True, False),
    },
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _validate_columns(
    bind: sa.Connection,
    table_name: str,
    expected: Mapping[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]],
    *,
    allow_server_defaults: bool = False,
) -> None:
    columns = _columns(bind, table_name)
    if set(columns) != set(expected):
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has an unexpected column shape."
        )
    for name, (type_class, length, nullable, primary_key) in expected.items():
        column = columns[name]
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or bool(column.get("nullable")) != nullable
            or bool(column.get("primary_key", False)) != primary_key
            or (not allow_server_defaults and column.get("default") is not None)
        ):
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name}.{name} has an unexpected definition."
            )


def _primary_key(bind: sa.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(sa.inspect(bind).get_pk_constraint(table_name).get("constrained_columns") or ())


def _normalize_sql(value: object) -> str:
    return re.sub(r"[\s\"`\[\]()]+", "", str(value or "")).casefold()


def _check_signatures(bind: sa.Connection, table_name: str) -> set[tuple[str | None, str]]:
    return {
        (check.get("name"), _normalize_sql(check.get("sqltext")))
        for check in sa.inspect(bind).get_check_constraints(table_name)
    }


def _unique_signatures(
    bind: sa.Connection, table_name: str
) -> set[tuple[str | None, tuple[str, ...]]]:
    return {
        (constraint.get("name"), tuple(constraint.get("column_names") or ()))
        for constraint in sa.inspect(bind).get_unique_constraints(table_name)
    }


def _sqlite_unique_constraint_columns(bind: sa.Connection, table_name: str) -> set[tuple[str, ...]]:
    if bind.dialect.name != "sqlite":
        return {columns for _, columns in _unique_signatures(bind, table_name)}
    quote = bind.dialect.identifier_preparer.quote
    indexes = bind.exec_driver_sql(f"PRAGMA index_list({quote(table_name)})").mappings().all()
    result: set[tuple[str, ...]] = set()
    for index in indexes:
        if not bool(index.get("unique")) or str(index.get("origin", "")) != "u":
            continue
        index_name = str(index["name"])
        columns = bind.exec_driver_sql(f"PRAGMA index_info({quote(index_name)})").mappings().all()
        if any(column.get("name") is None for column in columns):
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} has an expression unique constraint."
            )
        result.add(tuple(str(column["name"]) for column in columns))
    return result


def _copy_table_with_unique_constraints(bind: sa.Connection, table_name: str) -> sa.Table:
    copy_table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    copied_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in copy_table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    reflected_names = {
        columns: name for name, columns in _unique_signatures(bind, table_name) if name is not None
    }
    for columns in sorted(_sqlite_unique_constraint_columns(bind, table_name)):
        if columns not in copied_unique_columns:
            copy_table.append_constraint(
                sa.UniqueConstraint(*columns, name=reflected_names.get(columns))
            )
            copied_unique_columns.add(columns)
    copy_table.indexes.clear()
    return copy_table


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]]:
    return {
        (
            tuple(foreign_key.get("constrained_columns") or ()),
            str(foreign_key.get("referred_table")),
            tuple(foreign_key.get("referred_columns") or ()),
            str((foreign_key.get("options") or {}).get("ondelete") or "").upper(),
            foreign_key.get("name"),
        )
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
    }


def _has_named_user_foreign_key(
    bind: sa.Connection,
    table_name: str,
    constraint_name: str,
) -> bool:
    if bind.dialect.name != "sqlite":
        return False
    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table_name},
    ).scalar_one_or_none()
    if not isinstance(table_sql, str):
        return False
    identifier = r'["`\[]?' + re.escape(constraint_name) + r'["`\]]?'
    user_column = r'["`\[]?user_id["`\]]?'
    target = r'["`\[]?users["`\]]?\s*\(\s*["`\[]?id["`\]]?\s*\)'
    pattern = (
        r"\bCONSTRAINT\s+"
        + identifier
        + r"\s+(?:FOREIGN\s+KEY\s*\(\s*"
        + user_column
        + r"\s*\)\s+)?REFERENCES\s+"
        + target
        + r"\s+ON\s+DELETE\s+RESTRICT\b"
    )
    return re.search(pattern, table_sql, flags=re.IGNORECASE) is not None


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
            column_names = (
                None
                if any(column.get("name") is None for column in columns)
                else tuple(str(column["name"]) for column in columns)
            )
            result[name] = (bool(index.get("unique")), column_names)
        return result
    result = {}
    for index in sa.inspect(bind).get_indexes(table_name):
        raw_columns = index.get("column_names")
        columns = (
            None
            if raw_columns is None or any(column is None for column in raw_columns)
            else tuple(str(column) for column in raw_columns)
        )
        result[str(index["name"])] = (bool(index.get("unique")), columns)
    return result


def _validate_auth_table(bind: sa.Connection, table_name: str) -> None:
    _validate_columns(bind, table_name, _AUTH_COLUMN_SHAPES[table_name])
    primary_keys = {
        _USER_CREDENTIALS: ("user_id",),
        _BROWSER_SESSIONS: ("id",),
        _PASSWORD_RESETS: ("id",),
    }
    unique_constraints = {
        _USER_CREDENTIALS: {("uq_user_credentials_login_name", ("login_name",))},
        _BROWSER_SESSIONS: {("uq_browser_auth_sessions_token_hash", ("token_hash",))},
        _PASSWORD_RESETS: {("uq_password_reset_grants_token_hash", ("token_hash",))},
    }
    checks = {
        _USER_CREDENTIALS: {("ck_user_credentials_auth_revision", "auth_revision>=1")},
        _BROWSER_SESSIONS: {("ck_browser_auth_sessions_auth_revision", "auth_revision>=1")},
        _PASSWORD_RESETS: {("ck_password_reset_grants_auth_revision", "auth_revision>=1")},
    }
    foreign_keys = {
        _USER_CREDENTIALS: {
            (("user_id",), "users", ("id",), "RESTRICT", "fk_user_credentials_user_id_users")
        },
        _BROWSER_SESSIONS: {
            (("user_id",), "users", ("id",), "RESTRICT", "fk_browser_auth_sessions_user_id_users")
        },
        _PASSWORD_RESETS: {
            (("user_id",), "users", ("id",), "RESTRICT", "fk_password_reset_grants_user_id_users"),
            (
                ("created_by_user_id",),
                "users",
                ("id",),
                "RESTRICT",
                "fk_password_reset_grants_created_by_user_id_users",
            ),
        },
    }
    expected_indexes = {
        _USER_CREDENTIALS: {},
        _BROWSER_SESSIONS: {"ix_browser_auth_sessions_user_id": (False, ("user_id",))},
        _PASSWORD_RESETS: {"ix_password_reset_grants_user_id": (False, ("user_id",))},
    }
    if _primary_key(bind, table_name) != primary_keys[table_name]:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has an unexpected primary key."
        )
    if _unique_signatures(bind, table_name) != unique_constraints[table_name]:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected unique constraints."
        )
    if _check_signatures(bind, table_name) != checks[table_name]:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected checks."
        )
    if _foreign_key_signatures(bind, table_name) != foreign_keys[table_name]:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected foreign keys."
        )
    if _index_signatures(bind, table_name) != expected_indexes[table_name]:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected indexes."
        )
    triggers = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table_name LIMIT 1"
        ),
        {"table_name": table_name},
    ).first()
    if triggers:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected triggers."
        )


def _target_state(bind: sa.Connection) -> str:
    tables = _table_names(bind)
    present = set(_AUTH_TABLES) & tables
    if present and present != set(_AUTH_TABLES):
        raise RuntimeError(
            "Cannot migrate account authentication; only some authentication tables exist."
        )
    if not present:
        objects = bind.execute(
            sa.text(
                "SELECT type, name FROM sqlite_master WHERE name IN "
                "(:credential, :sessions, :resets, :session_index, :reset_index)"
            ),
            {
                "credential": _USER_CREDENTIALS,
                "sessions": _BROWSER_SESSIONS,
                "resets": _PASSWORD_RESETS,
                "session_index": "ix_browser_auth_sessions_user_id",
                "reset_index": "ix_password_reset_grants_user_id",
            },
        ).all()
        if objects:
            raise RuntimeError(
                "Cannot migrate account authentication; a reserved authentication object is malformed."
            )
        return "absent"

    for table_name in _AUTH_TABLES:
        _validate_auth_table(bind, table_name)
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first():
            raise RuntimeError(
                "Cannot migrate account authentication; early authentication tables must be empty."
            )
    return "present"


def _validate_simple_table(
    bind: sa.Connection,
    table_name: str,
    columns: Mapping[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]],
    primary_key: tuple[str, ...],
    *,
    unique_columns: set[tuple[str, ...]] | None = None,
    foreign_keys: set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]] | None = None,
) -> None:
    _validate_columns(bind, table_name, columns)
    if _primary_key(bind, table_name) != primary_key:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has an unexpected primary key."
        )
    if {columns for _, columns in _unique_signatures(bind, table_name)} != (
        unique_columns or set()
    ):
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected unique constraints."
        )
    if _check_signatures(bind, table_name):
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected checks."
        )
    if _foreign_key_signatures(bind, table_name) != (foreign_keys or set()):
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected foreign keys."
        )


def _validate_link_table(
    bind: sa.Connection,
    table_name: str,
    *,
    allow_populated: bool = False,
) -> bool:
    specs = dict(_API_TOKEN_COLUMNS if table_name == _API_TOKENS else _AUTOMATION_CLIENT_COLUMNS)
    columns = _columns(bind, table_name)
    has_user = "user_id" in columns
    has_revision = "user_auth_revision" in columns
    if has_user != has_revision:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has partial user linkage."
        )
    linked = has_user and has_revision
    if linked:
        specs["user_id"] = (sa.String, 36, True, False)
        specs["user_auth_revision"] = (sa.Integer, None, True, False)
    _validate_columns(
        bind,
        table_name,
        specs,
        allow_server_defaults=table_name == _API_TOKENS,
    )
    expected_pk = ("id",)
    if _primary_key(bind, table_name) != expected_pk:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has an unexpected primary key."
        )
    unique_columns = _sqlite_unique_constraint_columns(bind, table_name)
    expected_unique = {("token_hash",)} if table_name == _API_TOKENS else {("subject",)}
    if unique_columns != expected_unique:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected unique constraints."
        )
    expected_checks = set()
    expected_fks: set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]] = set()
    if table_name == _API_TOKENS:
        expected_fks.update(
            {
                (("client_id",), "automation_clients", ("id",), "SET NULL", None),
                (
                    ("client_id",),
                    "automation_clients",
                    ("id",),
                    "SET NULL",
                    "fk_api_tokens_client_id_automation_clients",
                ),
            }
        )
    if linked:
        _, check_name, index_name = _LINKAGE[table_name]
        expected_checks.add(
            (
                check_name,
                "user_idisnullanduser_auth_revisionisnulloruser_idisnotnullanduser_auth_revisionisnotnullanduser_auth_revision>=1",
            )
        )
        expected_indexes = _index_signatures(bind, table_name)
        if expected_indexes.get(index_name) != (False, ("user_id",)):
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} user index is malformed."
            )
        populated_filter = (
            "(user_id IS NULL) != (user_auth_revision IS NULL) "
            "OR (user_id IS NOT NULL AND user_auth_revision < 1)"
            if allow_populated
            else "user_id IS NOT NULL OR user_auth_revision IS NOT NULL"
        )
        if bind.execute(
            sa.text(f"SELECT 1 FROM {table_name} WHERE {populated_filter} LIMIT 1")
        ).first():
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} linkage is malformed."
            )
    else:
        expected_indexes = _index_signatures(bind, table_name)
        reserved_index = _LINKAGE[table_name][2]
        if reserved_index in expected_indexes:
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} has a reserved index name."
            )
    foreign_signatures = _foreign_key_signatures(bind, table_name)
    if table_name == _API_TOKENS:
        client_keys = {
            signature for signature in foreign_signatures if signature[0] == ("client_id",)
        }
        if len(client_keys) != 1 or not any(
            signature[:4] == (("client_id",), "automation_clients", ("id",), "SET NULL")
            for signature in client_keys
        ):
            raise RuntimeError(
                "Cannot migrate account authentication; api_tokens has an unexpected client foreign key."
            )
        foreign_signatures -= client_keys
        expected_fks = {signature for signature in expected_fks if signature[0] != ("client_id",)}
    if linked:
        fk_name = _LINKAGE[table_name][0]
        user_keys = {signature for signature in foreign_signatures if signature[0] == ("user_id",)}
        if len(user_keys) != 1:
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} has an unexpected user foreign key."
            )
        user_key = next(iter(user_keys))
        if user_key[:3] != (("user_id",), "users", ("id",)) or not _has_named_user_foreign_key(
            bind, table_name, fk_name
        ):
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} has an unexpected user foreign key."
            )
        foreign_signatures -= user_keys
    if _check_signatures(bind, table_name) != expected_checks or foreign_signatures != expected_fks:
        raise RuntimeError(
            f"Cannot migrate account authentication; {table_name} has unexpected linkage constraints."
        )
    if not linked:
        existing_names = set(_index_signatures(bind, table_name))
        if _LINKAGE[table_name][2] in existing_names:
            raise RuntimeError(
                f"Cannot migrate account authentication; {table_name} has a conflicting user index."
            )
    return linked


def _validate_source(
    bind: sa.Connection,
    *,
    allow_populated_links: bool = False,
) -> tuple[str, str | None, Any, Any]:
    tables = _table_names(bind)
    required = {"users", _OWNER_ACCOUNT, _API_TOKENS, _AUTOMATION_CLIENTS}
    if not required.issubset(tables):
        raise RuntimeError(
            "Cannot migrate account authentication; required identity or credential tables are missing."
        )
    _validate_simple_table(
        bind,
        "users",
        _USER_COLUMNS,
        ("id",),
        unique_columns={("legacy_subject",)},
    )
    _validate_simple_table(
        bind,
        _OWNER_ACCOUNT,
        _OWNER_COLUMNS,
        ("singleton_id",),
    )
    api_linked = _validate_link_table(bind, _API_TOKENS, allow_populated=allow_populated_links)
    clients_linked = _validate_link_table(
        bind, _AUTOMATION_CLIENTS, allow_populated=allow_populated_links
    )
    if allow_populated_links and not (api_linked and clients_linked):
        raise RuntimeError(
            "Cannot downgrade account authentication; credential linkage columns are missing."
        )

    owner_rows = (
        bind.execute(
            sa.text(
                "SELECT id, created_at, updated_at FROM users "
                "WHERE legacy_subject = 'owner' ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    if len(owner_rows) != 1:
        raise RuntimeError(
            "Cannot migrate account authentication; the initial owner is missing or ambiguous."
        )
    owner_id = owner_rows[0]["id"]
    if not isinstance(owner_id, str) or not owner_id:
        raise RuntimeError(
            "Cannot migrate account authentication; the initial owner ID is malformed."
        )
    unexpected_owner = bind.execute(
        sa.text("SELECT 1 FROM owner_account WHERE singleton_id != 1 LIMIT 1")
    ).first()
    if unexpected_owner:
        raise RuntimeError(
            "Cannot migrate account authentication; owner_account has a noncanonical singleton ID."
        )
    account_rows = (
        bind.execute(
            sa.text(
                "SELECT password_hash, created_at, updated_at FROM owner_account "
                "WHERE singleton_id = 1"
            )
        )
        .mappings()
        .all()
    )
    if len(account_rows) > 1:
        raise RuntimeError("Cannot migrate account authentication; owner_account is ambiguous.")
    if account_rows:
        password_hash = account_rows[0]["password_hash"]
        credential_created = account_rows[0]["created_at"]
        credential_updated = account_rows[0]["updated_at"]
        if not isinstance(password_hash, str):
            raise RuntimeError(
                "Cannot migrate account authentication; owner_account password hash is malformed."
            )
    else:
        password_hash = None
        credential_created = owner_rows[0]["created_at"]
        credential_updated = owner_rows[0]["updated_at"]
    return owner_id, password_hash, credential_created, credential_updated


def _create_auth_tables() -> None:
    op.create_table(
        _USER_CREDENTIALS,
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("login_name", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("auth_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("auth_revision >= 1", name="ck_user_credentials_auth_revision"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_credentials_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("login_name", name="uq_user_credentials_login_name"),
    )
    op.create_table(
        _BROWSER_SESSIONS,
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("auth_revision", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("principal_kind", sa.String(40), nullable=False),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("client_id", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("auth_revision >= 1", name="ck_browser_auth_sessions_auth_revision"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_browser_auth_sessions_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_browser_auth_sessions_token_hash"),
    )
    op.create_index(
        "ix_browser_auth_sessions_user_id", _BROWSER_SESSIONS, ["user_id"], unique=False
    )
    op.create_table(
        _PASSWORD_RESETS,
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("auth_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("auth_revision >= 1", name="ck_password_reset_grants_auth_revision"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_password_reset_grants_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_password_reset_grants_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_grants_token_hash"),
    )
    op.create_index("ix_password_reset_grants_user_id", _PASSWORD_RESETS, ["user_id"], unique=False)


def _add_linkage(bind: sa.Connection, table_name: str, *, already_linked: bool) -> None:
    if already_linked:
        return
    fk_name, check_name, index_name = _LINKAGE[table_name]
    if bind.dialect.name == "sqlite":
        quote = bind.dialect.identifier_preparer.quote
        quoted_table = quote(table_name)
        quoted_fk = quote(fk_name)
        quoted_check = quote(check_name)
        bind.exec_driver_sql(
            f"ALTER TABLE {quoted_table} ADD COLUMN user_id VARCHAR(36) "
            f"CONSTRAINT {quoted_fk} REFERENCES users (id) ON DELETE RESTRICT"
        )
        bind.exec_driver_sql(
            f"ALTER TABLE {quoted_table} ADD COLUMN user_auth_revision INTEGER "
            f"CONSTRAINT {quoted_check} CHECK ((user_id IS NULL AND user_auth_revision IS NULL) "
            "OR (user_id IS NOT NULL AND user_auth_revision IS NOT NULL "
            "AND user_auth_revision >= 1))"
        )
    else:
        op.add_column(
            table_name,
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="RESTRICT", name=fk_name),
                nullable=True,
            ),
        )
        check = sa.CheckConstraint(
            "(user_id IS NULL AND user_auth_revision IS NULL) OR "
            "(user_id IS NOT NULL AND user_auth_revision IS NOT NULL "
            "AND user_auth_revision >= 1)",
            name=check_name,
        )
        op.add_column(
            table_name,
            sa.Column("user_auth_revision", sa.Integer(), check, nullable=True),
        )
    op.create_index(index_name, table_name, ["user_id"], unique=False)


def _validate_no_inbound_auth_references(bind: sa.Connection) -> None:
    for table_name in sorted(_table_names(bind) - set(_AUTH_TABLES)):
        if any(
            key.get("referred_table") in _AUTH_TABLES
            for key in sa.inspect(bind).get_foreign_keys(table_name)
        ):
            raise RuntimeError(
                "Cannot downgrade account authentication; another table references an authentication table."
            )


def upgrade() -> None:
    bind = op.get_bind()
    owner_id, password_hash, credential_created, credential_updated = _validate_source(bind)
    target_state = _target_state(bind)
    api_linked = "user_id" in _columns(bind, _API_TOKENS)
    client_linked = "user_id" in _columns(bind, _AUTOMATION_CLIENTS)

    if target_state == "absent":
        _create_auth_tables()
    _add_linkage(bind, _API_TOKENS, already_linked=api_linked)
    _add_linkage(bind, _AUTOMATION_CLIENTS, already_linked=client_linked)

    bind.execute(
        sa.text(
            "INSERT INTO user_credentials "
            "(user_id, login_name, password_hash, auth_revision, created_at, updated_at) "
            "VALUES (:user_id, :login_name, :password_hash, 1, :created_at, :updated_at)"
        ),
        {
            "user_id": owner_id,
            "login_name": _INITIAL_LOGIN,
            "password_hash": password_hash,
            "created_at": credential_created,
            "updated_at": credential_updated,
        },
    )
    for table_name in (_API_TOKENS, _AUTOMATION_CLIENTS):
        bind.execute(
            sa.text(f"UPDATE {table_name} SET user_id = :user_id, user_auth_revision = 1"),
            {"user_id": owner_id},
        )


def _validate_downgrade_state(bind: sa.Connection) -> tuple[str, Any, Any, Any]:
    owner_id, _, _, _ = _validate_source(bind, allow_populated_links=True)
    _validate_linkage_triggers(bind)
    for table_name in _AUTH_TABLES:
        _validate_auth_table(bind, table_name)
    _validate_no_inbound_auth_references(bind)
    credentials = (
        bind.execute(
            sa.text(
                "SELECT user_id, login_name, password_hash, auth_revision, created_at, updated_at "
                "FROM user_credentials ORDER BY user_id"
            )
        )
        .mappings()
        .all()
    )
    if len(credentials) != 1 or credentials[0]["user_id"] != owner_id:
        raise RuntimeError(
            "Cannot downgrade account authentication; noninitial credentials must be retained."
        )
    credential = credentials[0]
    if credential["login_name"] != _INITIAL_LOGIN:
        raise RuntimeError(
            "Cannot downgrade account authentication; the initial login name changed."
        )
    for table_name in (_BROWSER_SESSIONS, _PASSWORD_RESETS):
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first():
            raise RuntimeError(
                "Cannot downgrade account authentication; browser sessions or reset grants must be retained."
            )
    for table_name in (_API_TOKENS, _AUTOMATION_CLIENTS):
        if bind.execute(
            sa.text(f"SELECT 1 FROM {table_name} WHERE user_id != :owner_id LIMIT 1"),
            {"owner_id": owner_id},
        ).first():
            raise RuntimeError(
                "Cannot downgrade account authentication; foreign credential linkage must be retained."
            )
    account_rows = (
        bind.execute(sa.text("SELECT singleton_id FROM owner_account ORDER BY singleton_id"))
        .scalars()
        .all()
        if _OWNER_ACCOUNT in _table_names(bind)
        else []
    )
    if any(singleton_id != 1 for singleton_id in account_rows):
        raise RuntimeError(
            "Cannot downgrade account authentication; owner_account has a noncanonical singleton ID."
        )
    return (
        owner_id,
        credential["password_hash"],
        credential["created_at"],
        credential["updated_at"],
    )


def _table_triggers(bind: sa.Connection, table_name: str) -> list[tuple[str, str]]:
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = :table_name ORDER BY name"
        ),
        {"table_name": table_name},
    ).all()
    if any(sql is None for _, sql in rows):
        raise RuntimeError(
            f"Cannot downgrade account authentication; {table_name} has an unpreservable trigger."
        )
    return [(str(name), str(sql)) for name, sql in rows]


def _validate_linkage_triggers(bind: sa.Connection) -> None:
    for table_name in (_API_TOKENS, _AUTOMATION_CLIENTS):
        for _, trigger_sql in _table_triggers(bind, table_name):
            if re.search(r"\b(?:user_id|user_auth_revision)\b", trigger_sql, re.IGNORECASE):
                raise RuntimeError(
                    "Cannot downgrade account authentication; a source trigger references "
                    "authentication linkage that would be removed."
                )


def _drop_linkage(bind: sa.Connection, table_name: str) -> None:
    fk_name, check_name, index_name = _LINKAGE[table_name]
    triggers = _table_triggers(bind, table_name)
    if any(
        re.search(r"\b(?:user_id|user_auth_revision)\b", trigger_sql, re.IGNORECASE)
        for _, trigger_sql in triggers
    ):
        raise RuntimeError(
            "Cannot downgrade account authentication; a source trigger references "
            "authentication linkage that would be removed."
        )
    indexes = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"
        ),
        {"table_name": table_name},
    ).all()
    for index_name_saved, _ in indexes:
        op.drop_index(str(index_name_saved), table_name=table_name)
    index_sql = [
        (str(name), str(sql)) for name, sql in indexes if name != index_name and sql is not None
    ]
    copy_table = _copy_table_with_unique_constraints(bind, table_name)
    with op.batch_alter_table(table_name, recreate="always", copy_from=copy_table) as batch:
        batch.drop_constraint(check_name, type_="check")
        if any(
            signature[0] == ("user_id",) and signature[4] == fk_name
            for signature in _foreign_key_signatures(bind, table_name)
        ):
            batch.drop_constraint(fk_name, type_="foreignkey")
        batch.drop_column("user_auth_revision")
        batch.drop_column("user_id")
    for _, sql in index_sql:
        bind.exec_driver_sql(sql)
    for _, sql in triggers:
        bind.exec_driver_sql(sql)


def downgrade() -> None:
    bind = op.get_bind()
    owner_id, password_hash, created_at, updated_at = _validate_downgrade_state(bind)
    if _OWNER_ACCOUNT not in _table_names(bind):
        raise RuntimeError("Cannot downgrade account authentication; owner_account is missing.")
    owner_row = bind.execute(sa.text("SELECT 1 FROM owner_account WHERE singleton_id = 1")).first()
    if password_hash is None:
        bind.execute(sa.text("DELETE FROM owner_account WHERE singleton_id = 1"))
    elif owner_row:
        bind.execute(
            sa.text(
                "UPDATE owner_account SET password_hash = :password_hash, "
                "created_at = :created_at, updated_at = :updated_at WHERE singleton_id = 1"
            ),
            {
                "password_hash": password_hash,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
    else:
        bind.execute(
            sa.text(
                "INSERT INTO owner_account "
                "(singleton_id, password_hash, created_at, updated_at) "
                "VALUES (1, :password_hash, :created_at, :updated_at)"
            ),
            {
                "password_hash": password_hash,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
    op.drop_index("ix_password_reset_grants_user_id", table_name=_PASSWORD_RESETS)
    op.drop_index("ix_browser_auth_sessions_user_id", table_name=_BROWSER_SESSIONS)
    op.drop_table(_PASSWORD_RESETS)
    op.drop_table(_BROWSER_SESSIONS)
    op.drop_table(_USER_CREDENTIALS)
    _drop_linkage(bind, _AUTOMATION_CLIENTS)
    _drop_linkage(bind, _API_TOKENS)
