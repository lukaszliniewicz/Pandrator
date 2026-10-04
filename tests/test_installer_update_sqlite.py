"""SQLite backup ownership on success and interrupted preparation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pandrator_installer.update import snapshot_sqlite


@pytest.mark.parametrize("failure", [None, "destination", "backup"])
def test_sqlite_snapshot_closes_every_opened_connection(
    tmp_path: Path, failure: str | None
) -> None:
    source, destination = tmp_path / "source.db", tmp_path / "snapshot.db"
    native_connect = sqlite3.connect
    with closing(native_connect(source)) as connection, connection:
        connection.execute("CREATE TABLE witness(value TEXT)")
        connection.execute("INSERT INTO witness VALUES ('original')")
    connections: list[sqlite3.Connection] = []

    class InterruptedBackup(sqlite3.Connection):
        def backup(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("backup sentinel")

    def connect(path: Path, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if failure == "destination" and path == destination:
            raise sqlite3.OperationalError("destination sentinel")
        if failure == "backup" and path == source:
            kwargs["factory"] = InterruptedBackup
        opened = native_connect(path, *args, **kwargs)
        connections.append(opened)
        return opened

    try:
        with patch("sqlite3.connect", side_effect=connect):
            if failure:
                with pytest.raises((RuntimeError, sqlite3.OperationalError), match="sentinel"):
                    snapshot_sqlite(source, destination)
            else:
                assert snapshot_sqlite(source, destination)
        assert len(connections) == (1 if failure == "destination" else 2)
        for opened in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                opened.execute("SELECT 1")
        if failure is None:
            with closing(native_connect(destination)) as connection:
                assert connection.execute("SELECT value FROM witness").fetchone() == ("original",)
    finally:
        for opened in connections:
            opened.close()
