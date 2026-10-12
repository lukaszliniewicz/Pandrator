"""Schema inspection must release its SQLite handle before migration fallback."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from pandrator.web import database


def _engine_with_creator(path: Path, creator: Callable[[], sqlite3.Connection]):
    native_create_engine = database.create_engine

    def create_engine_with_creator(url, *args, **kwargs):
        return native_create_engine(url, *args, creator=creator, **kwargs)

    with mock.patch.object(database, "create_engine", side_effect=create_engine_with_creator):
        return database.create_database_engine(path)


def test_startup_schema_head_matches_packaged_migrations() -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(database.__file__).with_name("migrations"))
    )
    assert database.SCHEMA_HEAD == ScriptDirectory.from_config(config).get_current_head()


@pytest.mark.parametrize("state", ["current", "stale", "unversioned", "invalid"])
def test_schema_probe_closes_before_return_or_migration(tmp_path: Path, state: str) -> None:
    path = tmp_path / "probe.sqlite3"
    if state == "invalid":
        path.write_bytes(b"not a SQLite database")
    else:
        connection = sqlite3.connect(path)
        try:
            if state != "unversioned":
                connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
                connection.execute(
                    "INSERT INTO alembic_version VALUES (?)",
                    (database.SCHEMA_HEAD if state == "current" else "older_revision",),
                )
                connection.commit()
        finally:
            connection.close()
    source = path.read_bytes()
    native_connect = sqlite3.connect
    held: list[sqlite3.Connection] = []

    def connect(*args, **kwargs) -> sqlite3.Connection:
        result = native_connect(*args, **kwargs)
        held.append(result)
        return result

    def assert_closed() -> None:
        assert len(held) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            held[0].execute("SELECT 1")

    def migrate(*_args, **_kwargs) -> None:
        # Observe the actual native probe before migration starts. The migration
        # itself is substituted, keeping this test about inspection ownership.
        assert_closed()

    try:
        with (
            mock.patch.object(database.sqlite3, "connect", side_effect=connect),
            mock.patch.object(database.command, "upgrade", side_effect=migrate) as upgrade,
        ):
            database.upgrade_database(path)
            assert_closed()
            assert upgrade.call_count == (0 if state == "current" else 1)
            if state != "current":
                assert upgrade.call_args.args[1] == "head"
        assert path.read_bytes() == source
    finally:
        for connection in held:
            connection.close()


@pytest.mark.parametrize(
    "failure_point",
    [
        "cursor",
        "PRAGMA foreign_keys = ON",
        "PRAGMA journal_mode = WAL",
        "PRAGMA busy_timeout = 30000",
        "cursor.close",
    ],
)
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_sqlite_configuration_failure_closes_native_connection_and_cursor(
    tmp_path: Path, failure_point: str, error_type: type[BaseException]
) -> None:
    path = tmp_path / "configuration-failure.sqlite3"
    native_connect = sqlite3.connect
    failure = error_type(f"controlled {failure_point} failure")
    connections: list[sqlite3.Connection] = []
    cursors: list[sqlite3.Cursor] = []
    configuration_cursors: list[sqlite3.Cursor] = []
    explicitly_closed: list[sqlite3.Cursor] = []
    cursor_acquisitions = 0
    configuration_statements = {
        "PRAGMA foreign_keys = ON",
        "PRAGMA journal_mode = WAL",
        "PRAGMA busy_timeout = 30000",
    }

    class ControlledCursor(sqlite3.Cursor):
        is_configuration_cursor = False

        def execute(self, statement, parameters=()):
            if statement in configuration_statements:
                if not self.is_configuration_cursor:
                    self.is_configuration_cursor = True
                    configuration_cursors.append(self)
                if statement == failure_point:
                    raise failure
            return super().execute(statement, parameters)

        def close(self):
            explicitly_closed.append(self)
            super().close()
            if failure_point == "cursor.close" and self.is_configuration_cursor:
                raise failure

    class ControlledConnection(sqlite3.Connection):
        def cursor(self, factory=sqlite3.Cursor):
            nonlocal cursor_acquisitions
            cursor_acquisitions += 1
            # The SQLite dialect opens one PRAGMA cursor before this listener.
            if failure_point == "cursor" and cursor_acquisitions == 2:
                raise failure
            cursor = super().cursor(ControlledCursor)
            cursors.append(cursor)
            return cursor

    def creator() -> sqlite3.Connection:
        connection = native_connect(
            path,
            check_same_thread=False,
            timeout=30,
            factory=ControlledConnection,
        )
        connections.append(connection)
        return connection

    engine = _engine_with_creator(path, creator)
    try:
        with pytest.raises(error_type) as caught:
            engine.connect()
        assert caught.value is failure
        assert len(connections) == 1
        if failure_point == "cursor":
            assert cursor_acquisitions == 2
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
        for cursor in cursors:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                cursor.execute("SELECT 1")
        if failure_point in {
            "PRAGMA foreign_keys = ON",
            "PRAGMA journal_mode = WAL",
            "PRAGMA busy_timeout = 30000",
            "cursor.close",
        }:
            assert len(configuration_cursors) == 1
            assert configuration_cursors[0] in explicitly_closed
    finally:
        engine.dispose()
        for connection in connections:
            connection.close()


def test_sqlite_configuration_closes_cursor_and_keeps_valid_connection_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "configuration-success.sqlite3"
    native_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []
    cursors: list[sqlite3.Cursor] = []

    configuration_statements = {
        "PRAGMA foreign_keys = ON",
        "PRAGMA journal_mode = WAL",
        "PRAGMA busy_timeout = 30000",
    }

    class ObservedCursor(sqlite3.Cursor):
        is_configuration_cursor = False

        def execute(self, statement, parameters=()):
            if statement in configuration_statements:
                self.is_configuration_cursor = True
            return super().execute(statement, parameters)

        def close(self):
            if self.is_configuration_cursor:
                cursors.append(self)
            super().close()

    class ObservedConnection(sqlite3.Connection):
        def cursor(self, factory=sqlite3.Cursor):
            return super().cursor(ObservedCursor)

    def creator() -> sqlite3.Connection:
        connection = native_connect(
            path,
            check_same_thread=False,
            timeout=30,
            factory=ObservedConnection,
        )
        connections.append(connection)
        return connection

    engine = _engine_with_creator(path, creator)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        assert len(connections) == 1
        assert len(cursors) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            cursors[0].execute("SELECT 1")
        assert connections[0].execute("SELECT 1").fetchone() == (1,)
    finally:
        engine.dispose()
        for connection in connections:
            connection.close()
