from __future__ import annotations

import shutil
import sqlite3
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SAWarning

from pandrator.web.database import sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0072_workspace_settings"
PREVIOUS_REVISION = "0071_provider_workspace"
CURRENT = "app_settings"
HISTORY = "app_settings_history"
WORKSPACE_CURRENT = "workspace_settings"
WORKSPACE_HISTORY = "workspace_settings_history"
HISTORY_INDEX = "ix_workspace_settings_history_workspace_key"
CURRENT_FK = "fk_workspace_settings_workspace_id_workspaces"
HISTORY_FK = "fk_workspace_settings_history_workspace_id_workspaces"
NOW = "2026-10-23 13:20:00.123456"
UPDATED = "2026-10-23 13:21:01.654321"
HOST_KEYS = ("system.identity", "session.trash_policy", "web.preferences")

CurrentRow = tuple[str, str, int, str]
HistoryRow = tuple[int, str, str, int, str]


@pytest.fixture(scope="session")
def schema_at_0071(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("settings-schema-base") / "at-0071.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _owner_workspace(connection: sqlite3.Connection) -> tuple[str, str]:
    rows = connection.execute(
        "SELECT users.id, workspaces.id FROM users JOIN workspaces "
        "ON workspaces.personal_owner_user_id = users.id "
        "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
    ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1])


