from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, Connection

from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
    AutomationCodeProjectGrant,
    AutomationCodeWorkspaceGrant,
    BrowserAuthProjectGrant,
    BrowserAuthWorkspaceGrant,
)

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
PREDECESSOR_REVISION = "0080_configuration_scope_consistency"
TARGET_REVISION = "0081_credential_resource_grants"
OWNED_SCHEMA_NAMES = {
    "api_token_workspace_grants",
    "ix_api_token_workspace_grants_workspace_id",
    "api_token_project_grants",
    "ix_api_token_project_grants_project_id",
    "automation_client_workspace_grants",
    "ix_automation_client_workspace_grants_workspace_id",
    "automation_client_project_grants",
    "ix_automation_client_project_grants_project_id",
    "browser_auth_workspace_grants",
    "ix_browser_auth_workspace_grants_workspace_id",
    "browser_auth_project_grants",
    "ix_browser_auth_project_grants_project_id",
    "automation_code_workspace_grants",
    "ix_automation_code_workspace_grants_workspace_id",
    "automation_code_project_grants",
    "ix_automation_code_project_grants_project_id",
}

GRANTS = (
    (
        ApiTokenWorkspaceGrant,
        "api_token_workspace_grants",
        "token_id",
        "api_tokens",
        "workspace_id",
        "workspaces",
    ),
    (
        ApiTokenProjectGrant,
        "api_token_project_grants",
        "token_id",
        "api_tokens",
        "project_id",
        "projects",
    ),
    (
        AutomationClientWorkspaceGrant,
        "automation_client_workspace_grants",
        "client_id",
        "automation_clients",
        "workspace_id",
        "workspaces",
    ),
    (
        AutomationClientProjectGrant,
        "automation_client_project_grants",
        "client_id",
        "automation_clients",
        "project_id",
        "projects",
    ),
    (
        BrowserAuthWorkspaceGrant,
        "browser_auth_workspace_grants",
        "browser_session_id",
        "browser_auth_sessions",
        "workspace_id",
        "workspaces",
    ),
    (
        BrowserAuthProjectGrant,
        "browser_auth_project_grants",
        "browser_session_id",
        "browser_auth_sessions",
        "project_id",
        "projects",
    ),
    (
        AutomationCodeWorkspaceGrant,
        "automation_code_workspace_grants",
        "code_grant_id",
        "automation_enrollment_grants",
        "workspace_id",
        "workspaces",
    ),
    (
        AutomationCodeProjectGrant,
        "automation_code_project_grants",
        "code_grant_id",
        "automation_enrollment_grants",
        "project_id",
        "projects",
    ),
)

NOW = "2026-10-24T12:00:00+00:00"
PAST = "2020-01-01T00:00:00+00:00"
IDS = {
    "named_user": "91000000-0000-4000-8000-000000000001",
    "inactive_user": "91000000-0000-4000-8000-000000000002",
    "no_workspace_user": "91000000-0000-4000-8000-000000000003",
    "no_owner_membership_user": "91000000-0000-4000-8000-000000000004",
    "named_workspace": "92000000-0000-4000-8000-000000000001",
    "inactive_workspace": "92000000-0000-4000-8000-000000000002",
    "no_owner_membership_workspace": "92000000-0000-4000-8000-000000000003",
    "team_workspace": "92000000-0000-4000-8000-000000000004",
    "team_workspace_delete": "92000000-0000-4000-8000-000000000005",
    "team_workspace_keep": "92000000-0000-4000-8000-000000000006",
    "project_host_workspace": "92000000-0000-4000-8000-000000000007",
    "project_delete": "93000000-0000-4000-8000-000000000001",
    "project_keep": "93000000-0000-4000-8000-000000000002",
    "token_named": "94000000-0000-4000-8000-000000000001",
    "token_inactive": "94000000-0000-4000-8000-000000000002",
    "token_no_workspace": "94000000-0000-4000-8000-000000000003",
    "token_no_owner_membership": "94000000-0000-4000-8000-000000000004",
    "token_unbound_owner_subject": "94000000-0000-4000-8000-000000000005",
    "token_initial_owner": "94000000-0000-4000-8000-000000000006",
    "token_bad_pair": "94000000-0000-4000-8000-000000000007",
    "token_bad_fk": "94000000-0000-4000-8000-000000000008",
    "token_bad_zero_revision": "94000000-0000-4000-8000-000000000009",
    "token_bad_negative_revision": "94000000-0000-4000-8000-000000000010",
    "client_named": "95000000-0000-4000-8000-000000000001",
    "client_inactive": "95000000-0000-4000-8000-000000000002",
    "client_no_workspace": "95000000-0000-4000-8000-000000000003",
    "client_no_owner_membership": "95000000-0000-4000-8000-000000000004",
    "client_unbound_owner_creator": "95000000-0000-4000-8000-000000000005",
    "client_initial_owner": "95000000-0000-4000-8000-000000000006",
    "browser_interactive": "96000000-0000-4000-8000-000000000001",
    "browser_manager": "96000000-0000-4000-8000-000000000002",
    "browser_mcp_local": "96000000-0000-4000-8000-000000000003",
    "browser_named_bound": "96000000-0000-4000-8000-000000000004",
    "browser_inactive": "96000000-0000-4000-8000-000000000005",
    "browser_bad_revision": "96000000-0000-4000-8000-000000000006",
    "code_named": "97000000-0000-4000-8000-000000000001",
    "code_inactive": "97000000-0000-4000-8000-000000000002",
    "code_no_workspace": "97000000-0000-4000-8000-000000000003",
    "code_unbound": "97000000-0000-4000-8000-000000000004",
    "code_initial_owner": "97000000-0000-4000-8000-000000000005",
}


