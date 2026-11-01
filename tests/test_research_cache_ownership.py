from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from pandrator.web.database import create_database_engine, sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0067_research_cache_scope"
PREVIOUS_REVISION = "0066_job_ownership"
NOW = "2026-10-23 13:20:00"
EXPIRES = "2026-11-02 13:20:00"
TABLE = "research_cache_entries"
CACHE_COLUMNS = (
    "cache_key",
    "provider",
    "operation",
    "request_json",
    "response_json",
    "created_at",
    "expires_at",
)
OLD_INDEX_NAMES = {
    "ix_research_cache_entries_provider",
    "ix_research_cache_entries_operation",
    "ix_research_cache_entries_expires_at",
}
WORKSPACE_INDEX = "ix_research_cache_entries_workspace_id"
PROJECT_INDEX = "ix_research_cache_entries_project_id"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0066(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("research-cache-base") / "at-0066.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _connect(path: Path) -> Engine:
    return create_database_engine(path)


def _sqlite_mutate(path: Path, callback: Any) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        callback(connection)
        connection.commit()
    finally:
        connection.close()


def _legacy_cache_database(schema_at_0066: Path, path: Path) -> None:
    shutil.copy2(schema_at_0066, path)

    def rebuild(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE research_cache_entries_legacy ("
            "cache_key VARCHAR(64) NOT NULL, "
            "provider VARCHAR(40) NOT NULL, "
            "operation VARCHAR(40) NOT NULL, "
            "request_json JSON NOT NULL, "
            "response_json JSON NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "expires_at DATETIME NOT NULL, "
            "PRIMARY KEY (cache_key))"
        )
        selected = ", ".join(f'"{name}"' for name in CACHE_COLUMNS)
        connection.execute(
            f"INSERT INTO research_cache_entries_legacy ({selected}) "
            f"SELECT {selected} FROM research_cache_entries"
        )
        connection.execute("DROP TABLE research_cache_entries")
        connection.execute(
            "ALTER TABLE research_cache_entries_legacy RENAME TO research_cache_entries"
        )
        connection.execute(
            "CREATE INDEX ix_research_cache_entries_provider ON research_cache_entries (provider)"
        )
        connection.execute(
            "CREATE INDEX ix_research_cache_entries_operation ON research_cache_entries (operation)"
        )
        connection.execute(
            "CREATE INDEX ix_research_cache_entries_expires_at "
            "ON research_cache_entries (expires_at)"
        )

    _sqlite_mutate(path, rebuild)


def _owner_workspace(connection: Connection) -> tuple[str, str]:
    row = connection.execute(
        sa.text(
            "SELECT users.id, workspaces.id FROM users JOIN workspaces "
            "ON workspaces.personal_owner_user_id = users.id "
            "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
        )
    ).one()
    return str(row[0]), str(row[1])


def _insert_cache(
    connection: Connection,
    cache_key: str,
    *,
    provider: str = "provider-test",
    operation: str = "search",
    request_json: str = '{"query":"example"}',
    response_json: str = '{"items":[1]}',
    created_at: str = NOW,
    expires_at: str = EXPIRES,
    workspace_id: str | None = None,
    project_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO research_cache_entries "
            "(cache_key, provider, operation, request_json, response_json, created_at, expires_at) "
            "VALUES (:cache_key, :provider, :operation, :request_json, :response_json, "
            ":created_at, :expires_at)"
        ),
        {
            "cache_key": cache_key,
            "provider": provider,
            "operation": operation,
            "request_json": request_json,
            "response_json": response_json,
            "created_at": created_at,
            "expires_at": expires_at,
        },
    )
    if workspace_id is not None or project_id is not None:
        connection.execute(
            sa.text(
                "UPDATE research_cache_entries SET workspace_id = :workspace_id, "
                "project_id = :project_id WHERE cache_key = :cache_key"
            ),
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "cache_key": cache_key,
            },
        )