def _legacy_database(
    schema_at_0071: Path,
    path: Path,
    *,
    current_rows: Sequence[CurrentRow] = (),
    history_rows: Sequence[HistoryRow] = (),
    keep_target_tables: bool = False,
    extra_current_index: bool = False,
    extra_history_index: bool = False,
    current_revision_default: bool = False,
    current_unique: bool = False,
) -> None:
    shutil.copy2(schema_at_0071, path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        if not keep_target_tables:
            connection.execute(f"DROP TABLE {WORKSPACE_HISTORY}")
            connection.execute(f"DROP TABLE {WORKSPACE_CURRENT}")
        connection.execute(f"DROP TABLE {HISTORY}")
        connection.execute(f"DROP TABLE {CURRENT}")
        revision_default = " DEFAULT 1" if current_revision_default else ""
        extra_unique = ", UNIQUE (key, revision)" if current_unique else ""
        connection.execute(
            "CREATE TABLE app_settings (\n"
            '    "key" VARCHAR(120) NOT NULL,\n'
            "    value_json JSON NOT NULL,\n"
            f"    revision INTEGER NOT NULL{revision_default},\n"
            "    updated_at DATETIME NOT NULL,\n"
            f'    PRIMARY KEY ("key"){extra_unique}\n'
            ")"
        )
        connection.execute(
            "CREATE TABLE app_settings_history (\n"
            "    id INTEGER NOT NULL,\n"
            '    "key" VARCHAR(120) NOT NULL,\n'
            "    value_json JSON NOT NULL,\n"
            "    revision INTEGER NOT NULL,\n"
            "    created_at DATETIME NOT NULL,\n"
            "    PRIMARY KEY (id)\n"
            ")"
        )
        connection.execute(
            'CREATE INDEX ix_app_settings_history_key ON app_settings_history ("key")'
        )
        connection.executemany(
            "INSERT INTO app_settings (key, value_json, revision, updated_at) VALUES (?, ?, ?, ?)",
            current_rows,
        )
        connection.executemany(
            "INSERT INTO app_settings_history (id, key, value_json, revision, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            history_rows,
        )
        if extra_current_index:
            connection.execute(
                "CREATE INDEX custom_app_settings_revision ON app_settings (revision DESC)"
            )
        if extra_history_index:
            connection.execute(
                "CREATE INDEX custom_app_settings_history_created "
                "ON app_settings_history (created_at, id DESC)"
            )


def _create_team(connection: sqlite3.Connection, workspace_id: str) -> None:
    connection.execute(
        "INSERT INTO workspaces "
        "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
        "VALUES (?, 'team', ?, NULL, 'active', 1, ?, ?)",
        (workspace_id, workspace_id, NOW, UPDATED),
    )


def _insert_workspace_current(
    connection: sqlite3.Connection,
    workspace_id: str,
    key: str,
    value_json: str,
    revision: int = 1,
    updated_at: str = NOW,
) -> None:
    connection.execute(
        "INSERT INTO workspace_settings "
        "(workspace_id, key, value_json, revision, updated_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, key, value_json, revision, updated_at),
    )


def _insert_workspace_history(
    connection: sqlite3.Connection,
    row_id: int,
    workspace_id: str,
    key: str,
    value_json: str,
    revision: int,
    created_at: str,
) -> None:
    connection.execute(
        "INSERT INTO workspace_settings_history "
        "(id, workspace_id, key, value_json, revision, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, workspace_id, key, value_json, revision, created_at),
    )


def _current_rows(path: Path, *, workspace_id: str | None = None) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as connection:
        if workspace_id is None:
            return connection.execute(
                "SELECT key, value_json, revision, updated_at FROM app_settings "
                "ORDER BY key COLLATE BINARY"
            ).fetchall()
        return connection.execute(
            "SELECT workspace_id, key, value_json, revision, updated_at "
            "FROM workspace_settings WHERE workspace_id = ? ORDER BY key COLLATE BINARY",
            (workspace_id,),
        ).fetchall()


def _history_rows(path: Path, *, workspace_id: str | None = None) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as connection:
        if workspace_id is None:
            return connection.execute(
                "SELECT id, key, value_json, revision, created_at FROM app_settings_history "
                "ORDER BY id"
            ).fetchall()
        return connection.execute(
            "SELECT id, workspace_id, key, value_json, revision, created_at "
            "FROM workspace_settings_history WHERE workspace_id = ? ORDER BY id",
            (workspace_id,),
        ).fetchall()


def _setting_schema_sql(path: Path) -> dict[str, str]:
    names = (CURRENT, HISTORY, "ix_app_settings_history_key")
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?, ?) ORDER BY name",
            names,
        ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _target_index_sql(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name IN (?, ?) AND sql IS NOT NULL ORDER BY name",
            (WORKSPACE_CURRENT, WORKSPACE_HISTORY),
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
            data.append((table_name, tuple(tuple(row) for row in rows)))
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return objects, tuple(data), str(version_row[0])


def _assert_no_migration_temp_tables(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("_alembic_tmp_") for name in names)


def _assert_upgrade_failure_is_atomic(path: Path, message: str | None = None) -> None:
    before = _database_snapshot(path)
    expectation = (
        pytest.raises(RuntimeError, match=message) if message else pytest.raises(RuntimeError)
    )
    with expectation:
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == before
    _assert_no_migration_temp_tables(path)


def _assert_downgrade_failure_is_atomic(path: Path, message: str | None = None) -> None:
    before = _database_snapshot(path)
    expectation = (
        pytest.raises(RuntimeError, match=message) if message else pytest.raises(RuntimeError)
    )
    with expectation:
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _database_snapshot(path) == before
    _assert_no_migration_temp_tables(path)


def test_private_rows_move_raw_while_host_tables_and_indexes_remain_unchanged(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "raw-settings-adoption.sqlite"
    current_rows = [
        ("system.identity", '{ "host": [ 1, null ], "raw": " café " }', 4, NOW),
        ("session.trash_policy", '{"days":30}', 1, UPDATED),
        ("web.preferences", '{ "theme" : "dark" }', 9, NOW),
        ("System.identity", '{ "case_variant": true }', 2, UPDATED),
        ("system.identity.extra", '{"nested": [3, 2, 1]}', 3, NOW),
        ("legacy.setting", ' { "space" : "  kept  " } ', 0, UPDATED),
        ("defaults.tts", '{ "provider" : "fixture", "value" : null }', -7, NOW),
    ]
    history_rows = [
        (11, "system.identity", '{ "host-history" : true }', 2, NOW),
        (19, "web.preferences", '[ "old", "host" ]', -3, UPDATED),
        (101, "legacy.setting", ' [ { "orphan" : true }, null ] ', 0, NOW),
        (305, "case.setting", '{"unicode":"naïve 東京"}', 17, UPDATED),
    ]
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=current_rows,
        history_rows=history_rows,
        extra_current_index=True,
        extra_history_index=True,
    )
    with sqlite3.connect(path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "CREATE TABLE unrelated_settings_fixture (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX unrelated_settings_fixture_payload ON unrelated_settings_fixture(payload)"
        )
        connection.execute(
            "INSERT INTO unrelated_settings_fixture (id, payload) VALUES (1, ?)",
            (' { "untouched": true } ',),
        )
    old_sql = _setting_schema_sql(path)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        command.upgrade(_config(path), REVISION)

    expected_private_current = sorted(
        (workspace_id, *row) for row in current_rows if row[0] not in HOST_KEYS
    )
    expected_private_history = [
        (row[0], workspace_id, *row[1:]) for row in history_rows if row[1] not in HOST_KEYS
    ]
    assert _current_rows(path) == sorted(
        (tuple(row) for row in current_rows if row[0] in HOST_KEYS), key=lambda row: row[0]
    )
    assert _history_rows(path) == [tuple(row) for row in history_rows if row[1] in HOST_KEYS]
    assert _current_rows(path, workspace_id=workspace_id) == expected_private_current
    assert _history_rows(path, workspace_id=workspace_id) == expected_private_history
    assert _setting_schema_sql(path) == old_sql
    assert owner_id
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT payload FROM unrelated_settings_fixture WHERE id = 1"
        ).fetchone() == (' { "untouched": true } ',)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
        assert (
            connection.execute(
                "SELECT * FROM sqlite_master WHERE name = 'custom_app_settings_revision'"
            ).fetchone()[0]
            == "index"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert _target_index_sql(path) == {
        HISTORY_INDEX: (
            "CREATE INDEX ix_workspace_settings_history_workspace_key "
            'ON workspace_settings_history (workspace_id, "key")'
        )
    }
    engine = sa.create_engine(sqlite_url(path))
    try:
        current_fks = sa.inspect(engine).get_foreign_keys(WORKSPACE_CURRENT)
        history_fks = sa.inspect(engine).get_foreign_keys(WORKSPACE_HISTORY)
        current_pk = sa.inspect(engine).get_pk_constraint(WORKSPACE_CURRENT)
    finally:
        engine.dispose()
    assert current_pk["constrained_columns"] == ["workspace_id", "key"]
    assert current_fks[0]["name"] == CURRENT_FK
    assert (current_fks[0].get("options") or {}).get("ondelete") == "RESTRICT"
    assert history_fks[0]["name"] == HISTORY_FK
    assert (history_fks[0].get("options") or {}).get("ondelete") == "RESTRICT"


def test_exact_host_keys_stay_host_and_same_private_key_can_exist_in_two_workspaces(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-shared-key.sqlite"
    private = ("System.identity", '{"private": true}', 4, NOW)
    _legacy_database(schema_at_0071, path, current_rows=[private])
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_team(connection, "workspace-team")
        _insert_workspace_current(
            connection,
            "workspace-team",
            private[0],
            '{ "other-workspace": true }',
        )
        rows = connection.execute(
            "SELECT workspace_id, key FROM workspace_settings WHERE key = ? ORDER BY workspace_id",
            (private[0],),
        ).fetchall()
    assert rows == [(workspace_id, private[0]), ("workspace-team", private[0])]


@pytest.mark.parametrize("keep_target_tables", [False, True])
def test_upgrade_then_downgrade_round_trip_preserves_raw_rows_and_old_sql(
    schema_at_0071: Path, tmp_path: Path, keep_target_tables: bool
) -> None:
    path = tmp_path / f"settings-round-trip-{keep_target_tables}.sqlite"
    current_rows = [
        ("defaults.audio", '{ "raw" : [ 2, 1 ] }', 3, NOW),
        ("web.preferences", '{"host":true}', 1, UPDATED),
    ]
    history_rows = [
        (9, "defaults.audio", '[ "old" ]', 2, UPDATED),
        (12, "legacy.orphan", ' { "orphan" : null } ', -2, NOW),
        (26, "web.preferences", '{"host-history":true}', 1, UPDATED),
    ]
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=current_rows,
        history_rows=history_rows,
        keep_target_tables=keep_target_tables,
        extra_current_index=True,
        extra_history_index=True,
    )
    old_sql = _setting_schema_sql(path)
    expected_current = sorted(current_rows, key=lambda row: row[0])
    expected_history = sorted(history_rows)

    command.upgrade(_config(path), REVISION)
    command.downgrade(_config(path), PREVIOUS_REVISION)

    assert _current_rows(path) == expected_current
    assert _history_rows(path) == expected_history
    assert _setting_schema_sql(path) == old_sql
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PREVIOUS_REVISION,
        )
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
        assert WORKSPACE_CURRENT not in names
        assert WORKSPACE_HISTORY not in names