def _config(path: Path) -> Config:
    config = Config()
    # Alembic uses ConfigParser interpolation, so escape SQLAlchemy 2.1 URL %.
    config.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
    config.set_main_option(
        "sqlalchemy.url",
        str(URL.create("sqlite", database=str(path))).replace("%", "%%"),
    )
    return config


def _upgrade(path: Path, revision: str) -> Config:
    config = _config(path)
    command.upgrade(config, revision)
    return config


def _connect(path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    return connection


def _insert_user(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    name: str,
    is_active: bool = True,
    is_admin: bool = False,
) -> None:
    connection.execute(
        """INSERT INTO users (
    id, legacy_subject, display_name, is_active, is_instance_admin, created_at, updated_at
) VALUES (?, NULL, ?, ?, ?, ?, ?)""",
        (user_id, name, int(is_active), int(is_admin), NOW, NOW),
    )


def _insert_workspace(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    name: str,
    kind: str,
    owner_user_id: str | None,
    state: str = "active",
) -> None:
    connection.execute(
        """INSERT INTO workspaces (
    id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (workspace_id, kind, name, owner_user_id, state, NOW, NOW),
    )


def _insert_membership(
    connection: sqlite3.Connection,
    workspace_id: str,
    user_id: str,
    role: str,
) -> None:
    connection.execute(
        """INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at)
VALUES (?, ?, ?, ?)""",
        (workspace_id, user_id, role, NOW),
    )


def _insert_api_token(
    connection: sqlite3.Connection,
    token_id: str,
    *,
    label: str,
    subject: str | None,
    user_id: str | None,
    user_auth_revision: int | None,
    expired: bool = False,
    revoked: bool = False,
) -> None:
    connection.execute(
        """INSERT INTO api_tokens (
    id, label, token_hash, token_prefix, subject, scopes_json, expires_at,
    principal_kind, created_by, user_id, user_auth_revision, client_id,
    target_instance_id, canonical_origin, created_at, last_used_at, revoked_at
) VALUES (?, ?, ?, ?, ?, ?, ?, 'api_token', ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
        (
            token_id,
            label,
            f"hash-{token_id}",
            f"prefix-{token_id[-4:]}",
            subject,
            json.dumps(["catalog:read"]),
            PAST if expired else "2030-01-01T00:00:00+00:00",
            subject or "synthetic-issuer",
            user_id,
            user_auth_revision,
            "instance-00000000000000000000000000000000",
            "https://example.invalid",
            PAST if expired else NOW,
            PAST if expired else NOW,
            PAST if revoked else None,
        ),
    )


def _insert_client(
    connection: sqlite3.Connection,
    client_id: str,
    *,
    user_id: str | None,
    user_auth_revision: int | None,
    expired: bool = False,
    revoked: bool = False,
    created_by: str = "synthetic-issuer",
) -> None:
    connection.execute(
        """INSERT INTO automation_clients (
    id, name, subject, redirect_uris_json, allowed_scopes_json, target_instance_id,
    canonical_origin, created_by, user_id, user_auth_revision, created_at,
    last_used_at, revoked_at
) VALUES (?, ?, ?, '[]', '["catalog:read"]', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            client_id,
            f"Client {client_id[-4:]}",
            f"subject-{client_id}",
            "instance-00000000000000000000000000000000",
            "https://example.invalid",
            created_by,
            user_id,
            user_auth_revision,
            PAST if expired else NOW,
            PAST if expired else NOW,
            PAST if revoked else None,
        ),
    )


def _insert_browser_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    user_id: str,
    principal_kind: str,
    client_id: str | None,
    expired: bool = True,
    revoked: bool = True,
    auth_revision: int = 1,
) -> None:
    connection.execute(
        """INSERT INTO browser_auth_sessions (
    id, user_id, token_hash, auth_revision, subject, principal_kind, scopes_json,
    client_id, created_at, last_seen_at, idle_expires_at, expires_at, revoked_at
) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            user_id,
            f"browser-hash-{session_id}",
            auth_revision,
            f"user:{user_id}",
            principal_kind,
            client_id,
            PAST,
            PAST,
            PAST,
            PAST if expired else "2030-01-01T00:00:00+00:00",
            PAST if revoked else None,
        ),
    )


