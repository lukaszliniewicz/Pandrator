"""Schema inspection must release its SQLite handle before migration fallback."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from pandrator.web import database


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