def test_valid_populated_early_targets_are_preserved_and_new_rows_are_added(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-populated-early-target.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.new", '{"old":true}', 2, NOW)],
        history_rows=[(27, "legacy.history", "[ 1, 2 ]", 0, UPDATED)],
        keep_target_tables=True,
    )
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _insert_workspace_current(
            connection, workspace_id, "early.current", ' { "before" : true } ', 8, UPDATED
        )
        _insert_workspace_history(
            connection, 801, workspace_id, "early.history", '{"before":2}', -5, NOW
        )
    command.upgrade(_config(path), REVISION)

    assert _current_rows(path, workspace_id=workspace_id) == [
        (workspace_id, "early.current", ' { "before" : true } ', 8, UPDATED),
        (workspace_id, "legacy.new", '{"old":true}', 2, NOW),
    ]
    assert _history_rows(path, workspace_id=workspace_id) == [
        (27, workspace_id, "legacy.history", "[ 1, 2 ]", 0, UPDATED),
        (801, workspace_id, "early.history", '{"before":2}', -5, NOW),
    ]


def test_disabled_demoted_owner_and_archived_personal_workspace_remain_adoptable(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-structural-owner.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"fixture":true}', 1, NOW)],
    )
    with sqlite3.connect(path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "UPDATE users SET is_active = 0, is_instance_admin = 0 WHERE id = ?", (owner_id,)
        )
        connection.execute("UPDATE workspaces SET state = 'archived' WHERE id = ?", (workspace_id,))
    command.upgrade(_config(path), REVISION)
    assert _current_rows(path, workspace_id=workspace_id) == [
        (workspace_id, "legacy.setting", '{"fixture":true}', 1, NOW)
    ]


