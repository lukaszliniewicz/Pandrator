"""Store explicit resource upper bounds for credential owners."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import RowMapping

revision = "0081_credential_resource_grants"
down_revision = "0080_configuration_scope_consistency"
branch_labels = None
depends_on = None

# These are intentionally migration-local: a historical migration must not
# import the application's current ORM or authorization services.
_GRANT_TABLES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "api_token_workspace_grants",
        "token_id",
        "api_tokens",
        "workspace_id",
        "workspaces",
    ),
    (
        "api_token_project_grants",
        "token_id",
        "api_tokens",
        "project_id",
        "projects",
    ),
    (
        "automation_client_workspace_grants",
        "client_id",
        "automation_clients",
        "workspace_id",
        "workspaces",
    ),
    (
        "automation_client_project_grants",
        "client_id",
        "automation_clients",
        "project_id",
        "projects",
    ),
    (
        "browser_auth_workspace_grants",
        "browser_session_id",
        "browser_auth_sessions",
        "workspace_id",
        "workspaces",
    ),
    (
        "browser_auth_project_grants",
        "browser_session_id",
        "browser_auth_sessions",
        "project_id",
        "projects",
    ),
    (
        "automation_code_workspace_grants",
        "code_grant_id",
        "automation_enrollment_grants",
        "workspace_id",
        "workspaces",
    ),
    (
        "automation_code_project_grants",
        "code_grant_id",
        "automation_enrollment_grants",
        "project_id",
        "projects",
    ),
)

_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "users": {"id"},
    "workspaces": {"id", "kind", "personal_owner_user_id"},
    "workspace_memberships": {"workspace_id", "user_id", "role"},
    "projects": {"id", "workspace_id"},
    "api_tokens": {"id", "user_id", "user_auth_revision"},
    "automation_clients": {"id", "user_id", "user_auth_revision"},
    "automation_enrollment_grants": {"id", "client_id"},
    "browser_auth_sessions": {
        "id",
        "user_id",
        "auth_revision",
        "principal_kind",
        "client_id",
    },
}

_REQUIRED_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "users": ("id",),
    "workspaces": ("id",),
    "workspace_memberships": ("workspace_id", "user_id"),
    "projects": ("id",),
    "api_tokens": ("id",),
    "automation_clients": ("id",),
    "automation_enrollment_grants": ("id",),
    "browser_auth_sessions": ("id",),
}

_REQUIRED_NULLABILITY: dict[str, dict[str, bool]] = {
    "users": {"id": False},
    "workspaces": {"id": False, "kind": False, "personal_owner_user_id": True},
    "workspace_memberships": {
        "workspace_id": False,
        "user_id": False,
        "role": False,
    },
    "projects": {"id": False, "workspace_id": False},
    "api_tokens": {"id": False, "user_id": True, "user_auth_revision": True},
    "automation_clients": {
        "id": False,
        "user_id": True,
        "user_auth_revision": True,
    },
    "automation_enrollment_grants": {"id": False, "client_id": False},
    "browser_auth_sessions": {
        "id": False,
        "user_id": False,
        "auth_revision": False,
        "principal_kind": False,
        "client_id": True,
    },
}

_REQUIRED_FOREIGN_KEYS = (
    (
        "workspaces",
        ("personal_owner_user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
    (
        "workspace_memberships",
        ("workspace_id",),
        "workspaces",
        ("id",),
        "CASCADE",
    ),
    (
        "workspace_memberships",
        ("user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
    ("projects", ("workspace_id",), "workspaces", ("id",), "RESTRICT"),
    ("api_tokens", ("user_id",), "users", ("id",), "RESTRICT"),
    (
        "automation_clients",
        ("user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
    (
        "automation_enrollment_grants",
        ("client_id",),
        "automation_clients",
        ("id",),
        "CASCADE",
    ),
    (
        "browser_auth_sessions",
        ("user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
)

_STRING_36_COLUMNS = (
    ("users", "id"),
    ("workspaces", "id"),
    ("workspaces", "personal_owner_user_id"),
    ("workspace_memberships", "workspace_id"),
    ("workspace_memberships", "user_id"),
    ("projects", "id"),
    ("projects", "workspace_id"),
    ("api_tokens", "id"),
    ("api_tokens", "user_id"),
    ("automation_clients", "id"),
    ("automation_clients", "user_id"),
    ("automation_enrollment_grants", "id"),
    ("automation_enrollment_grants", "client_id"),
    ("browser_auth_sessions", "id"),
    ("browser_auth_sessions", "user_id"),
)

_INTEGER_COLUMNS = (
    ("api_tokens", "user_auth_revision"),
    ("automation_clients", "user_auth_revision"),
    ("browser_auth_sessions", "auth_revision"),
)


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Credential resource grants migration is supported only on SQLite.")
    return bind


def _quote(bind: sa.Connection, identifier: str) -> str:
    return bind.dialect.identifier_preparer.quote(identifier)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, RowMapping]:
    quoted_table = _quote(bind, table_name)
    rows = bind.exec_driver_sql(f"PRAGMA table_info({quoted_table})").mappings().all()
    return {str(row["name"]): row for row in rows}


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> tuple[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None], ...]:
    # SQLite's catalog retains actions when Inspector omits them on historical unnamed FKs.
    inspector_names: dict[tuple[tuple[str, ...], str, tuple[str, ...]], str | None] = {}
    ambiguous_names: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
        structure = (
            tuple(foreign_key.get("constrained_columns") or ()),
            str(foreign_key.get("referred_table") or ""),
            tuple(foreign_key.get("referred_columns") or ()),
        )
        if structure in inspector_names:
            ambiguous_names.add(structure)
        else:
            name = foreign_key.get("name")
            inspector_names[structure] = str(name) if name is not None else None

    quoted_table = _quote(bind, table_name)
    rows = bind.exec_driver_sql(f"PRAGMA foreign_key_list({quoted_table})").mappings().all()
    groups: dict[int, list[RowMapping]] = {}
    for row in rows:
        groups.setdefault(int(row["id"]), []).append(row)

    signatures = []
    for group in groups.values():
        ordered = sorted(group, key=lambda row: int(row["seq"]))
        structure = (
            tuple(str(row["from"]) for row in ordered),
            str(ordered[0]["table"]),
            tuple(str(row["to"]) for row in ordered),
        )
        name = None if structure in ambiguous_names else inspector_names.get(structure)
        signatures.append(
            (
                structure[0],
                structure[1],
                structure[2],
                str(ordered[0]["on_delete"] or "").upper(),
                name,
            )
        )
    return tuple(signatures)


def _validate_required_schema(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    available_tables = set(inspector.get_table_names())
    missing_tables = set(_REQUIRED_COLUMNS) - available_tables
    if missing_tables:
        raise RuntimeError(
            "Cannot create credential resource grants; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )

    table_columns: dict[str, dict[str, RowMapping]] = {}
    for table_name, expected_columns in _REQUIRED_COLUMNS.items():
        columns = _columns(bind, table_name)
        table_columns[table_name] = columns
        missing_columns = expected_columns - set(columns)
        if missing_columns:
            raise RuntimeError(
                "Cannot create credential resource grants; "
                f"{table_name} is missing required columns: " + ", ".join(sorted(missing_columns))
            )

        primary_key = tuple(
            name
            for name, column in sorted(columns.items(), key=lambda item: int(item[1]["pk"]))
            if int(column["pk"]) > 0
        )
        if primary_key != _REQUIRED_PRIMARY_KEYS[table_name]:
            raise RuntimeError(
                "Cannot create credential resource grants; "
                f"{table_name} has an unexpected primary-key shape."
            )

        for column_name, nullable in _REQUIRED_NULLABILITY[table_name].items():
            if bool(columns[column_name]["notnull"]) != (not nullable):
                raise RuntimeError(
                    "Cannot create credential resource grants; "
                    f"{table_name}.{column_name} has unexpected nullability."
                )

    for table_name, column_name in _STRING_36_COLUMNS:
        column_type = str(table_columns[table_name][column_name]["type"]).upper()
        if column_type != "VARCHAR(36)":
            raise RuntimeError(
                "Cannot create credential resource grants; "
                f"{table_name}.{column_name} is not VARCHAR(36)."
            )
    for table_name, column_name in _INTEGER_COLUMNS:
        column_type = str(table_columns[table_name][column_name]["type"]).upper()
        if column_type != "INTEGER":
            raise RuntimeError(
                "Cannot create credential resource grants; "
                f"{table_name}.{column_name} is not INTEGER."
            )

    for table_name, columns, target_table, target_columns, ondelete in _REQUIRED_FOREIGN_KEYS:
        actual = [
            signature
            for signature in _foreign_key_signatures(bind, table_name)
            if signature[0] == columns
        ]
        if len(actual) != 1 or actual[0][1:4] != (
            target_table,
            target_columns,
            ondelete,
        ):
            raise RuntimeError(
                "Cannot create credential resource grants; "
                f"{table_name}.{columns[0]} has an unexpected foreign-key target or action."
            )

    browser_client_foreign_keys = [
        signature
        for signature in _foreign_key_signatures(bind, "browser_auth_sessions")
        if "client_id" in signature[0]
    ]
    if browser_client_foreign_keys:
        raise RuntimeError(
            "Cannot create credential resource grants; "
            "browser_auth_sessions.client_id must remain an unbound browser provenance value."
        )

    if not _has_unique_personal_owner_mapping(bind):
        raise RuntimeError(
            "Cannot create credential resource grants; workspaces.personal_owner_user_id "
            "is not uniquely indexed."
        )


def _has_unique_personal_owner_mapping(bind: sa.Connection) -> bool:
    quoted_table = _quote(bind, "workspaces")
    for row in bind.exec_driver_sql(f"PRAGMA index_list({quoted_table})").all():
        # SQLite reports: seq, name, unique, origin, partial.
        index_name = str(row[1])
        is_unique = bool(row[2])
        is_partial = bool(row[4]) if len(row) > 4 else False
        if not is_unique or is_partial:
            continue
        quoted_index = _quote(bind, index_name)
        indexed_columns = tuple(
            str(index_row[2])
            for index_row in bind.exec_driver_sql(f"PRAGMA index_info({quoted_index})").all()
            if index_row[2] is not None
        )
        if indexed_columns == ("personal_owner_user_id",):
            return True
    return False


def _validate_data(bind: sa.Connection) -> None:
    duplicate_owner = bind.exec_driver_sql(
        """SELECT personal_owner_user_id
