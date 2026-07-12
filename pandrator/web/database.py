"""Database engine, sessions, and Alembic bootstrap helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


SCHEMA_HEAD = "0003_parity_workspace"


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
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA busy_timeout = 30000")
        cursor.close()

    return engine


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.engine = create_database_engine(path)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def upgrade_database(path: Path) -> None:
    if path.is_file():
        try:
            connection = sqlite3.connect(path)
            row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            connection.close()
            if row and row[0] == SCHEMA_HEAD:
                return
        except sqlite3.DatabaseError:
            pass
    migrations = Path(__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    command.upgrade(config, "head")