def _insert_code(
    connection: sqlite3.Connection,
    code_id: str,
    *,
    client_id: str,
    expired: bool = True,
    consumed: bool = True,
) -> None:
    connection.execute(
        """INSERT INTO automation_enrollment_grants (
    id, client_id, code_hash, code_prefix, redirect_uri, scopes_json,
    code_challenge, code_challenge_method, expires_at, token_expires_at,
    consumed_at, created_at
) VALUES (?, ?, ?, ?, NULL, '["catalog:read"]', ?, 'S256', ?, ?, ?, ?)""",
        (
            code_id,
            client_id,
            f"code-hash-{code_id}",
            f"code-{code_id[-4:]}",
            f"challenge-{code_id}",
            PAST if expired else "2030-01-01T00:00:00+00:00",
            PAST if expired else "2030-01-01T00:00:00+00:00",
            PAST if consumed else None,
            PAST,
        ),
    )


def _seed_predecessor(path: Path) -> dict[str, str]:
    _upgrade(path, PREDECESSOR_REVISION)
    media_path = path.parent / "media" / "source-audio.bin"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"\x00synthetic preserved audio\xff\x10")
    media_bytes = media_path.read_bytes()

    with _connect(path) as connection:
        owner_rows = connection.execute(
            "SELECT id FROM users WHERE legacy_subject = 'owner'"
        ).fetchall()
        assert len(owner_rows) == 1
        initial_owner = str(owner_rows[0]["id"])
        _insert_user(connection, IDS["named_user"], name="Named user")
        _insert_user(
            connection,
            IDS["inactive_user"],
            name="Inactive historical user",
            is_active=False,
        )
        _insert_user(
            connection,
            IDS["no_workspace_user"],
            name="User without personal workspace",
        )
        _insert_user(
            connection,
            IDS["no_owner_membership_user"],
            name="Personal workspace without owner membership",
        )
        connection.execute(
            """INSERT INTO user_credentials (
    user_id, login_name, password_hash, auth_revision, created_at, updated_at
) VALUES (?, ?, NULL, 7, ?, ?)""",
            (IDS["inactive_user"], "historical-inactive", NOW, NOW),
        )

        _insert_workspace(
            connection,
            IDS["named_workspace"],
            name="Named personal workspace",
            kind="personal",
            owner_user_id=IDS["named_user"],
        )
        _insert_membership(connection, IDS["named_workspace"], IDS["named_user"], "owner")
        _insert_workspace(
            connection,
            IDS["inactive_workspace"],
            name="Archived personal workspace",
            kind="personal",
            owner_user_id=IDS["inactive_user"],
            state="archived",
        )
        _insert_membership(
            connection,
            IDS["inactive_workspace"],
            IDS["inactive_user"],
            "owner",
        )
        _insert_workspace(
            connection,
            IDS["no_owner_membership_workspace"],
            name="Personal workspace missing owner membership",
            kind="personal",
            owner_user_id=IDS["no_owner_membership_user"],
        )
        _insert_workspace(
            connection,
            IDS["team_workspace"],
            name="Named user team workspace",
            kind="team",
            owner_user_id=None,
        )
        _insert_membership(connection, IDS["team_workspace"], IDS["named_user"], "owner")
        _insert_membership(connection, IDS["team_workspace"], initial_owner, "member")
        _insert_workspace(
            connection,
            IDS["team_workspace_delete"],
            name="Empty team workspace deletion target",
            kind="team",
            owner_user_id=None,
        )
        _insert_workspace(
            connection,
            IDS["team_workspace_keep"],
            name="Empty team workspace retained target",
            kind="team",
            owner_user_id=None,
        )
        _insert_workspace(
            connection,
            IDS["project_host_workspace"],
            name="Empty project host workspace",
            kind="team",
            owner_user_id=None,
        )
        for project_id, name in (
            (IDS["project_delete"], "Empty project deletion target"),
            (IDS["project_keep"], "Empty project retained target"),
        ):
            connection.execute(
                """INSERT INTO projects (
    id, workspace_id, source_session_id, name, created_by_user_id, revision,
    created_at, updated_at
) VALUES (?, ?, NULL, ?, ?, 1, ?, ?)""",
                (
                    project_id,
                    IDS["project_host_workspace"],
                    name,
                    initial_owner,
                    NOW,
                    NOW,
                ),
            )
        connection.execute(
            """INSERT INTO artifacts (
    id, workspace_id, project_id, session_id, kind, role, relative_path,
    mime_type, size_bytes, content_hash, state, settings_hash, metadata_json,
    created_at, updated_at
) VALUES (?, ?, NULL, NULL, 'audio', 'source.audio', ?, 'audio/wav', ?, ?,
          'current', NULL, '{}', ?, ?)""",
            (
                "98000000-0000-4000-8000-000000000001",
                IDS["project_host_workspace"],
                "media/source-audio.bin",
                len(media_bytes),
                hashlib.sha256(media_bytes).hexdigest(),
                NOW,
                NOW,
            ),
        )

        _insert_api_token(
            connection,
            IDS["token_named"],
            label="Named token",
            subject="named-subject",
            user_id=IDS["named_user"],
            user_auth_revision=1,
        )
        _insert_api_token(
            connection,
            IDS["token_inactive"],
            label="Expired revoked historical token",
            subject="inactive-subject",
            user_id=IDS["inactive_user"],
            user_auth_revision=1,
            expired=True,
            revoked=True,
        )
        _insert_api_token(
            connection,
            IDS["token_no_workspace"],
            label="No workspace token",
            subject="no-workspace-subject",
            user_id=IDS["no_workspace_user"],
            user_auth_revision=1,
        )
        _insert_api_token(
            connection,
            IDS["token_no_owner_membership"],
            label="No owner membership token",
            subject="missing-membership-subject",
            user_id=IDS["no_owner_membership_user"],
            user_auth_revision=1,
        )
        _insert_api_token(
            connection,
            IDS["token_unbound_owner_subject"],
            label="Unbound token with owner-like subject",
            subject="owner",
            user_id=None,
            user_auth_revision=None,
        )
        _insert_api_token(
            connection,
            IDS["token_initial_owner"],
            label="Initial owner token",
            subject="owner",
            user_id=initial_owner,
            user_auth_revision=1,
        )

        _insert_client(
            connection,
            IDS["client_named"],
            user_id=IDS["named_user"],
            user_auth_revision=1,
        )
        _insert_client(
            connection,
            IDS["client_inactive"],
            user_id=IDS["inactive_user"],
            user_auth_revision=1,
            expired=True,
            revoked=True,
        )
        _insert_client(
            connection,
            IDS["client_no_workspace"],
            user_id=IDS["no_workspace_user"],
            user_auth_revision=1,
        )
        _insert_client(
            connection,
            IDS["client_no_owner_membership"],
            user_id=IDS["no_owner_membership_user"],
            user_auth_revision=1,
        )
        _insert_client(
            connection,
            IDS["client_unbound_owner_creator"],
            user_id=None,
            user_auth_revision=None,
            created_by="owner",
        )
        _insert_client(
            connection,
            IDS["client_initial_owner"],
            user_id=initial_owner,
            user_auth_revision=1,
        )
        for code_id, client_key in (
            (IDS["code_named"], "client_named"),
            (IDS["code_inactive"], "client_inactive"),
            (IDS["code_no_workspace"], "client_no_workspace"),
            (IDS["code_unbound"], "client_unbound_owner_creator"),
            (IDS["code_initial_owner"], "client_initial_owner"),
        ):
            _insert_code(
                connection,
                code_id,
                client_id=IDS[client_key],
                expired=True,
                consumed=True,
            )

        _insert_browser_session(
            connection,
            IDS["browser_interactive"],
            user_id=IDS["named_user"],
            principal_kind="owner_session",
            client_id=None,
        )
        _insert_browser_session(
            connection,
            IDS["browser_manager"],
            user_id=initial_owner,
            principal_kind="owner_session",
            client_id="pandrator-manager-browser",
        )
        _insert_browser_session(
            connection,
            IDS["browser_mcp_local"],
            user_id=initial_owner,
            principal_kind="owner_session",
            client_id="pandrator-mcp-local",
        )
        _insert_browser_session(
            connection,
            IDS["browser_named_bound"],
            user_id=IDS["named_user"],
            principal_kind="automation_client",
            client_id=IDS["client_named"],
        )
        _insert_browser_session(
            connection,
            IDS["browser_inactive"],
            user_id=IDS["inactive_user"],
            principal_kind="api_token",
            client_id=None,
        )

    return {
        "initial_owner": initial_owner,
        "media_path": str(media_path),
        **IDS,
    }