FROM workspaces
WHERE personal_owner_user_id IS NOT NULL
GROUP BY personal_owner_user_id
HAVING COUNT(*) > 1
LIMIT 1"""
    ).first()
    if duplicate_owner is not None:
        raise RuntimeError(
            "Cannot create credential resource grants; personal workspace owner mapping is ambiguous."
        )

    for table_name in ("api_tokens", "automation_clients"):
        invalid_pair = bind.exec_driver_sql(
            f"""SELECT id
FROM {table_name}
WHERE (user_id IS NULL) != (user_auth_revision IS NULL)
   OR (user_auth_revision IS NOT NULL AND (
       typeof(user_auth_revision) != 'integer' OR user_auth_revision <= 0
   ))
LIMIT 1"""
        ).first()
        if invalid_pair is not None:
            raise RuntimeError(
                "Cannot create credential resource grants; "
                f"{table_name} has an illegal user_id/user_auth_revision pair."
            )

    invalid_browser = bind.exec_driver_sql(
        """SELECT id
FROM browser_auth_sessions
WHERE user_id IS NULL
   OR auth_revision IS NULL
   OR typeof(auth_revision) != 'integer'
   OR auth_revision <= 0
   OR principal_kind IS NULL
LIMIT 1"""
    ).first()
    if invalid_browser is not None:
        raise RuntimeError(
            "Cannot create credential resource grants; browser_auth_sessions "
            "contains a NULL identity field or nonpositive auth revision."
        )

    foreign_key_error = bind.exec_driver_sql("PRAGMA foreign_key_check").first()
    if foreign_key_error is not None:
        table_name, row_id, parent_table, foreign_key_id = foreign_key_error
        raise RuntimeError(
            "Cannot create credential resource grants; existing foreign-key integrity "
            f"failed for {table_name} row {row_id} referencing {parent_table} "
            f"(constraint {foreign_key_id})."
        )


def _reject_name_collisions(bind: sa.Connection) -> None:
    names = []
    for table_name, _owner_column, _owner_table, target_column, _target_table in _GRANT_TABLES:
        names.extend((table_name, f"ix_{table_name}_{target_column}"))
    for name in names:
        collision = bind.execute(
            sa.text("SELECT type FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if collision is not None:
            raise RuntimeError(
                "Cannot create credential resource grants; owned schema name "
                f"{name} is already in use."
            )


def _create_grant_tables() -> None:
    for table_name, owner_column, owner_table, target_column, target_table in _GRANT_TABLES:
        op.create_table(
            table_name,
            sa.Column(owner_column, sa.String(36), nullable=False),
            sa.Column(target_column, sa.String(36), nullable=False),
            sa.ForeignKeyConstraint(
                [owner_column],
                [f"{owner_table}.id"],
                name=f"fk_{table_name}_{owner_column}",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                [target_column],
                [f"{target_table}.id"],
                name=f"fk_{table_name}_{target_column}",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint(owner_column, target_column),
        )
        op.create_index(
            f"ix_{table_name}_{target_column}",
            table_name,
            [target_column],
            unique=False,
        )


def _backfill_workspace_grants(bind: sa.Connection) -> None:
    bind.exec_driver_sql(
        """INSERT INTO api_token_workspace_grants (token_id, workspace_id)
