"""The write-ahead log shrinks back once load ends, and no read pins it open.

Tests count bytes, frames and checkpoint results; none asserts wall-clock time.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import text

from pandrator.web import database as database_module
from pandrator.web import generation_search
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import WAL_SIZE_LIMIT_BYTES, Database

WIDE = "x" * 3000
MAX_GROW_ROUNDS = 2000


def _wal_bytes(database: Database) -> int:
    wal = Path(f"{database.path}-wal")
    return wal.stat().st_size if wal.exists() else 0


def _make(path: Path) -> Database:
    value = Database(path)
    with value.immediate_session() as unit:
        unit.execute(text("CREATE TABLE effects (id INTEGER PRIMARY KEY, value TEXT)"))
    return value


def _write(database: Database, count: int) -> None:
    for _ in range(count):
        with database.immediate_session() as unit:
            unit.execute(text("INSERT INTO effects (value) VALUES (:v)"), {"v": WIDE})


def _grow_under_reader(database: Database, target_bytes: int) -> None:
    with database.snapshot_session() as reader:
        reader.execute(text("SELECT count(*) FROM effects")).scalar_one()
        # Each round adds at least 40 KiB, so the bound is far past any target used here.
        for _ in range(MAX_GROW_ROUNDS):
            if _wal_bytes(database) >= target_bytes:
                return
            _write(database, 10)
    pytest.fail(f"the WAL stayed at {_wal_bytes(database)} bytes, short of {target_bytes}")


def test_new_connection_reports_the_size_limit(tmp_path):
    database = _make(tmp_path / "limit.sqlite3")
    try:
        with database.engine.connect() as connection:
            value = connection.exec_driver_sql("PRAGMA journal_size_limit").scalar_one()
        assert value == WAL_SIZE_LIMIT_BYTES
    finally:
        database.dispose()


def test_wal_shrinks_to_the_limit_after_readers_finish(tmp_path, monkeypatch):
    limit = 64 * 1024
    monkeypatch.setattr(database_module, "WAL_SIZE_LIMIT_BYTES", limit)
    database = _make(tmp_path / "shrink.sqlite3")
    try:
        _grow_under_reader(database, 5 * 1024 * 1024)
        assert _wal_bytes(database) > limit * 8
        _write(database, 2)
        assert _wal_bytes(database) <= limit
    finally:
        database.dispose()


def test_wal_under_the_limit_is_not_truncated(tmp_path):
    database = _make(tmp_path / "cycling.sqlite3")
    try:
        _grow_under_reader(database, 5 * 1024 * 1024)
        before = _wal_bytes(database)
        assert before < WAL_SIZE_LIMIT_BYTES
        # The first write appends and auto-checkpoints; the second restarts the log.
        _write(database, 1)
        after_first = _wal_bytes(database)
        assert after_first >= before
        _write(database, 1)
        assert _wal_bytes(database) == after_first
    finally:
        database.dispose()


def test_segment_search_does_not_hold_a_statement_open_across_matching(tmp_path):
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    headers = {
        "X-CSRF-Token": client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
    }
    services = app.extensions["pandrator"]
    database = services["database"]
    try:
        session_id = client.post(
            "/api/v1/sessions",
            json={"name": "WAL search", "workflow_kind": "audiobook"},
            headers=headers,
        ).get_json()["id"]
        services["generation"].create_plan(
            session_id,
            source_revision_id=None,
            segments=[{"text": f"Row {index}: marker"} for index in range(12)],
        )
        url = f"/api/v1/sessions/{session_id}/generation-segments"
        baseline = client.get(url, query_string={"q": "marker"}).get_json()["items"]
        assert len(baseline) == 12

        # Leave frames in the WAL, so a reader still open has a nonzero mark.
        with closing(sqlite3.connect(database.path, timeout=30)) as other:
            other.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
            other.execute("INSERT INTO probe VALUES ('seed')")
            other.commit()

        original = generation_search.literal_matches
        observed: list[tuple[int, int, int]] = []

        def wrapped(*args, **kwargs):
            if not observed:
                with closing(sqlite3.connect(database.path, timeout=30)) as other:
                    other.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
                    other.execute("INSERT INTO probe VALUES ('x')")
                    other.commit()
                    observed.append(
                        tuple(other.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
                    )
            return original(*args, **kwargs)

        with mock.patch.object(generation_search, "literal_matches", wrapped):
            patched = client.get(url, query_string={"q": "marker"}).get_json()["items"]
        assert observed, "literal_matches was never called"
        _busy, log, checkpointed = observed[0]
        assert log == checkpointed
        assert patched == baseline
    finally:
        database.dispose()