def _cache_row(connection: Connection, cache_key: str) -> tuple[Any, ...]:
    return tuple(
        connection.execute(
            sa.text(
                "SELECT cache_key, workspace_id, project_id, provider, operation, "
                "request_json, response_json, created_at, expires_at "
                "FROM research_cache_entries WHERE cache_key = :cache_key"
            ),
            {"cache_key": cache_key},
        ).one()
    )


def _database_snapshot(connection: Connection) -> tuple[Any, ...]:
    objects = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).all()
    tables = (
        connection.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        .scalars()
        .all()
    )
    data = []
    for table_name in tables:
        quoted_table = '"' + str(table_name).replace('"', '""') + '"'
        columns = connection.exec_driver_sql(f"PRAGMA table_info({quoted_table})").all()
        order_by = ", ".join('"' + str(column[1]).replace('"', '""') + '"' for column in columns)
        rows = connection.exec_driver_sql(f"SELECT * FROM {quoted_table} ORDER BY {order_by}").all()
        data.append((table_name, tuple(tuple(row) for row in rows)))
    version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    return tuple(objects), tuple(data), version


def _version(connection: Connection) -> str:
    return str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())


def _index_sql(snapshot: tuple[Any, ...]) -> dict[str, str | None]:
    return {
        name: sql
        for object_type, name, table_name, sql in snapshot[0]
        if object_type == "index" and table_name == TABLE
    }


def _insert_project(connection: Connection, project_id: str, workspace_id: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO projects "
            "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
            "created_at, updated_at) VALUES (:id, :workspace_id, NULL, :name, NULL, 1, :now, :now)"
        ),
        {
            "id": project_id,
            "workspace_id": workspace_id,
            "name": project_id,
            "now": NOW,
        },
    )


def test_legacy_rows_adopt_only_initial_personal_workspace_and_preserve_cache_data(
    schema_at_0066: Path, tmp_path: Path
) -> None:
    path = tmp_path / "legacy-cache.sqlite"
    _legacy_cache_database(schema_at_0066, path)
    engine = _connect(path)
    cache_key = "  legacy opaque cache key  "
    request_json = ' { "workspace_id" : "decoy", "project_id" : "decoy", "query" : "x" } '
    response_json = ' { "items" : [ "kept", { "url" : "https://example.invalid/" } ] } '
    try:
        with engine.begin() as connection:
            _insert_cache(
                connection,
                cache_key,
                provider="provider-original",
                operation="operation-original",
                request_json=request_json,
                response_json=response_json,
                created_at="2025-01-02 03:04:05.000000",
                expires_at="2025-02-03 04:05:06.000000",
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_research_cache_custom_provider_expiry "
                "ON research_cache_entries (provider, expires_at DESC) "
                "WHERE provider != 'provider-never'"
            )
            owner_id, workspace_id = _owner_workspace(connection)
            before = _database_snapshot(connection)
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            row = _cache_row(connection, cache_key)
            assert row == (
                cache_key,
                workspace_id,
                None,
                "provider-original",
                "operation-original",
                request_json,
                response_json,
                "2025-01-02 03:04:05.000000",
                "2025-02-03 04:05:06.000000",
            )
            assert _version(connection) == REVISION
            assert _index_sql(_database_snapshot(connection))[
                "ix_research_cache_custom_provider_expiry"
            ] == (
                "CREATE INDEX ix_research_cache_custom_provider_expiry "
                "ON research_cache_entries (provider, expires_at DESC) "
                "WHERE provider != 'provider-never'"
            )
            assert owner_id
            assert before[2] == PREVIOUS_REVISION
    finally:
        engine.dispose()


