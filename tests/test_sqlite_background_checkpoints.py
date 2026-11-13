"""Commits can leave WAL checkpoints to a background thread."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from sqlalchemy import text

from pandrator.web.database import BACKGROUND_AUTOCHECKPOINT_PAGES, Database

# A single commit larger than SQLite's default 1000-page autocheckpoint threshold.
LARGE_COMMIT_BYTES = 1200 * 4096


def _blob_table(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, body BLOB)")
    # Closing the last connection checkpoints the table into the database file.


def _insert(database: Database, size: int) -> None:
    with database.immediate_session() as unit:
        unit.execute(text("INSERT INTO blobs (body) VALUES (randomblob(:size))"), {"size": size})


def _rows_in_database_file(path: Path) -> int | None:
    """Count the rows a reader finds in the database file alone, ignoring the WAL."""
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)) as reader:
            return reader.execute("SELECT count(*) FROM blobs").fetchone()[0]
    except sqlite3.DatabaseError:
        # A checkpoint may be writing the page this reader needs.
        return None


def _checkpoint_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "pandrator-wal-checkpoint"]


def test_large_commit_checkpoints_inline_by_default(tmp_path: Path) -> None:
    path = tmp_path / "inline.sqlite3"
    _blob_table(path)
    database = Database(path)
    try:
        _insert(database, LARGE_COMMIT_BYTES)
        assert _rows_in_database_file(path) == 1
        assert _checkpoint_threads() == []
    finally:
        database.dispose()


def test_background_checkpoints_keep_large_commits_from_checkpointing(tmp_path: Path) -> None:
    path = tmp_path / "background.sqlite3"
    _blob_table(path)
    database = Database(path, checkpoint_interval=3600)
    try:
        _insert(database, LARGE_COMMIT_BYTES)
        assert _rows_in_database_file(path) == 0
        with database.session() as unit:
            assert unit.execute(text("SELECT count(*) FROM blobs")).scalar_one() == 1
            assert (
                unit.execute(text("PRAGMA wal_autocheckpoint")).scalar_one()
                == BACKGROUND_AUTOCHECKPOINT_PAGES
            )
    finally:
        database.dispose()


def test_background_thread_copies_commits_into_the_database_file(tmp_path: Path) -> None:
    path = tmp_path / "copied.sqlite3"
    _blob_table(path)
    database = Database(path, checkpoint_interval=0.05)
    try:
        assert len(_checkpoint_threads()) == 1
        _insert(database, 16)
        deadline = time.monotonic() + 10
        while _rows_in_database_file(path) != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _rows_in_database_file(path) == 1
    finally:
        database.dispose()
    assert _checkpoint_threads() == []