SELECT token.id, workspace.id
FROM api_tokens AS token
JOIN workspaces AS workspace
  ON workspace.personal_owner_user_id = token.user_id
 AND workspace.kind = 'personal'
JOIN workspace_memberships AS membership
  ON membership.workspace_id = workspace.id
 AND membership.user_id = token.user_id
 AND membership.role = 'owner'
WHERE token.user_id IS NOT NULL"""
    )
    bind.exec_driver_sql(
        """INSERT INTO automation_client_workspace_grants (client_id, workspace_id)
SELECT client.id, workspace.id
FROM automation_clients AS client
JOIN workspaces AS workspace
  ON workspace.personal_owner_user_id = client.user_id
 AND workspace.kind = 'personal'
JOIN workspace_memberships AS membership
  ON membership.workspace_id = workspace.id
 AND membership.user_id = client.user_id
 AND membership.role = 'owner'
WHERE client.user_id IS NOT NULL"""
    )
    bind.exec_driver_sql(
        """INSERT INTO browser_auth_workspace_grants (browser_session_id, workspace_id)
SELECT browser.id, workspace.id
FROM browser_auth_sessions AS browser
JOIN workspaces AS workspace
  ON workspace.personal_owner_user_id = browser.user_id
 AND workspace.kind = 'personal'