def test_empty_early_target_metadata_backfills_only_null_scope(
    schema_at_0066: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-cache-target.sqlite"
    shutil.copy2(schema_at_0066, path)
    engine = _connect(path)
    cache_key = "early-target-cache-key"
    request_json = ' { "query" : "unchanged" } '
    response_json = ' { "results" : [ 3, 2, 1 ] } '
    try:
        with engine.begin() as connection:
            _insert_cache(
                connection,
                cache_key,
                request_json=request_json,
                response_json=response_json,
            )
            owner_id, workspace_id = _owner_workspace(connection)
            columns = {item["name"] for item in sa.inspect(connection).get_columns(TABLE)}
            assert {"workspace_id", "project_id"} <= columns
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _cache_row(connection, cache_key) == (
                cache_key,
                workspace_id,
                None,
                "provider-test",
                "search",
                request_json,
                response_json,
                NOW,
                EXPIRES,
            )
            assert owner_id
            assert _version(connection) == REVISION
            assert {
                index["name"] for index in sa.inspect(connection).get_indexes(TABLE)
            } >= OLD_INDEX_NAMES | {WORKSPACE_INDEX, PROJECT_INDEX}
            foreign_keys = {
                foreign_key["name"]: (foreign_key.get("options") or {}).get("ondelete")
                for foreign_key in sa.inspect(connection).get_foreign_keys(TABLE)
            }
            assert foreign_keys == {
                "fk_research_cache_entries_workspace_id_workspaces": "RESTRICT",
                "fk_research_cache_entries_project_id_projects": "CASCADE",
            }
    finally:
        engine.dispose()


def test_project_scope_fk_cascades_only_derived_cache_row(
    schema_at_0066: Path, tmp_path: Path
) -> None:
    path = tmp_path / "cache-project-cascade.sqlite"
    _legacy_cache_database(schema_at_0066, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_cache(connection, "legacy-row")
            _owner_id, workspace_id = _owner_workspace(connection)
        command.upgrade(_config(path), REVISION)
        with engine.begin() as connection:
            _insert_project(connection, "cache-cascade-project", workspace_id)
            _insert_cache(
                connection,
                "derived-project-row",
                workspace_id=workspace_id,
                project_id="cache-cascade-project",
            )
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            connection.execute(sa.text("DELETE FROM projects WHERE id = 'cache-cascade-project'"))
            keys = (
                connection.execute(
                    sa.text("SELECT cache_key FROM research_cache_entries ORDER BY cache_key")
                )
                .scalars()
                .all()
            )
            assert keys == ["legacy-row"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("invalid_owner", ["inactive", "membership"])
def test_invalid_seed_or_membership_fails_before_ddl(
    schema_at_0066: Path, tmp_path: Path, invalid_owner: str
) -> None:
    path = tmp_path / f"invalid-cache-owner-{invalid_owner}.sqlite"
    _legacy_cache_database(schema_at_0066, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_cache(connection, "preserved-invalid-owner-row")
            owner_id, workspace_id = _owner_workspace(connection)
            if invalid_owner == "inactive":
                connection.execute(
                    sa.text("UPDATE users SET is_active = 0 WHERE id = :owner_id"),
                    {"owner_id": owner_id},
                )
            else:
                connection.execute(
                    sa.text(
                        "UPDATE workspace_memberships SET role = 'member' "
                        "WHERE workspace_id = :workspace_id AND user_id = :owner_id"
                    ),
                    {"workspace_id": workspace_id, "owner_id": owner_id},
                )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("malformation", ["partial", "wrong_type", "populated"])
def test_partial_malformed_or_populated_target_fails_atomically(
    schema_at_0066: Path, tmp_path: Path, malformation: str
) -> None:
    path = tmp_path / f"bad-cache-target-{malformation}.sqlite"
    if malformation == "populated":
        shutil.copy2(schema_at_0066, path)
        engine = _connect(path)
        try:
            with engine.begin() as connection:
                _insert_cache(connection, "populated-target-row")
                _owner_id, workspace_id = _owner_workspace(connection)
                connection.execute(
                    sa.text(
                        "UPDATE research_cache_entries SET workspace_id = :workspace_id "
                        "WHERE cache_key = 'populated-target-row'"
                    ),
                    {"workspace_id": workspace_id},
                )
        finally:
            engine.dispose()
    else:
        _legacy_cache_database(schema_at_0066, path)

        def malformed(connection: sqlite3.Connection) -> None:
            if malformation == "partial":
                connection.execute(
                    "ALTER TABLE research_cache_entries ADD COLUMN workspace_id VARCHAR(36) "
                    "REFERENCES workspaces(id) ON DELETE RESTRICT"
                )
                connection.execute(
                    "CREATE INDEX ix_research_cache_entries_workspace_id "
                    "ON research_cache_entries (workspace_id)"
                )
            else:
                connection.execute(
                    "ALTER TABLE research_cache_entries ADD COLUMN workspace_id VARCHAR(20) "
                    "REFERENCES workspaces(id) ON DELETE RESTRICT"
                )
                connection.execute(
                    "ALTER TABLE research_cache_entries ADD COLUMN project_id VARCHAR(36) "
                    "REFERENCES projects(id) ON DELETE CASCADE"
                )
                connection.execute(
                    "CREATE INDEX ix_research_cache_entries_workspace_id "
                    "ON research_cache_entries (workspace_id)"
                )
                connection.execute(
                    "CREATE INDEX ix_research_cache_entries_project_id "
                    "ON research_cache_entries (project_id)"
                )

        _sqlite_mutate(path, malformed)

    engine = _connect(path)
    try:
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_late_failure_restores_exact_database_then_retry_repeat_and_downgrade(
    schema_at_0066: Path, tmp_path: Path
) -> None:
    path = tmp_path / "cache-late-rollback.sqlite"
    _legacy_cache_database(schema_at_0066, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_cache(
                connection,
                "cache-late-a",
                request_json=' { "a" : 1 } ',
                response_json=' { "cached" : "a" } ',
            )
            _insert_cache(
                connection,
                "cache-late-b",
                request_json=' { "b" : 2 } ',
                response_json=' { "cached" : "b" } ',
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_research_cache_custom_expiry "
                "ON research_cache_entries (expires_at DESC, operation)"
            )
            before = _database_snapshot(connection)
        before_indexes = _index_sql(before)
        updated_rows = 0

        def fail_after_first_backfill(
            connection: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            nonlocal updated_rows
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("update research_cache_entries set workspace_id"):
                updated_rows += 1
                if updated_rows == 2:
                    raise RuntimeError("injected late cache backfill failure")

        event.listen(Engine, "before_cursor_execute", fail_after_first_backfill)
        try:
            with pytest.raises(RuntimeError, match="injected late cache backfill failure"):
                command.upgrade(_config(path), REVISION)
        finally:
            event.remove(Engine, "before_cursor_execute", fail_after_first_backfill)

        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
            assert not connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).first()

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            _owner_id, workspace_id = _owner_workspace(connection)
            for key in ("cache-late-a", "cache-late-b"):
                row = _cache_row(connection, key)
                assert row[1:3] == (workspace_id, None)
            upgraded = _database_snapshot(connection)
            upgraded_indexes = _index_sql(upgraded)
            assert {name: upgraded_indexes[name] for name in before_indexes} == before_indexes
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == upgraded

        command.downgrade(_config(path), PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert _version(connection) == PREVIOUS_REVISION
            assert [column["name"] for column in sa.inspect(connection).get_columns(TABLE)] == list(
                CACHE_COLUMNS
            )
            after_downgrade = _database_snapshot(connection)
            assert after_downgrade[1:] == before[1:]
            assert tuple(obj for obj in after_downgrade[0] if obj[2] != TABLE) == tuple(
                obj for obj in before[0] if obj[2] != TABLE
            )
            assert _index_sql(after_downgrade) == before_indexes
            assert not {WORKSPACE_INDEX, PROJECT_INDEX} & set(_index_sql(after_downgrade))
    finally:
        engine.dispose()