def _sqlite_snapshot(path: Path, *, omit_owned_schema: bool = False) -> dict[str, Any]:
    with _connect(path, foreign_keys=False) as connection:
        schema_rows = connection.execute(
            """SELECT type, name, tbl_name, sql
FROM sqlite_master
WHERE name NOT LIKE 'sqlite_%'
ORDER BY type, name"""
        ).fetchall()
        schema = tuple(
            tuple(row)
            for row in schema_rows
            if not omit_owned_schema or row["name"] not in OWNED_SCHEMA_NAMES
        )
        table_names = [
            str(row["name"])
            for row in schema_rows
            if row["type"] == "table"
            and row["name"] != "alembic_version"
            and (not omit_owned_schema or row["name"] not in OWNED_SCHEMA_NAMES)
        ]
        rows_by_table: dict[str, tuple[tuple[Any, ...], ...]] = {}
        for table_name in sorted(table_names):
            escaped_table = table_name.replace('"', '""')
            rows = connection.execute(f'SELECT * FROM "{escaped_table}" ORDER BY rowid').fetchall()
            rows_by_table[table_name] = tuple(tuple(row) for row in rows)
        return {"schema": schema, "rows": rows_by_table}


def _version(path: Path) -> str:
    with _connect(path, foreign_keys=False) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None
        return str(row[0])