def test_partial_target_tables_fail_before_schema_or_data_changes(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-partial-target.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE workspace_settings (workspace_id VARCHAR(36) NOT NULL, "
            '"key" VARCHAR(120) NOT NULL, value_json JSON NOT NULL, revision INTEGER NOT NULL, '
            'updated_at DATETIME NOT NULL, PRIMARY KEY (workspace_id, "key"))'
        )
    _assert_upgrade_failure_is_atomic(path, "only one workspace settings table")


@pytest.mark.parametrize("damage", ["history_index", "current_fk", "current_default"])
def test_malformed_early_target_schema_fails_before_mutation(
    schema_at_0071: Path, tmp_path: Path, damage: str
) -> None:
    path = tmp_path / f"settings-malformed-target-{damage}.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
        keep_target_tables=True,
    )
    with sqlite3.connect(path) as connection:
        if damage == "history_index":
            connection.execute(f"DROP INDEX {HISTORY_INDEX}")
        else:
            connection.execute("DROP TABLE workspace_settings")
            if damage == "current_fk":
                connection.execute(
                    "CREATE TABLE workspace_settings (workspace_id VARCHAR(36) NOT NULL, "
                    '"key" VARCHAR(120) NOT NULL, value_json JSON NOT NULL, revision INTEGER NOT NULL, '
                    'updated_at DATETIME NOT NULL, PRIMARY KEY (workspace_id, "key"))'
                )
            else:
                connection.execute(
                    "CREATE TABLE workspace_settings (workspace_id VARCHAR(36) NOT NULL, "
                    '"key" VARCHAR(120) NOT NULL, value_json JSON NOT NULL, revision INTEGER NOT NULL DEFAULT 1, '
                    'updated_at DATETIME NOT NULL, PRIMARY KEY (workspace_id, "key"), '
                    f"CONSTRAINT {CURRENT_FK} FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT)"
                )
    pattern = "history index" if damage == "history_index" else "unexpected"
    _assert_upgrade_failure_is_atomic(path, pattern)


@pytest.mark.parametrize(
    ("table_name", "index_sql", "keep_target_tables"),
    [
        (
            CURRENT,
            "CREATE UNIQUE INDEX private_lower_key ON app_settings (lower(key))",
            False,
        ),
        (
            HISTORY,
            "CREATE UNIQUE INDEX private_history_lower_key ON app_settings_history (lower(key))",
            False,
        ),
        (
            WORKSPACE_CURRENT,
            "CREATE UNIQUE INDEX workspace_lower_key "
            "ON workspace_settings (lower(key)) WHERE key IS NOT NULL",
            True,
        ),
        (
            WORKSPACE_HISTORY,
            "CREATE UNIQUE INDEX workspace_history_lower_key "
            "ON workspace_settings_history (lower(key)) WHERE key IS NOT NULL",
            True,
        ),
    ],
)
def test_expression_unique_indexes_fail_preflight_without_reflection_warnings(
    schema_at_0071: Path,
    tmp_path: Path,
    table_name: str,
    index_sql: str,
    keep_target_tables: bool,
) -> None:
    path = tmp_path / f"settings-expression-unique-{table_name}.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("private.key", '{"raw":true}', 1, NOW)],
        keep_target_tables=keep_target_tables,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(index_sql)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        _assert_upgrade_failure_is_atomic(path, "unexpected unique index")


