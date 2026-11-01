from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Table
from sqlalchemy.exc import SAWarning, SQLAlchemyError

from pandrator.web.database import sqlite_url
from pandrator.web.models import StoredCredential

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0070_credential_namespaces"
PREVIOUS_REVISION = "0069_pronunciation_scope"
BOOTSTRAP_PREVIOUS_REVISION = "0008_generation_paragraph_boundaries"
BOOTSTRAP_REVISION = "0009_stored_credentials"
TABLE = "stored_credentials"
WORKSPACE_FK = "fk_stored_credentials_workspace_id_workspaces"
NOW = "2026-10-23 13:20:00.123456"


@pytest.fixture(scope="session")
def schema_at_0069(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("credential-schema-base") / "at-0069.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _secret_digest(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _legacy_database(
    schema_at_0069: Path,
    path: Path,
    *,
    timestamp_defaults: bool = True,
    rows: Sequence[tuple[str, str, str, str, str]] = (),
    custom_index: bool = False,
    primary_key_name: str | None = None,
) -> None:
    shutil.copy2(schema_at_0069, path)
    defaults = " DEFAULT CURRENT_TIMESTAMP" if timestamp_defaults else ""
    primary_key = (
        "PRIMARY KEY (key)"
        if primary_key_name is None
        else f"CONSTRAINT {primary_key_name} PRIMARY KEY (key)"
    )
    table_sql = f"""CREATE TABLE stored_credentials (
        key VARCHAR(255) NOT NULL,
        label VARCHAR(160) NOT NULL,
        secret_value TEXT NOT NULL,
        created_at DATETIME NOT NULL{defaults},
        updated_at DATETIME NOT NULL{defaults},
        {primary_key}
    )"""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE stored_credentials")
        connection.execute(table_sql)
        connection.executemany(
            "INSERT INTO stored_credentials "
            "(key, label, secret_value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        if custom_index:
            connection.execute(
                "CREATE INDEX custom_stored_credentials_label_desc "
                "ON stored_credentials (label DESC, key)"
            )


def _replace_with_model_target(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE stored_credentials")
    engine = sa.create_engine(sqlite_url(path))
    try:
        cast(Table, StoredCredential.__table__).create(engine)
    finally:
        engine.dispose()


def _owner_workspace(connection: sqlite3.Connection) -> tuple[str, str]:
    rows = connection.execute(
        "SELECT users.id, workspaces.id FROM users JOIN workspaces "
        "ON workspaces.personal_owner_user_id = users.id "
        "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
    ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1])


def _create_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    connection.execute(
        "INSERT INTO workspaces "
        "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
        "VALUES (?, 'team', ?, NULL, 'active', 1, ?, ?)",
        (workspace_id, workspace_id, NOW, NOW),
    )


def _credential_rows(path: Path, *, target: bool) -> list[tuple[Any, ...]]:
    if target:
        query = (
            "SELECT workspace_id, key, label, secret_value, created_at, updated_at, revision "
            "FROM stored_credentials ORDER BY workspace_id, key"
        )
    else:
        query = (
            "SELECT key, label, secret_value, created_at, updated_at "
            "FROM stored_credentials ORDER BY key"
        )
    with sqlite3.connect(path) as connection:
        rows = connection.execute(query).fetchall()
    if target:
        return [
            (
                str(workspace_id),
                str(key),
                str(label),
                _secret_digest(secret),
                str(created_at),
                str(updated_at),
                int(revision),
            )
            for workspace_id, key, label, secret, created_at, updated_at, revision in rows
        ]
    return [
        (
            str(key),
            str(label),
            _secret_digest(secret),
            str(created_at),
            str(updated_at),
        )
        for key, label, secret, created_at, updated_at in rows
    ]


def _index_sql(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL ORDER BY name",
            (TABLE,),
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
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        data: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        for (table_name_raw,) in tables:
            table_name = str(table_name_raw)
            columns = connection.execute(
                f'PRAGMA table_info("{table_name.replace(chr(34), chr(34) * 2)}")'
            ).fetchall()
            primary_key = [
                str(column[1]) for column in sorted(columns, key=lambda item: item[5]) if column[5]
            ]
            order_by = primary_key or [str(column[1]) for column in columns]
            order_sql = ", ".join('"' + column.replace('"', '""') + '"' for column in order_by)
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            rows = connection.execute(
                f"SELECT * FROM {quoted_table} ORDER BY {order_sql}"
            ).fetchall()
            indexes = {str(column[1]): index for index, column in enumerate(columns)}
            secret_index = indexes.get("secret_value") if table_name == TABLE else None
            if secret_index is not None:
                rows = [
                    tuple(
                        ("sha256", _secret_digest(value)) if index == secret_index else value
                        for index, value in enumerate(row)
                    )
                    for row in rows
                ]
            data.append((table_name, tuple(tuple(row) for row in rows)))
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    return objects, tuple(data), str(version)


def _assert_no_batch_tables(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("_alembic_tmp_") for name in names)


def _assert_upgrade_failure_is_atomic(
    path: Path, *, error_type: type[BaseException] = RuntimeError
) -> None:
    before = _database_snapshot(path)
    with pytest.raises(error_type):
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == before
    _assert_no_batch_tables(path)


@pytest.mark.parametrize("timestamp_defaults", [True, False])
def test_legacy_rows_and_timestamp_defaults_are_preserved(
    schema_at_0069: Path, tmp_path: Path, timestamp_defaults: bool
) -> None:
    path = tmp_path / f"legacy-{timestamp_defaults}.sqlite"
    secret = "  synthetic credential fixture with trailing whitespace  "
    legacy_row = (
        "synthetic-key",
        "  exact label  ",
        secret,
        "2024-02-03 04:05:06.123456",
        "2025-07-08 09:10:11.654321",
    )
    _legacy_database(
        schema_at_0069,
        path,
        timestamp_defaults=timestamp_defaults,
        rows=[legacy_row],
        custom_index=True,
    )
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        before = _credential_rows(path, target=False)
    index_before = _index_sql(path)

    command.upgrade(_config(path), REVISION)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == REVISION
        )
        assert connection.execute(
            "SELECT created_at, updated_at FROM stored_credentials"
        ).fetchone() == (legacy_row[3], legacy_row[4])
        defaults = {
            str(row[1]): row[4]
            for row in connection.execute("PRAGMA table_info(stored_credentials)")
        }
        assert connection.execute(
            "SELECT revision FROM stored_credentials WHERE key = ?", (legacy_row[0],)
        ).fetchone() == (1,)
    assert _credential_rows(path, target=True) == [
        (
            workspace_id,
            legacy_row[0],
            legacy_row[1],
            _secret_digest(secret),
            legacy_row[3],
            legacy_row[4],
            1,
        )
    ]
    expected_default = "CURRENT_TIMESTAMP" if timestamp_defaults else None
    assert defaults["created_at"] == expected_default
    assert defaults["updated_at"] == expected_default
    assert _index_sql(path) == index_before
    assert _index_sql(path)["custom_stored_credentials_label_desc"] == (
        "CREATE INDEX custom_stored_credentials_label_desc ON stored_credentials (label DESC, key)"
    )
    assert before == [
        (
            legacy_row[0],
            legacy_row[1],
            _secret_digest(secret),
            legacy_row[3],
            legacy_row[4],
        )
    ]


def test_composite_key_named_foreign_key_revision_default_and_restrict_delete(
    schema_at_0069: Path, tmp_path: Path
) -> None:
    path = tmp_path / "composite-namespace.sqlite"
    secret = "synthetic scoped row"
    _legacy_database(
        schema_at_0069,
        path,
        rows=[("same-key", "Initial", secret, NOW, NOW)],
    )
    command.upgrade(_config(path), REVISION)

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        owner_id, initial_workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-second")
        primary_key = [
            str(column[1])
            for column in sorted(
                connection.execute("PRAGMA table_info(stored_credentials)").fetchall(),
                key=lambda item: item[5],
            )
            if column[5]
        ]
        foreign_keys = connection.execute("PRAGMA foreign_key_list(stored_credentials)").fetchall()
        connection.execute(
            "INSERT INTO stored_credentials "
            "(workspace_id, key, label, secret_value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("workspace-second", "same-key", "Second", "synthetic second row", NOW, NOW),
        )
        assert connection.execute(
            "SELECT revision FROM stored_credentials WHERE workspace_id = ? AND key = ?",
            ("workspace-second", "same-key"),
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO stored_credentials "
                "(workspace_id, key, label, secret_value, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "workspace-second",
                    "same-key",
                    "Duplicate",
                    "synthetic duplicate",
                    NOW,
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM workspaces WHERE id = ?", ("workspace-second",))
    assert owner_id
    assert primary_key == ["workspace_id", "key"]
    assert len(foreign_keys) == 1
    assert foreign_keys[0][2:5] == (
        "workspaces",
        "workspace_id",
        "id",
    )
    assert foreign_keys[0][6] == "RESTRICT"
    engine = sa.create_engine(sqlite_url(path))
    try:
        reflected_foreign_keys = sa.inspect(engine).get_foreign_keys(TABLE)
    finally:
        engine.dispose()
    assert reflected_foreign_keys == [
        {
            "name": WORKSPACE_FK,
            "constrained_columns": ["workspace_id"],
            "referred_schema": None,
            "referred_table": "workspaces",
            "referred_columns": ["id"],
            "options": {"ondelete": "RESTRICT"},
        }
    ]
    assert _credential_rows(path, target=True) == [
        (initial_workspace_id, "same-key", "Initial", _secret_digest(secret), NOW, NOW, 1),
        (
            "workspace-second",
            "same-key",
            "Second",
            _secret_digest("synthetic second row"),
            NOW,
            NOW,
            1,
        ),
    ]


@pytest.mark.parametrize("primary_key_name", [None, "legacy_credential_primary"])
def test_pk_replacement_warning_is_error_and_existing_pk_name_is_resolved(
    schema_at_0069: Path,
    tmp_path: Path,
    primary_key_name: str | None,
) -> None:
    label = primary_key_name or "unnamed"
    path = tmp_path / f"pk-warning-{label}.sqlite"
    _legacy_database(
        schema_at_0069,
        path,
        rows=[("preserved-key", "Legacy", "synthetic-pk-row", NOW, NOW)],
        primary_key_name=primary_key_name,
    )
    warning_message = (
        r"Table .* specifies columns .* as primary_key=True, "
        r"not matching locally specified columns"
    )
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=warning_message, category=SAWarning)
        command.upgrade(_config(path), REVISION)

        engine = sa.create_engine(sqlite_url(path))
        try:
            upgraded_pk = sa.inspect(engine).get_pk_constraint(TABLE)
        finally:
            engine.dispose()
        expected_pk_name = primary_key_name or "pk_stored_credentials"
        assert upgraded_pk["name"] == expected_pk_name
        assert upgraded_pk["constrained_columns"] == ["workspace_id", "key"]

        command.downgrade(_config(path), PREVIOUS_REVISION)

    engine = sa.create_engine(sqlite_url(path))
    try:
        downgraded_pk = sa.inspect(engine).get_pk_constraint(TABLE)
    finally:
        engine.dispose()
    assert downgraded_pk["name"] == (primary_key_name or "pk_stored_credentials")
    assert downgraded_pk["constrained_columns"] == ["key"]
    assert _credential_rows(path, target=False) == [
        (
            "preserved-key",
            "Legacy",
            _secret_digest("synthetic-pk-row"),
            NOW,
            NOW,
        )
    ]


def test_invalid_seed_membership_fails_before_ddl(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "invalid-seed.sqlite"
    _legacy_database(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "DELETE FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, owner_id),
        )
    _assert_upgrade_failure_is_atomic(path)


def test_invalid_seed_owner_fails_before_ddl(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "inactive-seed-owner.sqlite"
    _legacy_database(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        owner_id, _workspace_id = _owner_workspace(connection)
        connection.execute("UPDATE users SET is_active = 0 WHERE id = ?", (owner_id,))
    _assert_upgrade_failure_is_atomic(path)


def test_bad_legacy_table_shape_fails_before_ddl(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "bad-legacy-shape.sqlite"
    _legacy_database(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE stored_credentials")
        connection.execute(
            "CREATE TABLE stored_credentials ("
            "key VARCHAR(255) NOT NULL, label VARCHAR(160) NOT NULL, "
            "secret_value TEXT NOT NULL, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL)"
        )
    _assert_upgrade_failure_is_atomic(path)


def test_unexpected_timestamp_default_fails_before_ddl(
    schema_at_0069: Path, tmp_path: Path
) -> None:
    path = tmp_path / "unexpected-time-default.sqlite"
    shutil.copy2(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE stored_credentials")
        connection.execute(
            "CREATE TABLE stored_credentials ("
            "key VARCHAR(255) NOT NULL PRIMARY KEY, label VARCHAR(160) NOT NULL, "
            "secret_value TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT 'bad', "
            "updated_at DATETIME NOT NULL DEFAULT 'bad')"
        )
    _assert_upgrade_failure_is_atomic(path)


def test_inbound_foreign_key_fails_before_ddl(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "inbound-foreign-key.sqlite"
    _legacy_database(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE credential_referrers ("
            "credential_key VARCHAR(255) REFERENCES stored_credentials(key))"
        )
    _assert_upgrade_failure_is_atomic(path)


def test_partial_target_columns_fail_before_ddl(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "partial-target.sqlite"
    _legacy_database(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE stored_credentials ADD COLUMN workspace_id VARCHAR(36)")
    _assert_upgrade_failure_is_atomic(path)


def test_malformed_target_shape_fails_before_ddl(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "malformed-target.sqlite"
    _legacy_database(schema_at_0069, path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE stored_credentials ADD COLUMN workspace_id TEXT")
        connection.execute(
            "ALTER TABLE stored_credentials ADD COLUMN revision INTEGER NOT NULL DEFAULT 2"
        )
    _assert_upgrade_failure_is_atomic(path)


def test_empty_correct_early_target_metadata_is_adopted(
    schema_at_0069: Path, tmp_path: Path
) -> None:
    path = tmp_path / "empty-early-target.sqlite"
    shutil.copy2(schema_at_0069, path)
    _replace_with_model_target(path)
    before = _database_snapshot(path)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == REVISION
        )
        assert connection.execute("SELECT COUNT(*) FROM stored_credentials").fetchone()[0] == 0
    after = _database_snapshot(path)
    assert before[0] == after[0]


def test_populated_early_target_fails_atomically(schema_at_0069: Path, tmp_path: Path) -> None:
    path = tmp_path / "populated-early-target.sqlite"
    shutil.copy2(schema_at_0069, path)
    _replace_with_model_target(path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "INSERT INTO stored_credentials "
            "(workspace_id, key, label, secret_value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (workspace_id, "prepopulated", "Early row", "synthetic early value", NOW, NOW),
        )
    _assert_upgrade_failure_is_atomic(path)


def test_late_version_failure_rolls_back_then_retry_repeat_and_downgrade(
    schema_at_0069: Path, tmp_path: Path
) -> None:
    path = tmp_path / "late-version-failure.sqlite"
    rows = [
        ("credential-a", "First", "synthetic-a", NOW, NOW),
        ("credential-b", "Second", "synthetic-b", NOW, NOW),
    ]
    _legacy_database(schema_at_0069, path, rows=rows, custom_index=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_credential_version_update "
            "BEFORE UPDATE OF version_num ON alembic_version "
            f"WHEN NEW.version_num = '{REVISION}' "
            "BEGIN SELECT RAISE(ABORT, 'late credential migration failure'); END"
        )
    _assert_upgrade_failure_is_atomic(path, error_type=SQLAlchemyError)

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_credential_version_update")
    old_rows = _credential_rows(path, target=False)
    old_indexes = _index_sql(path)
    command.upgrade(_config(path), REVISION)
    upgraded_snapshot = _database_snapshot(path)
    assert _credential_rows(path, target=True) == [
        (
            _owner_workspace_from_path(path),
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            1,
        )
        for row in old_rows
    ]
    assert _index_sql(path) == old_indexes
    command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == upgraded_snapshot

    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _credential_rows(path, target=False) == old_rows
    assert _index_sql(path) == old_indexes
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == PREVIOUS_REVISION
        )
        primary_key = [
            str(column[1])
            for column in sorted(
                connection.execute("PRAGMA table_info(stored_credentials)").fetchall(),
                key=lambda item: item[5],
            )
            if column[5]
        ]
        assert primary_key == ["key"]
        assert "workspace_id" not in {
            str(column[1]) for column in connection.execute("PRAGMA table_info(stored_credentials)")
        }
    command.upgrade(_config(path), REVISION)
    assert _credential_rows(path, target=True) == [
        (
            _owner_workspace_from_path(path),
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            1,
        )
        for row in old_rows
    ]


def _owner_workspace_from_path(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return _owner_workspace(connection)[1]


def test_downgrade_refuses_noninitial_namespace_before_ddl(
    schema_at_0069: Path, tmp_path: Path
) -> None:
    path = tmp_path / "downgrade-second-workspace.sqlite"
    _legacy_database(
        schema_at_0069,
        path,
        rows=[("shared-key", "Initial", "synthetic-initial", NOW, NOW)],
    )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_workspace(connection, "workspace-noninitial")
        connection.execute(
            "INSERT INTO stored_credentials "
            "(workspace_id, key, label, secret_value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "workspace-noninitial",
                "shared-key",
                "Noninitial",
                "synthetic-noninitial",
                NOW,
                NOW,
            ),
        )
    before = _database_snapshot(path)
    with pytest.raises(RuntimeError, match="non-initial workspace"):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _database_snapshot(path) == before
    _assert_no_batch_tables(path)


def test_0009_bootstrap_extraction_survives_0070_adoption(tmp_path: Path) -> None:
    path = tmp_path / "0009-bootstrap.sqlite"
    config = _config(path)
    command.upgrade(config, BOOTSTRAP_PREVIOUS_REVISION)
    secret = "synthetic inline provider secret fixture"
    settings = {
        "provider_configs": [
            {
                "id": "credential-schema-provider",
                "api_key": secret,
            }
        ]
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO app_settings (key, value_json, revision, updated_at) VALUES (?, ?, ?, ?)",
            ("services.tts", json.dumps(settings), 1, NOW),
        )

    command.upgrade(config, BOOTSTRAP_REVISION)
    with sqlite3.connect(path) as connection:
        legacy_row = connection.execute(
            "SELECT key, label, secret_value FROM stored_credentials"
        ).fetchone()
        migrated_settings = json.loads(
            connection.execute(
                "SELECT value_json FROM app_settings WHERE key = ?", ("services.tts",)
            ).fetchone()[0]
        )
    assert legacy_row[:2] == (
        "tts:credential_schema_provider",
        "credential-schema-provider API key",
    )
    assert _secret_digest(legacy_row[2]) == _secret_digest(secret)
    provider_record = migrated_settings["provider_configs"][0]
    assert provider_record["secret_ref"] == "db:tts:credential_schema_provider"
    assert "api_key" not in provider_record

    command.upgrade(config, REVISION)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        row = connection.execute(
            "SELECT workspace_id, key, revision, secret_value FROM stored_credentials"
        ).fetchone()
    assert row[:3] == (workspace_id, "tts:credential_schema_provider", 1)
    assert _secret_digest(row[3]) == _secret_digest(secret)