def _rows(path: Path, table: str, columns: tuple[str, ...]) -> tuple[tuple[Any, ...], ...]:
    selection = ", ".join(f'"{column}"' for column in columns)
    with _connect(path, foreign_keys=False) as connection:
        result = connection.execute(f'SELECT {selection} FROM "{table}" ORDER BY 1, 2').fetchall()
        return tuple(tuple(row) for row in result)


def _foreign_key_shapes(connection: sqlite3.Connection, table_name: str) -> set[tuple[Any, ...]]:
    quoted_table = table_name.replace('"', '""')
    rows = connection.execute(f'PRAGMA foreign_key_list("{quoted_table}")').fetchall()
    groups: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(int(row["id"]), []).append(row)
    return {
        (
            tuple(str(row["from"]) for row in sorted(group, key=lambda row: row["seq"])),
            str(group[0]["table"]),
            tuple(str(row["to"]) for row in sorted(group, key=lambda row: row["seq"])),
            str(group[0]["on_delete"]).upper(),
        )
        for group in groups.values()
    }


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _model_fk_shapes(model: type[Any]) -> set[tuple[Any, ...]]:
    return {
        (
            tuple(constraint.columns.keys()),
            next(iter(constraint.elements)).target_fullname.rsplit(".", 1)[0],
            (next(iter(constraint.elements)).target_fullname.rsplit(".", 1)[1],),
            str(constraint.ondelete).upper(),
            constraint.name,
        )
        for constraint in model.__table__.foreign_key_constraints
    }


def _expected_owner_ids(ids: dict[str, str], owner_table: str) -> tuple[str, str]:
    return {
        "api_tokens": (ids["token_named"], ids["token_initial_owner"]),
        "automation_clients": (ids["client_named"], ids["client_initial_owner"]),
        "browser_auth_sessions": (ids["browser_manager"], ids["browser_mcp_local"]),
        "automation_enrollment_grants": (ids["code_named"], ids["code_initial_owner"]),
    }[owner_table]


def _insert_grant(
    path: Path,
    table_name: str,
    owner_column: str,
    owner_id: str | None,
    target_column: str,
    target_id: str | None,
) -> None:
    with _connect(path) as connection:
        connection.execute(
            f'INSERT INTO "{table_name}" ("{owner_column}", "{target_column}") VALUES (?, ?)',
            (owner_id, target_id),
        )