def test_target_rows_with_dangling_workspace_or_host_key_fail_preflight(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    for case in ("dangling", "host"):
        path = tmp_path / f"settings-invalid-target-{case}.sqlite"
        _legacy_database(
            schema_at_0071,
            path,
            current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
            keep_target_tables=True,
        )
        with sqlite3.connect(path) as connection:
            if case == "dangling":
                _insert_workspace_current(connection, "missing-workspace", "early", "{}")
            else:
                _owner_id, workspace_id = _owner_workspace(connection)
                _insert_workspace_current(connection, workspace_id, "web.preferences", "{}")
        _assert_upgrade_failure_is_atomic(
            path,
            "foreign key is invalid" if case == "dangling" else "host-only keys",
        )


def test_personal_target_owner_graph_is_structurally_validated(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-invalid-target-owner.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
        keep_target_tables=True,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users "
            "(id, legacy_subject, display_name, is_active, is_instance_admin, created_at, updated_at) "
            "VALUES ('fixture-user', NULL, 'Fixture', 0, 0, ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
            "VALUES ('fixture-personal', 'personal', 'Fixture', 'fixture-user', 'active', 1, ?, ?)",
            (NOW, NOW),
        )
        _insert_workspace_current(connection, "fixture-personal", "early", "{}")
    _assert_upgrade_failure_is_atomic(path, "personal workspace owner graph")


@pytest.mark.parametrize("damage", ["missing_owner", "missing_owner_membership"])
def test_damaged_initial_seed_fails_before_transfer(
    schema_at_0071: Path, tmp_path: Path, damage: str
) -> None:
    path = tmp_path / f"settings-invalid-seed-{damage}.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
    )
    with sqlite3.connect(path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        if damage == "missing_owner":
            connection.execute("UPDATE users SET legacy_subject = NULL WHERE id = ?", (owner_id,))
        else:
            connection.execute(
                "DELETE FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, owner_id),
            )
    _assert_upgrade_failure_is_atomic(path)


@pytest.mark.parametrize("collision", ["current", "history"])
def test_private_target_collisions_fail_before_upgrade(
    schema_at_0071: Path, tmp_path: Path, collision: str
) -> None:
    path = tmp_path / f"settings-target-collision-{collision}.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"source":true}', 2, NOW)],
        history_rows=[(84, "legacy.history", '{"source":true}', 3, UPDATED)],
        keep_target_tables=True,
    )
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        if collision == "current":
            _insert_workspace_current(connection, workspace_id, "legacy.setting", '{"target":true}')
        else:
            _insert_workspace_history(
                connection, 84, workspace_id, "other.key", '{"target":true}', 1, NOW
            )
    _assert_upgrade_failure_is_atomic(path, "already exists")


@pytest.mark.parametrize("damage", ["default", "unique", "missing_history_index"])
def test_malformed_legacy_settings_schema_fails_before_transfer(
    schema_at_0071: Path, tmp_path: Path, damage: str
) -> None:
    path = tmp_path / f"settings-malformed-old-{damage}.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
        current_revision_default=damage == "default",
        current_unique=damage == "unique",
    )
    if damage == "missing_history_index":
        with sqlite3.connect(path) as connection:
            connection.execute("DROP INDEX ix_app_settings_history_key")
    _assert_upgrade_failure_is_atomic(path)


