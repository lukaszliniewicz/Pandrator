"""Native SQLite probes close before legacy fallback reads or marker effects."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from pandrator.runtime import DataPaths
from pandrator.web import legacy_migration as migration
from pandrator.web.database import SCHEMA_HEAD

NATIVE_CONNECT = sqlite3.connect
NATIVE_ITERDIR = Path.iterdir


@dataclass
class HeldConnections:
    values: list[sqlite3.Connection] = field(default_factory=list)

    def connect(self, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = NATIVE_CONNECT(*args, **kwargs)
        self.values.append(connection)
        return connection

    def assert_closed(self) -> None:
        assert len(self.values) == 1
        for connection in self.values:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")


@pytest.fixture
def held() -> Iterator[HeldConnections]:
    connections = HeldConnections()
    try:
        yield connections
    finally:
        # Explicit native retirement also runs for the expected before failures;
        # strong references rule out garbage-collection close as the witness.
        for connection in connections.values:
            connection.close()


@pytest.mark.parametrize("table_case", ["dict", "missing", "invalid_json"])
def test_settings_probe_closes_before_native_json_fallback(
    tmp_path: Path, held: HeldConnections, table_case: str
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    table_settings = {"text": {"language": "pl"}, "audio": {"sample_rate": 24000}}
    fallback_settings = {"text": {"language": "ja"}, "audio": {"sample_rate": 22050}}
    fallback = paths.root / "global_settings.json"
    fallback.write_text(json.dumps({"settings": fallback_settings, "revision": 7}))
    with closing(NATIVE_CONNECT(paths.legacy_database)) as connection:
        if table_case != "missing":
            connection.execute(
                "CREATE TABLE app_settings_current (singleton_id INTEGER, payload_json TEXT)"
            )
            connection.execute(
                "INSERT INTO app_settings_current VALUES (?, ?)",
                (1, json.dumps(table_settings) if table_case == "dict" else "invalid fixture JSON"),
            )
            connection.commit()
    native_load = migration._load_json
    reads: list[Path] = []

    def load(path: Path) -> Any:
        if path == fallback:
            held.assert_closed()
            reads.append(path)
        return native_load(path)

    with (
        mock.patch.object(migration.sqlite3, "connect", side_effect=held.connect),
        mock.patch.object(migration, "_load_json", side_effect=load),
    ):
        result = migration._legacy_settings(paths)
    assert result == (table_settings if table_case == "dict" else fallback_settings)
    assert reads == ([] if table_case == "dict" else [fallback])
    held.assert_closed()


@pytest.mark.parametrize("missing_table", [False, True])
def test_rows_probe_closes_before_native_output_directory_fallback(
    tmp_path: Path, held: HeldConnections, missing_table: bool
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    outputs = paths.legacy_outputs
    matching, extra, trash = (outputs / name for name in ("matching", "extra", ".trash"))
    for directory in (matching, extra, trash / "trashed"):
        directory.mkdir(parents=True)
    (outputs / "readme.txt").write_text("disposable fixture")
    active_row = {
        "session_name": "matching",
        "session_path": str(matching),
        "status": "running",
        "trashed_at": None,
        "fixture_notes": "active row",
    }
    with closing(NATIVE_CONNECT(paths.legacy_database)) as connection:
        if not missing_table:
            connection.execute(
                "CREATE TABLE sessions (session_name TEXT, session_path TEXT, status TEXT, "
                "trashed_at TEXT, fixture_notes TEXT)"
            )
            connection.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                [
                    tuple(active_row.values()),
                    ("trashed", str(trash / "trashed"), "trashed", "fixture", "trashed row"),
                ],
            )
            connection.commit()
    expected = [] if missing_table else [active_row]
    for child in NATIVE_ITERDIR(outputs):
        if (
            child.is_dir()
            and child.name != ".trash"
            and (missing_table or child.name != "matching")
        ):
            expected.append(
                {"session_name": child.name, "session_path": str(child), "status": "idle"}
            )
    reads: list[Path] = []

    def iterdir(path: Path) -> Iterator[Path]:
        if path == outputs:
            held.assert_closed()
            reads.append(path)
        return NATIVE_ITERDIR(path)

    with (
        mock.patch.object(migration.sqlite3, "connect", side_effect=held.connect),
        mock.patch.object(Path, "iterdir", iterdir),
    ):
        result = migration._legacy_session_rows(paths)
    assert result == expected
    assert reads == [outputs]
    held.assert_closed()


@pytest.mark.parametrize("schema", [SCHEMA_HEAD, "fixture-old-schema", None])
def test_existing_marker_probe_closes_before_backup_and_upgrade_effects(
    tmp_path: Path, held: HeldConnections, schema: str | None
) -> None:
    # Real marker/schema read; backup/upgrade are controlled effect witnesses.
    # This protocol does not claim a complete legacy migration or Alembic upgrade.
    paths = DataPaths.from_value(tmp_path).ensure()
    marker = {
        "version": migration.MIGRATION_VERSION,
        "generation_promotion_version": migration.GENERATION_PROMOTION_VERSION,
        "web_schema": SCHEMA_HEAD,
        "status": "complete",
        "promoted_generation_segments": 0,
        "counts": {"sessions": 3},
    }
    paths.migration_marker.write_text(json.dumps(marker))
    with closing(NATIVE_CONNECT(paths.database)) as connection:
        if schema is not None:
            connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
            connection.execute("INSERT INTO alembic_version VALUES (?)", (schema,))
            connection.commit()
    effects: list[str] = []

    def backup(value: DataPaths) -> Path:
        assert value is paths
        held.assert_closed()
        effects.append("backup")
        return paths.backups / "fixture-controlled-backup"

    def upgrade(value: Path) -> None:
        assert value == paths.database
        held.assert_closed()
        effects.append("upgrade")

    with (
        mock.patch.object(migration.sqlite3, "connect", side_effect=held.connect),
        mock.patch.object(migration, "create_metadata_backup", side_effect=backup) as backup_call,
        mock.patch.object(migration, "upgrade_database", side_effect=upgrade) as upgrade_call,
        mock.patch.object(migration, "Database") as database,
        mock.patch.object(migration, "_atomic_json") as marker_write,
    ):
        result = migration.import_legacy_data(paths)
    assert result == marker
    assert json.loads(paths.migration_marker.read_text()) == marker
    assert effects == (["upgrade"] if schema == SCHEMA_HEAD else ["backup", "upgrade"])
    assert backup_call.call_count == (0 if schema == SCHEMA_HEAD else 1)
    upgrade_call.assert_called_once_with(paths.database)
    database.assert_not_called()
    marker_write.assert_not_called()
    held.assert_closed()