def test_model_metadata_matches_migrated_eight_table_schema(tmp_path: Path) -> None:
    path = tmp_path / "credential-grant-metadata.sqlite3"
    _seed_predecessor(path)
    _upgrade(path, TARGET_REVISION)

    with _connect(path, foreign_keys=False) as connection:
        for model, table_name, owner_column, owner_table, target_column, target_table in GRANTS:
            table = model.__table__
            assert table.name == table_name
            assert tuple(table.columns.keys()) == (owner_column, target_column)
            assert tuple(table.primary_key.columns.keys()) == (owner_column, target_column)
            assert _model_fk_shapes(model) == {
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
            columns = list(table.columns)
            assert all(column.type.length == 36 for column in columns)
            assert all(not column.nullable for column in columns)
            assert all(
                column.default is None and column.server_default is None for column in columns
            )
            assert {
                (
                    index.name,
                    tuple(column.name for column in index.columns),
                    bool(index.unique),
                )
                for index in table.indexes
            } == {(f"ix_{table_name}_{target_column}", (target_column,), False)}

            quoted_table = table_name.replace('"', '""')
            db_columns = connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
            assert tuple(str(row["name"]) for row in db_columns) == (
                owner_column,
                target_column,
            )
            assert tuple(int(row["pk"]) for row in db_columns) == (1, 2)
            assert all(str(row["type"]).upper() == "VARCHAR(36)" for row in db_columns)
            assert all(bool(row["notnull"]) and row["dflt_value"] is None for row in db_columns)
            assert _foreign_key_shapes(connection, table_name) == {
                ((owner_column,), owner_table, ("id",), "CASCADE"),
                ((target_column,), target_table, ("id",), "CASCADE"),
            }
            table_sql = _table_sql(connection, table_name)
            assert f"CONSTRAINT fk_{table_name}_{owner_column}" in table_sql
            assert f"CONSTRAINT fk_{table_name}_{target_column}" in table_sql
            index_name = f"ix_{table_name}_{target_column}"
            index = connection.execute(
                "SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            assert index is not None and index["tbl_name"] == table_name
            quoted_index = index_name.replace('"', '""')
            index_columns = connection.execute(f'PRAGMA index_info("{quoted_index}")').fetchall()
            assert tuple(str(row["name"]) for row in index_columns) == (target_column,)


def test_upgrade_backfills_only_proven_personal_workspace_upper_bounds_and_preserves_sources(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential-grant-backfill.sqlite3"
    ids = _seed_predecessor(path)
    before = _sqlite_snapshot(path, omit_owned_schema=True)
    media_path = Path(ids["media_path"])
    media_before = media_path.read_bytes()

    _upgrade(path, TARGET_REVISION)

    assert _version(path) == TARGET_REVISION
    assert set(
        _rows(
            path,
            "api_token_workspace_grants",
            ("token_id", "workspace_id"),
        )
    ) == {
        (ids["token_inactive"], ids["inactive_workspace"]),
        (ids["token_initial_owner"], _initial_workspace(path, ids["initial_owner"])),
        (ids["token_named"], ids["named_workspace"]),
    }
    assert set(
        _rows(
            path,
            "automation_client_workspace_grants",
            ("client_id", "workspace_id"),
        )
    ) == {
        (ids["client_inactive"], ids["inactive_workspace"]),
        (ids["client_initial_owner"], _initial_workspace(path, ids["initial_owner"])),
        (ids["client_named"], ids["named_workspace"]),
    }
    assert set(
        _rows(
            path,
            "browser_auth_workspace_grants",
            ("browser_session_id", "workspace_id"),
        )
    ) == {
        (ids["browser_inactive"], ids["inactive_workspace"]),
        (ids["browser_manager"], _initial_workspace(path, ids["initial_owner"])),
        (ids["browser_mcp_local"], _initial_workspace(path, ids["initial_owner"])),
        (ids["browser_named_bound"], ids["named_workspace"]),
    }
    assert set(
        _rows(
            path,
            "automation_code_workspace_grants",
            ("code_grant_id", "workspace_id"),
        )
    ) == {
        (ids["code_inactive"], ids["inactive_workspace"]),
        (ids["code_initial_owner"], _initial_workspace(path, ids["initial_owner"])),
        (ids["code_named"], ids["named_workspace"]),
    }
    for _model, table_name, *_ in GRANTS:
        if table_name.endswith("_project_grants"):
            assert _rows(path, table_name, (GRANTS_BY_TABLE[table_name][0], "project_id")) == ()

    with _connect(path, foreign_keys=False) as connection:
        interactive = connection.execute(
            "SELECT 1 FROM browser_auth_workspace_grants WHERE browser_session_id=?",
            (ids["browser_interactive"],),
        ).fetchone()
        assert interactive is None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM automation_clients "
                "WHERE id IN ('pandrator-manager-browser', 'pandrator-mcp-local')"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM api_token_workspace_grants WHERE token_id=?",
                (ids["token_unbound_owner_subject"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM automation_client_workspace_grants WHERE client_id=?",
                (ids["client_unbound_owner_creator"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM automation_code_workspace_grants WHERE code_grant_id=?",
                (ids["code_unbound"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_memberships WHERE workspace_id=?",
                (ids["team_workspace"],),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM browser_auth_sessions "
                "WHERE id IN (?, ?) AND client_id IN (?, ?)",
                (
                    ids["browser_manager"],
                    ids["browser_mcp_local"],
                    "pandrator-manager-browser",
                    "pandrator-mcp-local",
                ),
            ).fetchone()[0]
            == 2
        )

    assert _sqlite_snapshot(path, omit_owned_schema=True) == before
    assert media_path.read_bytes() == media_before


def _initial_workspace(path: Path, owner_id: str) -> str:
    with _connect(path, foreign_keys=False) as connection:
        row = connection.execute(
            "SELECT id FROM workspaces WHERE kind='personal' AND personal_owner_user_id=?",
            (owner_id,),
        ).fetchone()
        assert row is not None
        return str(row[0])


GRANTS_BY_TABLE = {
    table_name: (owner_column, owner_table, target_column, target_table)
    for _model, table_name, owner_column, owner_table, target_column, target_table in GRANTS
}


@pytest.mark.parametrize("grant", GRANTS, ids=[entry[1] for entry in GRANTS])
def test_each_grant_relation_enforces_pk_owner_and_target_cascades(
    tmp_path: Path,
    grant: tuple[type[Any], str, str, str, str, str],
) -> None:
    _model, table_name, owner_column, owner_table, target_column, target_table = grant
    path = tmp_path / f"{table_name}.sqlite3"
    ids = _seed_predecessor(path)
    _upgrade(path, TARGET_REVISION)
    owner_one, owner_two = _expected_owner_ids(ids, owner_table)
    target_one = (
        ids["team_workspace_delete"] if target_table == "workspaces" else ids["project_delete"]
    )
    target_two = ids["team_workspace_keep"] if target_table == "workspaces" else ids["project_keep"]
    retained_before = set(_rows(path, table_name, (owner_column, target_column)))

    _insert_grant(path, table_name, owner_column, owner_one, target_column, target_one)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, owner_one, target_column, target_one)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, "missing-owner-id", target_column, target_one)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, owner_one, target_column, "missing-target-id")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, "", target_column, target_one)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, owner_one, target_column, "")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, None, target_column, target_one)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(path, table_name, owner_column, owner_one, target_column, None)

    _insert_grant(path, table_name, owner_column, owner_two, target_column, target_two)
    with _connect(path) as connection:
        connection.execute(f'DELETE FROM "{target_table}" WHERE id=?', (target_one,))
        remaining_after_target_delete = connection.execute(
            f'SELECT "{owner_column}", "{target_column}" FROM "{table_name}"'
        ).fetchall()
        assert {tuple(row) for row in remaining_after_target_delete} == retained_before | {
            (owner_two, target_two)
        }

        connection.execute(f'DELETE FROM "{owner_table}" WHERE id=?', (owner_two,))
        remaining = connection.execute(
            f'SELECT "{owner_column}", "{target_column}" FROM "{table_name}"'
        ).fetchall()
        assert {tuple(row) for row in remaining} == {
            row for row in retained_before if row[0] != owner_two
        }


@pytest.mark.parametrize(
    ("corruption", "expected_message"),
    [
        ("missing_table", "required tables are missing"),
        ("missing_column", "is missing required columns"),
        ("wrong_fk_action", "unexpected foreign-key target or action"),
        ("partial_user_pair", "illegal user_id/user_auth_revision pair"),
        ("zero_user_revision", "illegal user_id/user_auth_revision pair"),
        ("negative_user_revision", "illegal user_id/user_auth_revision pair"),
        ("browser_nonpositive_revision", "nonpositive auth revision"),
        ("foreign_key_corruption", "existing foreign-key integrity failed"),
        ("owned_name_collision", "owned schema name"),
    ],
)
def test_upgrade_preflight_failure_leaves_all_other_objects_unchanged(
    tmp_path: Path,
    corruption: str,
    expected_message: str,
) -> None:
    path = tmp_path / f"preflight-{corruption}.sqlite3"
    _seed_predecessor(path)
    with _connect(path, foreign_keys=False) as connection:
        if corruption == "missing_table":
            connection.execute("DROP TABLE automation_enrollment_grants")
        elif corruption == "missing_column":
            connection.execute("ALTER TABLE browser_auth_sessions DROP COLUMN client_id")
        elif corruption == "wrong_fk_action":
            connection.execute("DROP TABLE api_tokens")
            connection.execute(
                """CREATE TABLE api_tokens (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36),
    user_auth_revision INTEGER,
    CONSTRAINT fk_api_tokens_user_id_users FOREIGN KEY (user_id)
      REFERENCES users (id) ON DELETE CASCADE
)"""
            )
        elif corruption == "partial_user_pair":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            _insert_api_token(
                connection,
                IDS["token_bad_pair"],
                label="Partial pair",
                subject="owner",
                user_id=IDS["named_user"],
                user_auth_revision=None,
            )
        elif corruption in {"zero_user_revision", "negative_user_revision"}:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            _insert_api_token(
                connection,
                IDS[
                    "token_bad_zero_revision"
                    if corruption == "zero_user_revision"
                    else "token_bad_negative_revision"
                ],
                label="Nonpositive revision",
                subject="named-subject",
                user_id=IDS["named_user"],
                user_auth_revision=0 if corruption == "zero_user_revision" else -1,
            )
        elif corruption == "browser_nonpositive_revision":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            _insert_browser_session(
                connection,
                IDS["browser_bad_revision"],
                user_id=IDS["named_user"],
                principal_kind="automation_client",
                client_id="unregistered-browser-client",
                auth_revision=0,
            )
        elif corruption == "foreign_key_corruption":
            _insert_api_token(
                connection,
                IDS["token_bad_fk"],
                label="Corrupt user reference",
                subject="unbound",
                user_id="missing-user-id",
                user_auth_revision=1,
            )
        elif corruption == "owned_name_collision":
            connection.execute("CREATE TABLE api_token_workspace_grants (reserved TEXT)")

    before = _sqlite_snapshot(path)
    with pytest.raises(RuntimeError, match=expected_message):
        command.upgrade(_config(path), TARGET_REVISION)
    assert _sqlite_snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION
    with _connect(path, foreign_keys=False) as connection:
        present_owned_names = (
            set(row[0] for row in connection.execute("SELECT name FROM sqlite_master"))
            & OWNED_SCHEMA_NAMES
        )
        expected_collision = (
            {"api_token_workspace_grants"} if corruption == "owned_name_collision" else set()
        )
        assert present_owned_names == expected_collision


def test_upgrade_mid_ddl_failure_rolls_back_tables_indexes_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.operations.base import Operations

    path = tmp_path / "credential-grant-ddl-rollback.sqlite3"
    _seed_predecessor(path)
    before = _sqlite_snapshot(path)
    original_create_index = Operations.create_index

    def fail_after_initial_objects(
        operations: Operations,
        index_name: str,
        table_name: str,
        columns: Any,
        **kwargs: Any,
    ) -> None:
        if index_name == "ix_automation_client_project_grants_project_id":
            raise RuntimeError("injected credential grant DDL failure")
        original_create_index(operations, index_name, table_name, columns, **kwargs)

    monkeypatch.setattr(Operations, "create_index", fail_after_initial_objects)
    with pytest.raises(RuntimeError, match="injected credential grant DDL failure"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _sqlite_snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_upgrade_mid_backfill_failure_rolls_back_partial_grants_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "credential-grant-backfill-rollback.sqlite3"
    _seed_predecessor(path)
    before = _sqlite_snapshot(path)
    original_exec_driver_sql = Connection.exec_driver_sql

    def fail_during_client_backfill(
        connection: Connection,
        statement: str,
        parameters: Any = (),
        execution_options: Any = None,
    ) -> Any:
        if statement.lstrip().startswith("INSERT INTO automation_client_workspace_grants"):
            raise RuntimeError("injected credential grant backfill failure")
        return original_exec_driver_sql(
            connection,
            statement,
            parameters,
            execution_options=execution_options,
        )

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_during_client_backfill)
    with pytest.raises(RuntimeError, match="injected credential grant backfill failure"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _sqlite_snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_upgrade_downgrade_upgrade_preserves_predecessor_state_and_recreates_grants(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential-grant-roundtrip.sqlite3"
    ids = _seed_predecessor(path)
    predecessor = _sqlite_snapshot(path)
    config = _upgrade(path, TARGET_REVISION)
    upgraded_grants = _sqlite_snapshot(path)
    expected_named = (ids["token_named"], ids["named_workspace"])
    assert expected_named in _rows(
        path,
        "api_token_workspace_grants",
        ("token_id", "workspace_id"),
    )

    command.downgrade(config, PREDECESSOR_REVISION)
    assert _version(path) == PREDECESSOR_REVISION
    assert _sqlite_snapshot(path) == predecessor

    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert _sqlite_snapshot(path) == upgraded_grants


@pytest.mark.parametrize(
    "drift",
    ["missing_index", "foreign_key_shape", "missing_table"],
    ids=["missing-index", "foreign-key-shape", "missing-table"],
)
def test_downgrade_drift_preflight_is_atomic(
    tmp_path: Path,
    drift: str,
) -> None:
    path = tmp_path / f"credential-grant-downgrade-{drift}.sqlite3"
    config = _upgrade(path, PREDECESSOR_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with _connect(path, foreign_keys=False) as connection:
        if drift == "missing_index":
            connection.execute("DROP INDEX ix_api_token_workspace_grants_workspace_id")
        elif drift == "missing_table":
            connection.execute("DROP TABLE api_token_workspace_grants")
        else:
            connection.execute("DROP TABLE api_token_project_grants")
            connection.execute(
                """CREATE TABLE api_token_project_grants (
    token_id VARCHAR(36) NOT NULL,
    project_id VARCHAR(36) NOT NULL,
    PRIMARY KEY (token_id, project_id)
)"""
            )
            connection.execute(
                "CREATE INDEX ix_api_token_project_grants_project_id "
                "ON api_token_project_grants (project_id)"
            )

    before = _sqlite_snapshot(path)
    with pytest.raises(RuntimeError, match="Cannot downgrade credential resource grants"):
        command.downgrade(config, PREDECESSOR_REVISION)
    assert _sqlite_snapshot(path) == before
    assert _version(path) == TARGET_REVISION


def test_reserved_manager_browser_client_ids_are_not_credential_clients(tmp_path: Path) -> None:
    path = tmp_path / "credential-grant-manager-browser.sqlite3"
    ids = _seed_predecessor(path)
    _upgrade(path, TARGET_REVISION)

    with _connect(path, foreign_keys=False) as connection:
        client_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM automation_clients WHERE id IN (?, ?)",
                ("pandrator-manager-browser", "pandrator-mcp-local"),
            )
        }
        assert client_ids == set()
        browser_clients = {
            str(row[0])
            for row in connection.execute(
                "SELECT client_id FROM browser_auth_sessions WHERE id IN (?, ?)",
                (ids["browser_manager"], ids["browser_mcp_local"]),
            )
        }
        assert browser_clients == {"pandrator-manager-browser", "pandrator-mcp-local"}