def test_unexpected_legacy_fk_or_trigger_fails_before_transfer(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    for damage in ("inbound_fk", "trigger"):
        path = tmp_path / f"settings-legacy-{damage}.sqlite"
        _legacy_database(
            schema_at_0071,
            path,
            current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
        )
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            if damage == "inbound_fk":
                connection.execute(
                    "CREATE TABLE settings_reference (setting_key TEXT REFERENCES app_settings(key))"
                )
            else:
                connection.execute(
                    "CREATE TRIGGER app_settings_guard AFTER DELETE ON app_settings "
                    "BEGIN SELECT 1; END"
                )
        _assert_upgrade_failure_is_atomic(path)


def test_late_failure_after_transfer_rolls_back_complete_database_and_allows_retry(
    schema_at_0071: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings-late-rollback.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", ' { "raw" : [ 1, null ] } ', 2, NOW)],
        history_rows=[(91, "legacy.orphan", '{"raw":true}', -2, UPDATED)],
    )
    from alembic.util import pyfiles

    original_loader = pyfiles.load_module_py

    def load_and_fail_after_transfer(module_id: str, module_path: str | Path) -> Any:
        module = original_loader(module_id, module_path)
        if Path(module_path).name == "0072_workspace_settings.py":

            def fail_after_transfer(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("injected post-transfer failure")

            vars(module)["_validate_postconditions"] = fail_after_transfer
        return module

    monkeypatch.setattr(pyfiles, "load_module_py", load_and_fail_after_transfer)
    _assert_upgrade_failure_is_atomic(path, "injected post-transfer failure")
    monkeypatch.setattr(pyfiles, "load_module_py", original_loader)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
    assert _current_rows(path, workspace_id=workspace_id) == [
        (workspace_id, "legacy.setting", ' { "raw" : [ 1, null ] } ', 2, NOW)
    ]
    assert _history_rows(path, workspace_id=workspace_id) == [
        (91, workspace_id, "legacy.orphan", '{"raw":true}', -2, UPDATED)
    ]


def test_repeat_upgrade_with_no_private_source_rows_is_safe(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-repeat-upgrade.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"raw":true}', 1, NOW)],
    )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
    before = (
        _current_rows(path, workspace_id=workspace_id),
        _history_rows(path, workspace_id=workspace_id),
    )
    import importlib

    migration = importlib.import_module("pandrator.web.migrations.versions.0072_workspace_settings")
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine(sqlite_url(path))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
    finally:
        engine.dispose()
    assert before == (
        _current_rows(path, workspace_id=workspace_id),
        _history_rows(path, workspace_id=workspace_id),
    )


@pytest.mark.parametrize("refusal", ["other_workspace", "current_collision", "history_collision"])
def test_downgrade_refusals_preserve_exact_schema_data_and_version(
    schema_at_0071: Path, tmp_path: Path, refusal: str
) -> None:
    path = tmp_path / f"settings-downgrade-refusal-{refusal}.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("web.preferences", '{"host":true}', 1, NOW)],
        history_rows=[(4, "system.identity", '{"host-history":true}', 1, UPDATED)],
    )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        if refusal == "other_workspace":
            _create_team(connection, "team-workspace")
            _insert_workspace_current(connection, "team-workspace", "team.key", "{}")
        elif refusal == "current_collision":
            connection.execute(
                "INSERT INTO app_settings (key, value_json, revision, updated_at) "
                "VALUES ('legacy.collision', '{}', 1, ?)",
                (NOW,),
            )
            _insert_workspace_current(connection, workspace_id, "legacy.collision", "{}")
        else:
            connection.execute(
                "INSERT INTO app_settings_history (id, key, value_json, revision, created_at) "
                "VALUES (990, 'legacy.collision', '{}', 1, ?)",
                (NOW,),
            )
            _insert_workspace_history(
                connection, 990, workspace_id, "legacy.collision", "{}", 1, NOW
            )
    message = {
        "other_workspace": "non-initial workspace history",
        "current_collision": "current key collision",
        "history_collision": "history ID collision",
    }[refusal]
    _assert_downgrade_failure_is_atomic(path, message)


def test_target_history_has_no_current_setting_fk_and_preserves_orphans(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-history-orphans.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        history_rows=[(4, "removed.setting", '{"old":true}', 3, NOW)],
    )
    command.upgrade(_config(path), REVISION)
    engine = sa.create_engine(sqlite_url(path))
    try:
        foreign_keys = sa.inspect(engine).get_foreign_keys(WORKSPACE_HISTORY)
    finally:
        engine.dispose()
    assert [foreign_key["referred_table"] for foreign_key in foreign_keys] == ["workspaces"]
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        assert connection.execute(
            "SELECT key FROM workspace_settings_history WHERE id = 4 AND workspace_id = ?",
            (workspace_id,),
        ).fetchone() == ("removed.setting",)


def test_workspace_foreign_keys_restrict_delete_with_current_and_history_rows(
    schema_at_0071: Path, tmp_path: Path
) -> None:
    path = tmp_path / "settings-workspace-restrict.sqlite"
    _legacy_database(
        schema_at_0071,
        path,
        current_rows=[("legacy.setting", '{"current":true}', 1, NOW)],
        history_rows=[(44, "legacy.setting", '{"history":true}', 1, UPDATED)],
    )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _owner_id, workspace_id = _owner_workspace(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
