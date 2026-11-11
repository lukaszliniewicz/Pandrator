"""Database engine, sessions, and Alembic bootstrap helpers."""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TypeVar

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

SCHEMA_HEAD = "0087_public_project_links"
_WriteResult = TypeVar("_WriteResult")


def is_sqlite_busy(error: BaseException) -> bool:
    """Recognize SQLite writer contention without hiding other SQL errors."""
    original = getattr(error, "orig", error)
    code = getattr(original, "sqlite_errorcode", None)
    return (
        isinstance(original, sqlite3.Error)
        and isinstance(code, int)
        and (code & 0xFF) == sqlite3.SQLITE_BUSY
    )


@contextmanager
def _scoped_busy_timeout(connection: Connection, busy_timeout_ms: int | None) -> Iterator[None]:
    if busy_timeout_ms is None:
        yield
        return
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be nonnegative")
    previous = int(connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one())
    connection.exec_driver_sql(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    try:
        yield
    finally:
        connection.exec_driver_sql(f"PRAGMA busy_timeout = {previous}")


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def create_database_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        sqlite_url(path),
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA busy_timeout = 30000")
            finally:
                cursor.close()
        except BaseException:
            dbapi_connection.close()
            raise

    return engine


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.engine = create_database_engine(path)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self, *, busy_timeout_ms: int | None = None) -> Iterator[Session]:
        # Keep explicitly timed connections open through commit and timeout
        # restoration. A Session-owned connection closes at commit.
        connection = self.engine.connect() if busy_timeout_ms is not None else None
        session = (
            self._factory(bind=connection, join_transaction_mode="control_fully")
            if connection is not None else self._factory()
        )
        try:
            if connection is None:
                yield session
                session.commit()
            else:
                with _scoped_busy_timeout(connection, busy_timeout_ms):
                    yield session
                    session.flush()
                    session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            if connection is not None:
                connection.close()

    @contextmanager
    def snapshot_session(self) -> Iterator[Session]:
        """Read one SQLite snapshot; finish it before opening a queue writer.

        SQLite's legacy transaction mode does not issue BEGIN for SELECTs.
        Explicit BEGIN keeps all reads on the first observed database version,
        while WAL still lets concurrent writers commit. Never write here.
        """
        connection = self.engine.connect()
        session: Session | None = None
        try:
            connection.exec_driver_sql("BEGIN")
            session = Session(bind=connection, expire_on_commit=False, future=True)
            yield session
        finally:
            if session is not None:
                session.close()
            connection.rollback()
            connection.close()

    @contextmanager
    def immediate_session(self, *, busy_timeout_ms: int | None = None) -> Iterator[Session]:
        """Begin a short SQLite write transaction before reading mutable state."""
        connection = self.engine.connect()
        session: Session | None = None
        try:
            with _scoped_busy_timeout(connection, busy_timeout_ms):
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                session = Session(bind=connection, expire_on_commit=False, future=True)
                yield session
                session.flush()
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if session is not None:
                session.close()
            connection.close()

    def dispose(self) -> None:
        self.engine.dispose()

    def run_immediate(
        self,
        operation: Callable[[Session], _WriteResult],
        *,
        timeout_seconds: float = 2.0,
        busy_timeout_ms: int = 100,
        max_attempts: int = 8,
    ) -> _WriteResult:
        """Retry an explicitly rollback-safe SQL unit in fresh transactions.

        The callback must contain only transactional database effects: never
        provider requests, file writes, publication, sleeps or caller responses.
        A BUSY_SNAPSHOT also needs a fresh snapshot; LOCKED is a connection or
        shared-cache conflict and must be diagnosed instead of blindly retried.
        Each failed attempt is fully rolled back before bounded jittered backoff.
        This deadline bounds contention waits, not arbitrary callback CPU time.
        """
        if timeout_seconds <= 0 or busy_timeout_ms < 0 or max_attempts < 1:
            raise ValueError("Invalid SQLite write retry budget.")
        deadline = time.monotonic() + timeout_seconds
        for attempt in range(max_attempts):
            remaining = max(0.0, deadline - time.monotonic())
            wait_ms = min(busy_timeout_ms, int(remaining * 1000))
            try:
                with self.immediate_session(busy_timeout_ms=wait_ms) as session:
                    result = operation(session)
                return result
            except Exception as error:
                remaining = deadline - time.monotonic()
                if (
                    not is_sqlite_busy(error)
                    or attempt + 1 >= max_attempts
                    or remaining <= 0
                ):
                    raise
                delay = random.uniform(0.0, min(0.025 * 2**attempt, 0.2, remaining))
                time.sleep(delay)
        raise AssertionError("SQLite retry loop exhausted without a result.")


def upgrade_database(path: Path) -> None:
    if path.is_file():
        try:
            with closing(sqlite3.connect(path)) as connection:
                row = connection.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                ).fetchone()
            if row and row[0] == SCHEMA_HEAD:
                return
        except sqlite3.DatabaseError:
            pass
    migrations = Path(__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations).replace("%", "%%"))
    # Alembic uses ConfigParser interpolation, but database paths may contain %.
    config.set_main_option("sqlalchemy.url", sqlite_url(path).replace("%", "%%"))
    command.upgrade(config, "head")
