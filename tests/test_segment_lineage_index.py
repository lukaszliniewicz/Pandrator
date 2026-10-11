"""The segment-lineage child lookup has a dedicated, reversible index."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pandrator.web.database import SCHEMA_HEAD, create_database_engine, sqlite_url, upgrade_database
from pandrator.web.models import (
    Base,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
)

MIGRATION_HEAD = "0057_segment_lineage_child_index"
PREVIOUS_REVISION = "0056_project_sound_plans"
INDEX_NAME = "ix_segment_lineage_child_segment_id"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "pandrator/web/migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _index_names(engine: Engine, table_name: str) -> set[str]:
    names: set[str] = set()
    for index in inspect(engine).get_indexes(table_name):
        name = index["name"]
        assert name is not None
        names.add(name)
    return names


def _assert_child_index(engine: Engine) -> None:
    indexes = inspect(engine).get_indexes("segment_lineage")
    matches = [index for index in indexes if index["name"] == INDEX_NAME]
    assert len(matches) == 1
    assert matches[0]["column_names"] == ["child_segment_id"]
    assert not matches[0]["unique"]


def _create_0056_fixture(path: Path) -> Engine:
    command.upgrade(_config(path), PREVIOUS_REVISION)
    engine = create_database_engine(path)
    # Early baseline migrations use current metadata's create_all; remove this
    # new index to model a database that was already at 0056 before this change.
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    with Session(engine) as session:
        session.add(SessionRecord(id="session-fixture", name="Lineage fixture"))
        session.flush()
        session.add(
            Document(
                id="document-fixture",
                session_id="session-fixture",
                stage="transcription",
                language="en",
            )
        )
        session.flush()
        session.add(
            DocumentRevision(
                id="revision-fixture",
                document_id="document-fixture",
                revision_number=1,
                content_hash="content-hash-fixture",
            )
        )
        session.flush()
        session.add_all(
            [
                Segment(
                    id="segment-parent",
                    revision_id="revision-fixture",
                    ordinal=1,
                    text="parent",
                ),
                Segment(
                    id="segment-child",
                    revision_id="revision-fixture",
                    ordinal=2,
                    text="child",
                ),
                Segment(
                    id="segment-other-parent",
                    revision_id="revision-fixture",
                    ordinal=3,
                    text="other parent",
                ),
                Segment(
                    id="segment-other-child",
                    revision_id="revision-fixture",
                    ordinal=4,
                    text="other child",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                SegmentLineage(
                    parent_segment_id="segment-parent",
                    child_segment_id="segment-child",
                    relation="derived",
                    sequence=7,
                ),
                SegmentLineage(
                    parent_segment_id="segment-other-parent",
                    child_segment_id="segment-other-child",
                    relation="unrelated",
                    sequence=11,
                ),
            ]
        )
        session.commit()
    return engine


def _fixture_rows(engine: Engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    table_names = ("documents", "document_revisions", "segments", "segment_lineage")
    with engine.connect() as connection:
        return {
            table_name: tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    f"SELECT * FROM {table_name} ORDER BY rowid"
                ).all()
            )
            for table_name in table_names
        }


def _all_indexes(engine: Engine) -> dict[str, tuple[tuple[str, tuple[str, ...], bool], ...]]:
    inspector = inspect(engine)
    result: dict[str, tuple[tuple[str, tuple[str, ...], bool], ...]] = {}
    for table_name in inspector.get_table_names():
        indexes: list[tuple[str, tuple[str, ...], bool]] = []
        for index in inspector.get_indexes(table_name):
            name = index["name"]
            columns = index["column_names"]
            assert name is not None
            assert all(column is not None for column in columns)
            indexes.append(
                (
                    name,
                    tuple(column for column in columns if column is not None),
                    bool(index["unique"]),
                )
            )
        result[table_name] = tuple(sorted(indexes))
    return result


def test_fresh_upgrade_and_metadata_schema_have_child_index(tmp_path: Path) -> None:
    assert SCHEMA_HEAD == MIGRATION_HEAD

    upgraded_path = tmp_path / "upgraded.sqlite3"
    upgrade_database(upgraded_path)
    upgraded_engine = create_database_engine(upgraded_path)
    try:
        _assert_child_index(upgraded_engine)
    finally:
        upgraded_engine.dispose()

    metadata_path = tmp_path / "metadata.sqlite3"
    metadata_engine = create_database_engine(metadata_path)
    try:
        Base.metadata.create_all(metadata_engine)
        _assert_child_index(metadata_engine)
    finally:
        metadata_engine.dispose()


def test_upgrade_preserves_lineage_and_child_delete_cascades(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite3"
    engine = _create_0056_fixture(path)
    try:
        original_rows = _fixture_rows(engine)
        command.upgrade(_config(path), "head")
        _assert_child_index(engine)
        assert _fixture_rows(engine) == original_rows

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                plan = connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT parent_segment_id FROM segment_lineage "
                    "WHERE child_segment_id = ?",
                    ("segment-child",),
                ).all()
                assert any(INDEX_NAME in str(row[3]) for row in plan)

                connection.execute(
                    delete(Segment).where(Segment.id == "segment-child")
                )
                remaining = connection.execute(
                    select(
                        SegmentLineage.parent_segment_id,
                        SegmentLineage.child_segment_id,
                    )
                ).all()
                assert remaining == [("segment-other-parent", "segment-other-child")]
            finally:
                transaction.rollback()

        assert _fixture_rows(engine) == original_rows
    finally:
        engine.dispose()


def test_downgrade_reupgrade_preserves_rows_and_other_indexes(tmp_path: Path) -> None:
    path = tmp_path / "roundtrip.sqlite3"
    engine = _create_0056_fixture(path)
    try:
        original_rows = _fixture_rows(engine)
        original_indexes = _all_indexes(engine)

        command.upgrade(_config(path), "head")
        _assert_child_index(engine)
        command.downgrade(_config(path), PREVIOUS_REVISION)
        assert INDEX_NAME not in _index_names(engine, "segment_lineage")
        assert _all_indexes(engine) == original_indexes
        assert _fixture_rows(engine) == original_rows

        command.upgrade(_config(path), "head")
        _assert_child_index(engine)
        command.upgrade(_config(path), "head")
        _assert_child_index(engine)
        assert _fixture_rows(engine) == original_rows
    finally:
        engine.dispose()
