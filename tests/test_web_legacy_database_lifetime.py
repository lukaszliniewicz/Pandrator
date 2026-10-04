"""Legacy import owns its database through promotion and failed-import cleanup."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.pool import Pool

from pandrator.runtime import DataPaths
from pandrator.web import legacy_migration
from pandrator.web.database import SCHEMA_HEAD, Database, upgrade_database
from pandrator.web.models import SessionRecord


@dataclass
class CapturedDatabase:
    database: Database
    connection: sqlite3.Connection
    pool: Pool

    def assert_retired(self) -> None:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            self.connection.execute("SELECT 1")
        assert self.database.engine.pool is not self.pool


@pytest.fixture
def owners() -> Iterator[list[CapturedDatabase]]:
    held: list[CapturedDatabase] = []
    try:
        yield held
    finally:
        for owner in held:
            owner.database.dispose()
            owner.connection.close()


def capture_database(path: Path, owners: list[CapturedDatabase]) -> Database:
    database = Database(path)
    with database.engine.connect() as checkout:
        checkout.exec_driver_sql("SELECT 1")
        connection = checkout.connection.driver_connection
        assert isinstance(connection, sqlite3.Connection)
    assert connection.execute("SELECT 1").fetchone() == (1,)
    owners.append(CapturedDatabase(database, connection, database.engine.pool))
    return database


def promotion_paths(root: Path) -> DataPaths:
    paths = DataPaths.from_value(root).ensure()
    upgrade_database(paths.database)
    legacy = paths.legacy_outputs / "Promotion fixture"
    legacy.mkdir(parents=True)
    database = Database(paths.database)
    try:
        with database.session() as session:
            session.add(SessionRecord(name="Promotion fixture", legacy_path=str(legacy)))
    finally:
        database.dispose()
    paths.migration_marker.write_text(
        json.dumps(
            {
                "version": legacy_migration.MIGRATION_VERSION,
                "status": "complete",
                "web_schema": SCHEMA_HEAD,
                "generation_promotion_version": 0,
            }
        ),
        encoding="utf-8",
    )
    return paths


def test_malformed_provider_import_retires_database_before_failed_file_removal(
    tmp_path: Path, owners: list[CapturedDatabase]
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    paths.root.joinpath("global_settings.json").write_text(
        json.dumps({"llm": {"provider_configs": [{"provider": "fixture", "models": 1}]}}),
        encoding="utf-8",
    )
    native_unlink = Path.unlink
    removed: list[Path] = []

    def unlink(path: Path, *args, **kwargs) -> None:
        if path == paths.database:
            assert len(owners) == 1
            owners[0].assert_retired()
            removed.append(path)
        native_unlink(path, *args, **kwargs)

    with (
        patch.object(
            legacy_migration, "Database", side_effect=lambda path: capture_database(path, owners)
        ),
        patch.object(Path, "unlink", new=unlink),
    ):
        with pytest.raises(TypeError, match="not iterable"):
            legacy_migration.import_legacy_data(paths)
    assert removed == [paths.database]
    owners[0].assert_retired()
    assert not paths.database.exists()
    assert not paths.migration_marker.exists()
    backups = list(paths.backups.glob("pre-web-*"))
    assert len(backups) == 1
    assert (
        backups[0].joinpath("global_settings.json").read_bytes()
        == paths.root.joinpath("global_settings.json").read_bytes()
    )


def test_interrupted_fresh_import_retires_database_without_changing_cleanup_policy(
    tmp_path: Path, owners: list[CapturedDatabase]
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    sentinel = KeyboardInterrupt("legacy settings interrupted")
    with (
        patch.object(
            legacy_migration, "Database", side_effect=lambda path: capture_database(path, owners)
        ),
        patch.object(legacy_migration, "_legacy_settings", side_effect=sentinel),
    ):
        with pytest.raises(KeyboardInterrupt) as caught:
            legacy_migration.import_legacy_data(paths)
        assert caught.value is sentinel
    assert len(owners) == 1
    owners[0].assert_retired()
    assert paths.database.exists()
    assert not paths.migration_marker.exists()


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_promotion_failure_retires_database_and_preserves_marker_and_stored_rows(
    tmp_path: Path, owners: list[CapturedDatabase], failure_type: type[BaseException]
) -> None:
    paths = promotion_paths(tmp_path)
    marker = paths.migration_marker.read_bytes()
    before = Database(paths.database)
    try:
        with before.engine.connect() as connection:
            native = connection.connection.driver_connection
            assert isinstance(native, sqlite3.Connection)
            dump = list(native.iterdump())
    finally:
        before.dispose()
    sentinel = failure_type("promotion interrupted")
    with (
        patch.object(
            legacy_migration, "Database", side_effect=lambda path: capture_database(path, owners)
        ),
        patch.object(legacy_migration, "_import_generation", side_effect=sentinel),
    ):
        with pytest.raises(failure_type) as caught:
            legacy_migration.import_legacy_data(paths)
        assert caught.value is sentinel
    assert len(owners) == 1
    owners[0].assert_retired()
    assert paths.migration_marker.read_bytes() == marker
    with closing(sqlite3.connect(paths.database)) as connection:
        assert list(connection.iterdump()) == dump


@pytest.mark.parametrize("promotion", [False, True])
def test_native_import_success_retires_database_before_marker_publication(
    tmp_path: Path, owners: list[CapturedDatabase], promotion: bool
) -> None:
    paths = promotion_paths(tmp_path) if promotion else DataPaths.from_value(tmp_path).ensure()
    native_atomic_json = legacy_migration._atomic_json
    published: list[dict] = []

    def atomic_json(path: Path, payload: dict) -> None:
        if path == paths.migration_marker:
            assert len(owners) == 1
            owners[0].assert_retired()
            published.append(payload.copy())
        native_atomic_json(path, payload)

    with (
        patch.object(
            legacy_migration, "Database", side_effect=lambda path: capture_database(path, owners)
        ),
        patch.object(legacy_migration, "_atomic_json", side_effect=atomic_json),
    ):
        result = legacy_migration.import_legacy_data(paths)
    assert len(owners) == 1
    owners[0].assert_retired()
    assert published == [result]
    assert json.loads(paths.migration_marker.read_text(encoding="utf-8")) == result
    assert result["generation_promotion_version"] == legacy_migration.GENERATION_PROMOTION_VERSION
    assert result["web_schema"] == SCHEMA_HEAD
    if promotion:
        assert result["promoted_generation_segments"] == 0
    else:
        assert {
            key: result[key] for key in ("status", "sessions", "segments", "artifacts", "providers")
        } == {
            "status": "complete",
            "sessions": 0,
            "segments": 0,
            "artifacts": 0,
            "providers": 0,
        }


@pytest.mark.parametrize(
    "state,expected",
    [
        (None, ("audiobook", "auto", None, "custom", [])),
        ([], ("audiobook", "auto", None, "custom", [])),
        (42, ("audiobook", "auto", None, "custom", [])),
        ({}, ("audiobook", "auto", None, "custom", [])),
        (
            {"workflow": None, "original_language": "pl", "target_language": "en"},
            ("audiobook", "pl", "en", "custom", []),
        ),
        (
            {
                "workflow": {
                    "workflow_kind": "voiceover",
                    "source_language": "ja",
                    "target_language": "pl",
                    "workflow_preset": "fixture",
                    "included_stages": ["transcribe", "translate"],
                }
            },
            ("voiceover", "ja", "pl", "fixture", ["transcribe", "translate"]),
        ),
    ],
)
def test_native_legacy_session_settings_projection_remains_compatible(
    tmp_path: Path, state: object, expected: tuple
) -> None:
    paths = DataPaths.from_value(tmp_path).ensure()
    legacy = paths.legacy_outputs / "Settings fixture"
    legacy.mkdir(parents=True)
    legacy.joinpath("session_config.json").write_text(
        json.dumps({"state": state}), encoding="utf-8"
    )
    result = legacy_migration.import_legacy_data(paths)
    assert (result["sessions"], result["segments"], result["artifacts"], result["providers"]) == (
        1,
        0,
        0,
        0,
    )
    database = Database(paths.database)
    try:
        from sqlalchemy import select

        with database.session() as session:
            record = session.scalar(select(SessionRecord))
            assert record is not None
            assert record.name == "Settings fixture"
            assert record.legacy_path == str(legacy)
            assert (
                record.workflow_kind,
                record.source_language,
                record.target_language,
                record.workflow_preset,
                record.included_stages_json,
            ) == expected
    finally:
        database.dispose()
