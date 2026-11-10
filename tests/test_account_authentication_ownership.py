from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from flask import Flask, g
from flask.testing import FlaskClient
from sqlalchemy import Table, create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.auth_models import (
    BrowserAuthSession,
    PasswordResetGrant,
    UserCredential,
)
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import sqlite_url
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.models import Job, JobActorRecord, JobEvent
from tests.web_test_support import prepare_web_test_data_root

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0073_account_authentication"
PREVIOUS_REVISION = "0072_workspace_settings"
NOW = "2026-10-23 13:20:00.123456"
UPDATED = "2026-10-23 13:21:01.654321"
LEGACY_HASH = "fixture-encoded-password-hash"


@pytest.fixture(scope="session")
def schema_at_0072(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("account-auth-schema-base") / "at-0072.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    # Bootstrap uses current metadata, including this table added by 0087.
    # It did not exist at 0072 and must not create an inbound auth reference.
    with sqlite3.connect(path) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_link_sessions'"
        ).fetchone():
            assert connection.execute("SELECT COUNT(*) FROM project_link_sessions").fetchone() == (0,)
            connection.execute("DROP TABLE project_link_sessions")
    return path


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _owner_id(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT id FROM users WHERE legacy_subject = 'owner'").fetchone()
    assert row is not None
    return str(row[0])


def _legacy_database(
    schema_at_0072: Path,
    path: Path,
    *,
    include_owner_account: bool = True,
    source_rows: bool = True,
    custom_indexes: bool = False,
) -> tuple[str, tuple[Any, ...], tuple[Any, ...]]:
    shutil.copy2(schema_at_0072, path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        owner_id = _owner_id(connection)
        connection.execute(
            "UPDATE users SET created_at = ?, updated_at = ? WHERE id = ?",
            (NOW, UPDATED, owner_id),
        )
        if include_owner_account:
            connection.execute(
                "INSERT INTO owner_account "
                "(singleton_id, password_hash, created_at, updated_at) VALUES (1, ?, ?, ?)",
                (LEGACY_HASH, "2026-10-20 08:30:00.123456", "2026-10-21 09:31:00.654321"),
            )
        connection.execute("DROP TABLE api_tokens")
        connection.execute("DROP TABLE automation_clients")
        connection.execute(
            """CREATE TABLE automation_clients (
                id VARCHAR(36) NOT NULL,
                name VARCHAR(160) NOT NULL,
                subject VARCHAR(200) NOT NULL UNIQUE,
                redirect_uris_json JSON NOT NULL,
                allowed_scopes_json JSON NOT NULL,
                target_instance_id VARCHAR(36) NOT NULL,
                canonical_origin TEXT NOT NULL,
                created_by VARCHAR(200) NOT NULL,
                created_at DATETIME NOT NULL,
                last_used_at DATETIME,
                revoked_at DATETIME,
                PRIMARY KEY (id)
            )"""
        )
        connection.execute(
            """CREATE TABLE api_tokens (
                id VARCHAR(36) NOT NULL,
                label VARCHAR(160) NOT NULL,
                token_hash VARCHAR(128) NOT NULL UNIQUE,
                token_prefix VARCHAR(16) NOT NULL,
                subject VARCHAR(200),
                scopes_json JSON NOT NULL,
                expires_at DATETIME,
                principal_kind VARCHAR(40) NOT NULL,
                created_by VARCHAR(200),
                client_id VARCHAR(36),
                target_instance_id VARCHAR(36),
                canonical_origin TEXT,
                created_at DATETIME NOT NULL,
                last_used_at DATETIME,
                revoked_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY (client_id) REFERENCES automation_clients (id) ON DELETE SET NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX ix_automation_clients_revoked_at ON automation_clients (revoked_at)"
        )
        for index_name, column in (
            ("ix_api_tokens_token_prefix", "token_prefix"),
            ("ix_api_tokens_subject", "subject"),
            ("ix_api_tokens_expires_at", "expires_at"),
            ("ix_api_tokens_client_id", "client_id"),
        ):
            connection.execute(f"CREATE INDEX {index_name} ON api_tokens ({column})")
        if source_rows:
            connection.execute(
                "INSERT INTO automation_clients "
                "(id, name, subject, redirect_uris_json, allowed_scopes_json, "
                "target_instance_id, canonical_origin, created_by, created_at, "
                "last_used_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "client-legacy",
                    "Legacy client",
                    "legacy-client-subject",
                    '["http://127.0.0.1/callback"]',
                    '["app.read", "app.write"]',
                    "instance-legacy",
                    "http://127.0.0.1:8765",
                    "owner",
                    NOW,
                    UPDATED,
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO api_tokens "
                "(id, label, token_hash, token_prefix, subject, scopes_json, expires_at, "
                "principal_kind, created_by, client_id, target_instance_id, canonical_origin, "
                "created_at, last_used_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "token-legacy",
                    "Legacy token",
                    "opaque-existing-token-digest",
                    "pan_fixture",
                    "legacy-subject",
                    '["app.read", "app.write"]',
                    "2027-10-23 13:20:00",
                    "api_token",
                    "owner",
                    "client-legacy",
                    "instance-legacy",
                    "http://127.0.0.1:8765",
                    NOW,
                    UPDATED,
                    UPDATED,
                ),
            )
        if custom_indexes:
            connection.execute(
                "CREATE INDEX custom_api_tokens_label_desc ON api_tokens (label DESC, token_prefix)"
            )
            connection.execute(
                "CREATE INDEX custom_api_tokens_subject_fold "
                "ON api_tokens (lower(subject)) WHERE subject IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX custom_automation_clients_created_desc "
                "ON automation_clients (created_at DESC)"
            )
        token_rows = tuple(
            connection.execute(
                "SELECT id, label, token_hash, token_prefix, subject, scopes_json, expires_at, "
                "principal_kind, created_by, client_id, target_instance_id, canonical_origin, "
                "created_at, last_used_at, revoked_at FROM api_tokens ORDER BY id"
            ).fetchall()
        )
        client_rows = tuple(
            connection.execute(
                "SELECT id, name, subject, redirect_uris_json, allowed_scopes_json, "
                "target_instance_id, canonical_origin, created_by, created_at, last_used_at, "
                "revoked_at FROM automation_clients ORDER BY id"
            ).fetchall()
        )
    return owner_id, token_rows, client_rows


def _index_sql(path: Path, table_name: str) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = ? AND sql IS NOT NULL ORDER BY name",
            (table_name,),
        ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _unique_constraint_signatures(path: Path, table_name: str) -> tuple[tuple[str, ...], ...]:
    quoted_name = table_name.replace('"', '""')
    with sqlite3.connect(path) as connection:
        indexes = connection.execute(f'PRAGMA index_list("{quoted_name}")').fetchall()
        signatures = []
        for index in indexes:
            if not bool(index[2]) or str(index[3]) != "u":
                continue
            quoted_index = str(index[1]).replace('"', '""')
            columns = connection.execute(f'PRAGMA index_info("{quoted_index}")').fetchall()
            signatures.append(tuple(str(column[2]) for column in columns))
    return tuple(sorted(signatures))


def _foreign_key_signatures(path: Path, table_name: str) -> tuple[tuple[Any, ...], ...]:
    quoted_name = table_name.replace('"', '""')
    with sqlite3.connect(path) as connection:
        return tuple(connection.execute(f'PRAGMA foreign_key_list("{quoted_name}")').fetchall())


def _trigger_sql(path: Path, table_name: str) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = ? ORDER BY name",
            (table_name,),
        ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _database_snapshot(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents = tuple(
            (
                table_name,
                tuple(
                    connection.execute(
                        f'SELECT * FROM "{table_name.replace(chr(34), chr(34) * 2)}" ORDER BY rowid'
                    ).fetchall()
                ),
            )
            for table_name in table_names
        )
    return objects, contents


def _create_early_auth_tables(path: Path) -> None:
    engine = create_engine(sqlite_url(path))
    try:
        cast(Table, UserCredential.__table__).create(engine)
        cast(Table, BrowserAuthSession.__table__).create(engine)
        cast(Table, PasswordResetGrant.__table__).create(engine)
    finally:
        engine.dispose()


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def test_historical_upgrade_preserves_legacy_rows_indexes_and_exact_owner_metadata(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "historical.sqlite"
    owner_id, token_rows, client_rows = _legacy_database(schema_at_0072, path, custom_indexes=True)
    old_token_indexes = _index_sql(path, "api_tokens")
    old_client_indexes = _index_sql(path, "automation_clients")

    command.upgrade(_config(path), REVISION)
    command.upgrade(_config(path), REVISION)

    assert _version(path) == REVISION
    with sqlite3.connect(path) as connection:
        credential = connection.execute(
            "SELECT user_id, login_name, password_hash, auth_revision, created_at, updated_at "
            "FROM user_credentials"
        ).fetchone()
        assert credential == (
            owner_id,
            "owner",
            LEGACY_HASH,
            1,
            "2026-10-20 08:30:00.123456",
            "2026-10-21 09:31:00.654321",
        )
        assert connection.execute(
            "SELECT user_id, user_auth_revision FROM api_tokens WHERE id='token-legacy'"
        ).fetchone() == (owner_id, 1)
        assert connection.execute(
            "SELECT user_id, user_auth_revision FROM automation_clients WHERE id='client-legacy'"
        ).fetchone() == (owner_id, 1)
        assert connection.execute(
            "SELECT id, label, token_hash, token_prefix, subject, scopes_json, expires_at, "
            "principal_kind, created_by, client_id, target_instance_id, canonical_origin, "
            "created_at, last_used_at, revoked_at FROM api_tokens ORDER BY id"
        ).fetchall() == list(token_rows)
        assert connection.execute(
            "SELECT id, name, subject, redirect_uris_json, allowed_scopes_json, "
            "target_instance_id, canonical_origin, created_by, created_at, last_used_at, "
            "revoked_at FROM automation_clients ORDER BY id"
        ).fetchall() == list(client_rows)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE type='table' AND name IN "
            "('user_credentials', 'browser_auth_sessions', 'password_reset_grants') "
            "ORDER BY name"
        ).fetchall() == [("table",), ("table",), ("table",)]
    assert {
        name: sql
        for name, sql in _index_sql(path, "api_tokens").items()
        if name != "ix_api_tokens_user_id"
    } == old_token_indexes
    assert {
        name: sql
        for name, sql in _index_sql(path, "automation_clients").items()
        if name != "ix_automation_clients_user_id"
    } == old_client_indexes


def test_upgrade_without_legacy_owner_account_uses_initial_user_timestamps(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "no-owner-password.sqlite"
    owner_id, _, _ = _legacy_database(
        schema_at_0072, path, include_owner_account=False, source_rows=False
    )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT user_id, login_name, password_hash, auth_revision, created_at, updated_at "
            "FROM user_credentials"
        ).fetchone() == (owner_id, "owner", None, 1, NOW, UPDATED)


def test_canonical_empty_early_auth_tables_are_adopted_without_replacement(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-empty.sqlite"
    owner_id, _, _ = _legacy_database(schema_at_0072, path, source_rows=False)
    _create_early_auth_tables(path)
    with sqlite3.connect(path) as connection:
        old_sql = {
            str(name): str(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name IN "
                "('user_credentials', 'browser_auth_sessions', 'password_reset_grants', "
                "'ix_browser_auth_sessions_user_id', 'ix_password_reset_grants_user_id') "
                "ORDER BY name"
            )
        }
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        new_sql = {
            str(name): str(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name IN "
                "('user_credentials', 'browser_auth_sessions', 'password_reset_grants', "
                "'ix_browser_auth_sessions_user_id', 'ix_password_reset_grants_user_id') "
                "ORDER BY name"
            )
        }
        assert new_sql == old_sql
        assert connection.execute(
            "SELECT user_id, login_name FROM user_credentials"
        ).fetchall() == [(owner_id, "owner")]


@pytest.mark.parametrize("target_state", ["partial", "prepopulated", "malformed_index"])
def test_early_auth_targets_must_be_complete_canonical_and_empty(
    schema_at_0072: Path, tmp_path: Path, target_state: str
) -> None:
    path = tmp_path / f"early-{target_state}.sqlite"
    owner_id, _, _ = _legacy_database(schema_at_0072, path, source_rows=False)
    if target_state == "partial":
        engine = create_engine(sqlite_url(path))
        try:
            cast(Table, UserCredential.__table__).create(engine)
        finally:
            engine.dispose()
    else:
        _create_early_auth_tables(path)
        with sqlite3.connect(path) as connection:
            if target_state == "prepopulated":
                connection.execute(
                    "INSERT INTO user_credentials "
                    "(user_id, login_name, password_hash, auth_revision, created_at, updated_at) "
                    "VALUES (?, 'owner', NULL, 1, ?, ?)",
                    (owner_id, NOW, UPDATED),
                )
            else:
                connection.execute("DROP INDEX ix_browser_auth_sessions_user_id")
                connection.execute(
                    "CREATE INDEX custom_browser_user ON browser_auth_sessions(user_id)"
                )
    before = _database_snapshot(path)
    with pytest.raises((RuntimeError, SQLAlchemyError)):
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == before
    assert _version(path) == PREVIOUS_REVISION


@pytest.mark.parametrize("bad_state", ["missing_owner", "bad_singleton", "prebound_token"])
def test_source_conflicts_fail_before_ddl_with_exact_recovery(
    schema_at_0072: Path, tmp_path: Path, bad_state: str
) -> None:
    path = tmp_path / f"bad-source-{bad_state}.sqlite"
    owner_id, _, _ = _legacy_database(schema_at_0072, path)
    with sqlite3.connect(path) as connection:
        if bad_state == "missing_owner":
            connection.execute("UPDATE users SET legacy_subject = NULL WHERE id = ?", (owner_id,))
        elif bad_state == "bad_singleton":
            connection.execute("UPDATE owner_account SET singleton_id = 2")
        else:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN user_id VARCHAR(36)")
    before = _database_snapshot(path)
    with pytest.raises((RuntimeError, SQLAlchemyError)):
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == before
    assert _version(path) == PREVIOUS_REVISION


def test_missing_legacy_owner_table_refuses_atomically_and_retries_after_restore(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "missing-owner-account-table.sqlite"
    _legacy_database(schema_at_0072, path)
    source_snapshot = _database_snapshot(path)
    with sqlite3.connect(path) as connection:
        owner_table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'owner_account'"
        ).fetchone()
        owner_row = connection.execute(
            "SELECT singleton_id, password_hash, created_at, updated_at FROM owner_account"
        ).fetchone()
        assert owner_table_sql_row is not None
        assert owner_row is not None
        owner_table_sql = str(owner_table_sql_row[0])
        connection.execute("DROP TABLE owner_account")

    missing_table_snapshot = _database_snapshot(path)
    with pytest.raises(RuntimeError, match="required identity or credential tables"):
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == missing_table_snapshot
    assert _version(path) == PREVIOUS_REVISION

    with sqlite3.connect(path) as connection:
        connection.execute(owner_table_sql)
        connection.execute(
            "INSERT INTO owner_account "
            "(singleton_id, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
            owner_row,
        )
    assert _database_snapshot(path) == source_snapshot

    command.upgrade(_config(path), REVISION)
    assert _version(path) == REVISION
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT singleton_id, password_hash, created_at, updated_at FROM owner_account"
            ).fetchone()
            == owner_row
        )


def test_late_alembic_version_failure_rolls_back_all_ddl_and_data(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "late-failure.sqlite"
    _legacy_database(schema_at_0072, path, custom_indexes=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_auth_version_update BEFORE UPDATE OF version_num "
            "ON alembic_version BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END"
        )
    before = _database_snapshot(path)
    with pytest.raises(SQLAlchemyError):
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == before
    assert _version(path) == PREVIOUS_REVISION


def test_downgrade_restores_exact_initial_password_and_source_indexes(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "downgrade.sqlite"
    _, original_token_rows, original_client_rows = _legacy_database(
        schema_at_0072, path, custom_indexes=True
    )
    old_token_indexes = _index_sql(path, "api_tokens")
    old_client_indexes = _index_sql(path, "automation_clients")
    old_token_unique = _unique_constraint_signatures(path, "api_tokens")
    old_client_unique = _unique_constraint_signatures(path, "automation_clients")
    old_token_fks = _foreign_key_signatures(path, "api_tokens")
    old_client_fks = _foreign_key_signatures(path, "automation_clients")
    assert old_token_unique == (("token_hash",),)
    assert old_client_unique == (("subject",),)
    command.upgrade(_config(path), REVISION)
    assert _unique_constraint_signatures(path, "api_tokens") == old_token_unique
    assert _unique_constraint_signatures(path, "automation_clients") == old_client_unique
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE user_credentials SET password_hash = ?, created_at = ?, updated_at = ?",
            (
                "fixture-rehashed-password",
                "2026-10-22 10:11:12.123456",
                "2026-10-23 11:12:13.654321",
            ),
        )
    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _version(path) == PREVIOUS_REVISION
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT singleton_id, password_hash, created_at, updated_at FROM owner_account"
        ).fetchone() == (
            1,
            "fixture-rehashed-password",
            "2026-10-22 10:11:12.123456",
            "2026-10-23 11:12:13.654321",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row[1] for row in connection.execute("PRAGMA table_info(api_tokens)")} == {
            "id",
            "label",
            "token_hash",
            "token_prefix",
            "subject",
            "scopes_json",
            "expires_at",
            "principal_kind",
            "created_by",
            "client_id",
            "target_instance_id",
            "canonical_origin",
            "created_at",
            "last_used_at",
            "revoked_at",
        }
    assert _index_sql(path, "api_tokens") == old_token_indexes
    assert _index_sql(path, "automation_clients") == old_client_indexes
    assert _unique_constraint_signatures(path, "api_tokens") == old_token_unique
    assert _unique_constraint_signatures(path, "automation_clients") == old_client_unique
    assert _foreign_key_signatures(path, "api_tokens") == old_token_fks
    assert _foreign_key_signatures(path, "automation_clients") == old_client_fks
    with sqlite3.connect(path) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT id, label, token_hash, token_prefix, subject, scopes_json, expires_at, "
                    "principal_kind, created_by, client_id, target_instance_id, canonical_origin, "
                    "created_at, last_used_at, revoked_at FROM api_tokens ORDER BY id"
                ).fetchall()
            )
            == original_token_rows
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT id, name, subject, redirect_uris_json, allowed_scopes_json, "
                    "target_instance_id, canonical_origin, created_by, created_at, last_used_at, "
                    "revoked_at FROM automation_clients ORDER BY id"
                ).fetchall()
            )
            == original_client_rows
        )
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO api_tokens (id, label, token_hash, token_prefix, subject, "
                "scopes_json, expires_at, principal_kind, created_by, client_id, "
                "target_instance_id, canonical_origin, created_at, last_used_at, revoked_at) "
                "SELECT 'duplicate-token', label, token_hash, token_prefix, subject, "
                "scopes_json, expires_at, principal_kind, created_by, client_id, "
                "target_instance_id, canonical_origin, created_at, last_used_at, revoked_at "
                "FROM api_tokens WHERE id = 'token-legacy'"
            )
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO automation_clients (id, name, subject, redirect_uris_json, "
                "allowed_scopes_json, target_instance_id, canonical_origin, created_by, "
                "created_at, last_used_at, revoked_at) "
                "SELECT 'duplicate-client', name, subject, redirect_uris_json, "
                "allowed_scopes_json, target_instance_id, canonical_origin, created_by, "
                "created_at, last_used_at, revoked_at FROM automation_clients "
                "WHERE id = 'client-legacy'"
            )


def test_downgrade_preserves_unrelated_source_trigger_sql_and_behavior(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "downgrade-trigger.sqlite"
    _legacy_database(schema_at_0072, path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER audit_token_label AFTER UPDATE OF label ON api_tokens "
            "BEGIN UPDATE owner_account SET updated_at = NEW.created_at "
            "WHERE singleton_id = 1; END"
        )
    original_trigger = _trigger_sql(path, "api_tokens")
    command.upgrade(_config(path), REVISION)
    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _trigger_sql(path, "api_tokens") == original_trigger
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE api_tokens SET label = 'Trigger remains active' WHERE id = 'token-legacy'"
        )
        assert connection.execute(
            "SELECT updated_at FROM owner_account WHERE singleton_id = 1"
        ).fetchone() == (NOW,)


def test_downgrade_refuses_trigger_referencing_removed_linkage_atomically(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "downgrade-linkage-trigger.sqlite"
    _legacy_database(schema_at_0072, path)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER audit_token_owner AFTER UPDATE OF user_id ON api_tokens "
            "BEGIN SELECT NEW.user_id; END"
        )
    before = _database_snapshot(path)
    with pytest.raises(RuntimeError, match="source trigger references authentication linkage"):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _database_snapshot(path) == before
    assert _version(path) == REVISION


@pytest.mark.parametrize("retained_table", ["browser_auth_sessions", "password_reset_grants"])
def test_downgrade_refuses_retained_session_or_reset_rows_atomically(
    schema_at_0072: Path, tmp_path: Path, retained_table: str
) -> None:
    path = tmp_path / f"retained-{retained_table}.sqlite"
    owner_id, _, _ = _legacy_database(schema_at_0072, path, source_rows=False)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        if retained_table == "browser_auth_sessions":
            connection.execute(
                "INSERT INTO browser_auth_sessions "
                "(id, user_id, token_hash, auth_revision, subject, principal_kind, scopes_json, "
                "client_id, created_at, last_seen_at, idle_expires_at, expires_at, revoked_at) "
                "VALUES ('session-kept', ?, 'opaque-digest', 1, 'owner', 'owner_session', '[]', "
                "NULL, ?, ?, ?, ?, NULL)",
                (owner_id, NOW, NOW, UPDATED, UPDATED),
            )
        else:
            connection.execute(
                "INSERT INTO password_reset_grants "
                "(id, user_id, created_by_user_id, token_hash, auth_revision, created_at, "
                "expires_at, consumed_at, revoked_at) "
                "VALUES ('reset-kept', ?, ?, 'opaque-reset-digest', 1, ?, ?, NULL, NULL)",
                (owner_id, owner_id, NOW, UPDATED),
            )
    before = _database_snapshot(path)
    with pytest.raises((RuntimeError, SQLAlchemyError)):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _database_snapshot(path) == before
    assert _version(path) == REVISION


def test_downgrade_refuses_noninitial_credential_and_foreign_link_atomically(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "foreign-credential.sqlite"
    owner_id, _, _ = _legacy_database(schema_at_0072, path)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users (id, legacy_subject, display_name, is_active, "
            "is_instance_admin, created_at, updated_at) VALUES "
            "('other-user', NULL, 'Other', 1, 0, ?, ?)",
            (NOW, UPDATED),
        )
        connection.execute(
            "INSERT INTO user_credentials "
            "(user_id, login_name, password_hash, auth_revision, created_at, updated_at) "
            "VALUES ('other-user', 'other', NULL, 1, ?, ?)",
            (NOW, UPDATED),
        )
        connection.execute(
            "UPDATE api_tokens SET user_id = 'other-user', user_auth_revision = 1 "
            "WHERE id = 'token-legacy'"
        )
    before = _database_snapshot(path)
    with pytest.raises(RuntimeError):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _database_snapshot(path) == before
    assert _version(path) == REVISION


def test_auth_constraints_reject_invalid_revision_and_partial_token_link(
    schema_at_0072: Path, tmp_path: Path
) -> None:
    path = tmp_path / "constraints.sqlite"
    owner_id, _, _ = _legacy_database(schema_at_0072, path, source_rows=False)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE user_credentials SET auth_revision = 0 WHERE user_id = ?",
                (owner_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO api_tokens "
                "(id, label, token_hash, token_prefix, scopes_json, principal_kind, created_at, "
                "user_id, user_auth_revision) VALUES "
                "('invalid-token', 'Invalid', 'digest-invalid', 'pan_invalid', '[]', "
                "'api_token', ?, ?, NULL)",
                (NOW, owner_id),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@dataclass
class AuthenticatedJobCase:
    app: Flask
    client: FlaskClient
    services: dict[str, Any]
    csrf: str
    user_id: str
    auth_revision: int
    browser_session_id: str


@pytest.fixture
def authenticated_job_case(tmp_path: Path) -> Iterator[AuthenticatedJobCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    launch = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]
    client = app.test_client()
    try:
        response = client.post("/api/v1/auth/bootstrap", json={"token": launch})
        assert response.status_code == 200, response.get_json()
        account = services["auth"].initial_account()
        with services["database"].session() as db_session:
            browser_session_id = db_session.scalar(select(BrowserAuthSession.id))
        assert browser_session_id is not None
        yield AuthenticatedJobCase(
            app=app,
            client=client,
            services=services,
            csrf=str(response.get_json()["csrf_token"]),
            user_id=account.user_id,
            auth_revision=account.auth_revision,
            browser_session_id=str(browser_session_id),
        )
    finally:
        services["quick_transcriptions"].stop_maintenance()
        services["database"].dispose()


def test_authenticated_http_job_records_the_request_actor(
    authenticated_job_case: AuthenticatedJobCase,
) -> None:
    case = authenticated_job_case
    response = case.client.post(
        "/api/v1/jobs",
        json={"kind": "noop"},
        headers={"X-CSRF-Token": case.csrf},
    )
    assert response.status_code == 202, response.get_json()
    job_id = response.get_json()["id"]
    with case.services["database"].session() as db_session:
        record = db_session.get(JobActorRecord, job_id)
        assert record is not None
        assert record.user_id == case.user_id
        assert record.auth_revision == case.auth_revision
        assert record.context_json["browser_session_id"] == case.browser_session_id
    assert current_execution_actor.get() is None


def test_authenticated_request_without_actor_fails_closed(
    authenticated_job_case: AuthenticatedJobCase,
) -> None:
    case = authenticated_job_case
    _record, raw_token = case.services["auth"].create_api_token("missing actor probe")
    principal = case.services["auth"].resolve_api_token(
        raw_token, network_zone="loopback", target_instance_id=None
    )
    assert principal is not None
    database = case.services["database"]
    queue = case.services["jobs"]
    with case.app.test_request_context("/api/v1/jobs", method="POST"):
        g.principal_resolved = True
        g.principal = principal
        assert current_execution_actor.get() is None
        with pytest.raises(AccessDenied):
            with database.session() as db_session:
                queue.enqueue_in_session(db_session, "noop")
    with database.session() as db_session:
        assert db_session.scalar(select(func.count()).select_from(Job)) == 0
        assert db_session.scalar(select(func.count()).select_from(JobActorRecord)) == 0
        assert db_session.scalar(select(func.count()).select_from(JobEvent)) == 0


def test_internal_enqueue_without_http_request_keeps_trusted_behavior(
    authenticated_job_case: AuthenticatedJobCase,
) -> None:
    case = authenticated_job_case
    database = case.services["database"]
    queue = case.services["jobs"]
    with execution_actor_scope(None):
        unattributed = queue.enqueue("noop")
    with execution_actor_scope(HostActor("pandrator-cli")):
        host_job = queue.enqueue("noop")
    with database.session() as db_session:
        assert db_session.get(Job, unattributed.id) is not None
        assert db_session.get(JobActorRecord, unattributed.id) is None
        host_record = db_session.get(JobActorRecord, host_job.id)
        assert host_record is not None
        assert host_record.user_id is None
        assert host_record.context_json["source"] == "host"


def test_direct_principal_binding_is_scoped_to_the_request_context(
    authenticated_job_case: AuthenticatedJobCase,
) -> None:
    case = authenticated_job_case
    services = case.services["services"]
    _record, raw_token = case.services["auth"].create_api_token("context binding probe")
    guards = ApiGuards(case.app, services, testing=True, script_policy="'self'")
    outer = HostActor("pandrator-cli")
    with execution_actor_scope(outer):
        with case.app.test_request_context(
            "/api/v1/jobs",
            method="GET",
            headers={"Authorization": f"Bearer {raw_token}"},
        ):
            assert guards.principal() is not None
            bound = current_execution_actor.get()
            assert isinstance(bound, UserActor)
            assert bound.user_id == case.user_id
        assert current_execution_actor.get() == outer
        with case.app.test_request_context("/api/v1/jobs", method="GET"):
            assert guards.principal() is None
            assert current_execution_actor.get() == outer
    assert current_execution_actor.get() is None


def _session_actor(user_id: str, session_id: str) -> UserActor:
    return UserActor(
        user_id=user_id,
        auth_revision=1,
        subject=f"user:{user_id}",
        principal_kind="owner_session",
        scopes=frozenset({"app.read", "app.run", "app.write", "app.cancel"}),
        token_id=None,
        browser_session_id=session_id,
    )


def test_nested_request_contexts_restore_actor_bindings_in_lifo_order(
    tmp_path: Path,
) -> None:
    app = Flask("actor-stack-regression")
    app.secret_key = "actor-stack-regression"
    guards = ApiGuards(
        app,
        SimpleNamespace(paths=SimpleNamespace(root=tmp_path)),
        testing=True,
        script_policy="'self'",
    )
    guards.register()
    outer_actor = _session_actor("outer-user", "outer-session")
    inner_actor = _session_actor("inner-user", "inner-session")
    neutral = HostActor("pandrator-cli")
    with execution_actor_scope(neutral):
        with app.test_request_context("/api/v1/jobs"):
            guards._bind_execution_actor(outer_actor)
            assert current_execution_actor.get() == outer_actor
            with app.test_request_context("/api/v1/jobs"):
                guards._bind_execution_actor(inner_actor)
                assert current_execution_actor.get() == inner_actor
            # Same-app request contexts share ``g``; the inner teardown must
            # restore only its own binding, not consume the outer token.
            assert current_execution_actor.get() == outer_actor
        assert current_execution_actor.get() == neutral
    assert current_execution_actor.get() is None


def test_nested_dispatched_request_preserves_outer_actor_and_job_provenance(
    authenticated_job_case: AuthenticatedJobCase,
) -> None:
    case = authenticated_job_case
    services = case.services["services"]
    database = case.services["database"]
    queue = case.services["jobs"]
    _record, raw_token = case.services["auth"].create_api_token("nested dispatch probe")
    guards = ApiGuards(case.app, services, testing=True, script_policy="'self'")
    with case.app.test_request_context(
        "/api/v1/jobs",
        method="GET",
        headers={"Authorization": f"Bearer {raw_token}"},
    ):
        case.app.preprocess_request()
        outer_principal = guards.principal()
        assert outer_principal is not None
        outer_actor = current_execution_actor.get()
        assert isinstance(outer_actor, UserActor)
        assert outer_actor.user_id == case.user_id

        # The nested request reuses this app context (and ``g``), so its
        # teardown used to clear the outer actor and principal.
        response = case.app.test_client().get("/api/v1/health")
        assert response.status_code == 200
        assert current_execution_actor.get() == outer_actor
        assert g.principal is outer_principal

        with database.session() as db_session:
            job = queue.enqueue_in_session(db_session, "noop")
        job_id = job.id
    assert current_execution_actor.get() is None
    with database.session() as db_session:
        record = db_session.get(JobActorRecord, job_id)
        assert record is not None
        assert record.user_id == case.user_id
        assert record.auth_revision == case.auth_revision


def test_teardown_exception_still_resets_request_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def failing_acquisition(_database):
        yield
        raise RuntimeError("acquisition teardown failed")

    monkeypatch.setattr(
        "pandrator.web.http_lifecycle.model_acquisition_for_actor",
        failing_acquisition,
    )
    app = Flask("actor-teardown-regression")
    app.secret_key = "actor-teardown-regression"
    guards = ApiGuards(
        app,
        SimpleNamespace(
            paths=SimpleNamespace(root=tmp_path),
            database=None,
            redactor=SimpleNamespace(
                redact=lambda value: str(value),
                redact_value=lambda value: value,
            ),
        ),
        testing=True,
        script_policy="'self'",
    )
    guards.register()
    actor = _session_actor("request-user", "request-session")

    @app.get("/actor-probe")
    def actor_probe():
        guards._bind_execution_actor(actor)
        assert current_execution_actor.get() == actor
        return "ok"

    with execution_actor_scope(None):
        with pytest.raises(RuntimeError, match="acquisition teardown failed"):
            app.test_client().get("/actor-probe")
        assert current_execution_actor.get() is None