JOIN workspace_memberships AS membership
  ON membership.workspace_id = workspace.id
 AND membership.user_id = browser.user_id
 AND membership.role = 'owner'
WHERE browser.principal_kind <> 'owner_session'
   OR browser.client_id IS NOT NULL"""
    )
    bind.exec_driver_sql(
        """INSERT INTO automation_code_workspace_grants (code_grant_id, workspace_id)
SELECT code.id, client_grant.workspace_id
FROM automation_enrollment_grants AS code
JOIN automation_client_workspace_grants AS client_grant
  ON client_grant.client_id = code.client_id"""
    )


def upgrade() -> None:
    bind = _sqlite_bind()
    _validate_required_schema(bind)
    _validate_data(bind)
    _reject_name_collisions(bind)
    _create_grant_tables()
    _backfill_workspace_grants(bind)


def _validate_owned_table(
    bind: sa.Connection, table_definition: tuple[str, str, str, str, str]
) -> None:
    table_name, owner_column, owner_table, target_column, target_table = table_definition
    quoted_table = _quote(bind, table_name)
    column_rows = bind.exec_driver_sql(f"PRAGMA table_info({quoted_table})").all()
    expected_columns = (owner_column, target_column)
    actual_columns = tuple(str(row[1]) for row in column_rows)
    if actual_columns != expected_columns:
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{table_name} has missing, extra, or reordered columns."
        )
    expected_primary_key = (1, 2)
    for row, expected_pk in zip(column_rows, expected_primary_key, strict=True):
        column_type = str(row[2]).upper()
        if (
            column_type != "VARCHAR(36)"
            or not bool(row[3])
            or row[4] is not None
            or int(row[5]) != expected_pk
        ):
            raise RuntimeError(
                "Cannot downgrade credential resource grants; "
                f"{table_name}.{row[1]} has drifted from its owned column shape."
            )

    expected_foreign_keys = {
        (
            (owner_column,),
            owner_table,
            ("id",),
            "CASCADE",
            f"fk_{table_name}_{owner_column}",
        ),
        (
            (target_column,),
            target_table,
            ("id",),
            "CASCADE",
            f"fk_{table_name}_{target_column}",
        ),
    }
    actual_foreign_keys = _foreign_key_signatures(bind, table_name)
    if len(actual_foreign_keys) != 2 or set(actual_foreign_keys) != expected_foreign_keys:
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{table_name} has drifted foreign-key definitions."
        )

    expected_index = f"ix_{table_name}_{target_column}"
    index_rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name"
        ),
        {"table_name": table_name},
    ).all()
    explicit_indexes = {str(name): sql for name, sql in index_rows if sql is not None}
    if set(explicit_indexes) != {expected_index}:
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{table_name} has a missing or unexpected explicit index."
        )
    implicit_indexes = [name for name, sql in index_rows if sql is None]
    if len(implicit_indexes) != 1:
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{table_name} has an unexpected implicit index or uniqueness constraint."
        )
    quoted_index = _quote(bind, expected_index)
    indexed_columns = tuple(
        str(row[2])
        for row in bind.exec_driver_sql(f"PRAGMA index_info({quoted_index})").all()
        if row[2] is not None
    )
    if indexed_columns != (target_column,):
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{expected_index} has a drifted definition."
        )
    quoted_table = _quote(bind, table_name)
    expected_index_info = bind.exec_driver_sql(f"PRAGMA index_list({quoted_table})").all()
    target_index_info = next(
        (row for row in expected_index_info if str(row[1]) == expected_index),
        None,
    )
    if (
        target_index_info is None
        or bool(target_index_info[2])
        or (len(target_index_info) > 4 and bool(target_index_info[4]))
    ):
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{expected_index} is not the expected non-unique index."
        )

    attached_triggers = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = :table_name LIMIT 1"
        ),
        {"table_name": table_name},
    ).first()
    if attached_triggers is not None:
        raise RuntimeError(
            "Cannot downgrade credential resource grants; "
            f"{table_name} has an unexpected attached trigger."
        )


def _validate_owned_schema(bind: sa.Connection) -> None:
    available_tables = set(sa.inspect(bind).get_table_names())
    expected_tables = {definition[0] for definition in _GRANT_TABLES}
    missing_tables = expected_tables - available_tables
    if missing_tables:
        raise RuntimeError(
            "Cannot downgrade credential resource grants; owned tables are missing: "
            + ", ".join(sorted(missing_tables))
        )

    for definition in _GRANT_TABLES:
        _validate_owned_table(bind, definition)


def downgrade() -> None:
    bind = _sqlite_bind()
    _validate_owned_schema(bind)
    for table_name, _owner_column, _owner_table, target_column, _target_table in reversed(
        _GRANT_TABLES
    ):
        op.drop_index(f"ix_{table_name}_{target_column}", table_name=table_name)
        op.drop_table(table_name)
