"""Only rollback-safe SQLite contention is retried, with fresh transactions."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pandrator.web.database import Database, is_sqlite_busy


def _error(code: int) -> OperationalError:
    original = sqlite3.OperationalError("synthetic contention")
    original.sqlite_errorcode = code
    return OperationalError("synthetic", {}, original)


@pytest.fixture
def database(tmp_path: Path):
    value = Database(tmp_path / "retry.sqlite3")
    with value.immediate_session() as unit:
        unit.execute(text("CREATE TABLE effects (id INTEGER PRIMARY KEY, value TEXT)"))
    try:
        yield value
    finally:
        value.dispose()


@pytest.mark.parametrize("code,expected", [
    (sqlite3.SQLITE_BUSY, True),
    (sqlite3.SQLITE_BUSY_SNAPSHOT, True),
    (sqlite3.SQLITE_LOCKED, False),
    (sqlite3.SQLITE_LOCKED_SHAREDCACHE, False),
    (sqlite3.SQLITE_IOERR, False),
])
def test_busy_classification_distinguishes_connection_conflicts(code, expected):
    assert is_sqlite_busy(_error(code)) is expected


def test_retry_rolls_back_before_reexecuting_sql_in_a_fresh_session(database):
    attempts: list[Session] = []

    def operation(unit: Session) -> str:
        attempts.append(unit)
        assert unit.scalar(text("SELECT count(*) FROM effects")) == 0
        unit.execute(text("INSERT INTO effects VALUES (1, 'one committed effect')"))
        if len(attempts) == 1:
            raise _error(sqlite3.SQLITE_BUSY_SNAPSHOT)
        return "accepted"

    assert database.run_immediate(operation) == "accepted"
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    with database.snapshot_session() as unit:
        assert unit.scalar(text("SELECT count(*) FROM effects")) == 1


@pytest.mark.parametrize("code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR])
def test_noncontention_errors_are_rolled_back_without_retry(database, code):
    attempts = 0

    def operation(unit: Session) -> None:
        nonlocal attempts
        attempts += 1
        unit.execute(text("INSERT INTO effects VALUES (1, 'rolled back')"))
        raise _error(code)

    with pytest.raises(OperationalError):
        database.run_immediate(operation)
    assert attempts == 1
    with database.snapshot_session() as unit:
        assert unit.scalar(text("SELECT count(*) FROM effects")) == 0


def test_actual_writer_contention_retries_begin_then_commits_once(database):
    locked, release = threading.Event(), threading.Event()
    attempts = []

    def record_begin(_connection, _cursor, statement, _parameters, _context, _many):
        if statement == "BEGIN IMMEDIATE":
            attempts.append(statement)

    def writer():
        with closing(sqlite3.connect(database.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked.set()
            assert release.wait(5)
            connection.rollback()

    thread = threading.Thread(target=writer)
    timer = threading.Timer(0.2, release.set)
    event.listen(database.engine, "before_cursor_execute", record_begin)
    thread.start()
    try:
        assert locked.wait(5)
        timer.start()
        database.run_immediate(
            lambda unit: unit.execute(text("INSERT INTO effects VALUES (1, 'single')")),
            busy_timeout_ms=20, timeout_seconds=2, max_attempts=20,
        )
        assert len(attempts) >= 2
        with database.snapshot_session() as unit:
            assert unit.scalar(text("SELECT count(*) FROM effects")) == 1
    finally:
        release.set()
        timer.cancel()
        thread.join(5)
        event.remove(database.engine, "before_cursor_execute", record_begin)
    assert not thread.is_alive()


def test_attempt_and_time_budget_terminate_retryable_errors(database):
    attempts = 0

    def operation(_unit: Session) -> None:
        nonlocal attempts
        attempts += 1
        raise _error(sqlite3.SQLITE_BUSY)

    started = time.monotonic()
    with pytest.raises(OperationalError):
        database.run_immediate(operation, timeout_seconds=0.05, max_attempts=3)
    assert 1 <= attempts <= 3
    assert time.monotonic() - started < 1


def test_custom_busy_timeout_includes_commit_and_is_restored(database):
    observed = []

    def committing(unit: Session) -> None:
        observed.append(unit.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one())

    event.listen(database._factory, "before_commit", committing)
    try:
        with database.session(busy_timeout_ms=25) as unit:
            unit.execute(text("INSERT INTO effects VALUES (1, 'commit')"))
    finally:
        event.remove(database._factory, "before_commit", committing)
    assert observed == [25]
    with database.snapshot_session() as unit:
        assert unit.scalar(text("SELECT value FROM effects WHERE id=1")) == "commit"
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000


def test_timed_session_rolls_back_and_restores_connection_after_failure(database):
    with pytest.raises(ValueError, match="abort"):
        with database.session(busy_timeout_ms=25) as unit:
            unit.execute(text("INSERT INTO effects VALUES (1, 'rolled back')"))
            raise ValueError("abort")
    with database.snapshot_session() as unit:
        assert unit.scalar(text("SELECT count(*) FROM effects")) == 0
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000


@pytest.mark.parametrize("arguments", [
    {"timeout_seconds": 0}, {"busy_timeout_ms": -1}, {"max_attempts": 0},
])
def test_invalid_retry_budget_fails_before_callback(database, arguments):
    with pytest.raises(ValueError):
        database.run_immediate(lambda _unit: pytest.fail("must not run"), **arguments)
