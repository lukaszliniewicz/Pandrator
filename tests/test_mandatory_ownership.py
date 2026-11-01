from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, event

from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    AppSettingHistory,
    Artifact,
    Job,
    Project,
    ProjectFolder,
    ProjectFolderMembership,
    PronunciationEntry,
    Provider,
    ResearchCacheEntry,
    SessionRecord,
    SessionSettingHistory,
    SoundItem,
    SoundVariant,
    SourceAsset,
    TrainingRun,
    TranslationProject,
    UploadSessionRecord,
    UsageEvent,
    Voice,
    VoiceCollection,
    VoiceCollectionMember,
    VoiceSample,
    Workspace,
    WorkspaceSettingHistory,
)
from pandrator.web.projects.identity import session_project

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
PREVIOUS_REVISION = "0074_actor_attribution"
REVISION = "0075_mandatory_ownership"

ROOTS: tuple[tuple[str, str, str, str], ...] = (
    ("providers", "workspace_id", "workspaces", "RESTRICT"),
    ("sessions", "project_id", "projects", "RESTRICT"),
    ("translation_projects", "project_root_id", "projects", "RESTRICT"),
    ("project_folders", "workspace_id", "workspaces", "RESTRICT"),
    ("project_folder_memberships", "project_id", "projects", "CASCADE"),
    ("source_assets", "workspace_id", "workspaces", "RESTRICT"),
    ("jobs", "workspace_id", "workspaces", "RESTRICT"),
    ("artifacts", "workspace_id", "workspaces", "RESTRICT"),
    ("upload_sessions", "workspace_id", "workspaces", "RESTRICT"),
    ("research_cache_entries", "workspace_id", "workspaces", "RESTRICT"),
    ("pronunciation_entries", "workspace_id", "workspaces", "RESTRICT"),
    ("voices", "workspace_id", "workspaces", "RESTRICT"),
    ("voice_samples", "workspace_id", "workspaces", "RESTRICT"),
    ("training_runs", "workspace_id", "workspaces", "RESTRICT"),
    ("usage_events", "workspace_id", "workspaces", "RESTRICT"),
    ("voice_collections", "workspace_id", "workspaces", "RESTRICT"),
    ("voice_collection_members", "workspace_id", "workspaces", "RESTRICT"),
    ("sound_items", "workspace_id", "workspaces", "RESTRICT"),
    ("sound_variants", "workspace_id", "workspaces", "RESTRICT"),
)
ROOT_TABLES = tuple(table for table, *_rest in ROOTS)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0074(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mandatory-ownership-base") / "at-0074.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    assert _version(path) == PREVIOUS_REVISION
    _assert_root_shapes(path, nullable=True)
    return path


@pytest.fixture
def populated_0074(schema_at_0074: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "populated-0074.sqlite"
    _copy_sqlite(schema_at_0074, path)
    return path, _populate_valid_graph(path)


def _copy_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _pragma_rows(
    connection: sqlite3.Connection, pragma: str, identifier: str
) -> list[tuple[Any, ...]]:
    return connection.execute(f"PRAGMA {pragma}({_quote(identifier)})").fetchall()


def _assert_root_shapes(path: Path, *, nullable: bool) -> None:
    with sqlite3.connect(path) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table_name, column_name, parent_table, ondelete in ROOTS:
            assert table_name in table_names
            assert parent_table in table_names
            column = next(
                row
                for row in _pragma_rows(connection, "table_info", table_name)
                if str(row[1]) == column_name
            )
            assert (int(column[3]) == 0) is nullable
            assert column[4] is None
            foreign_keys = [
                row
                for row in _pragma_rows(connection, "foreign_key_list", table_name)
                if str(row[3]) == column_name
            ]
            assert len(foreign_keys) == 1
            foreign_key = foreign_keys[0]
            assert str(foreign_key[2]) == parent_table
            assert str(foreign_key[4]) == "id"
            assert str(foreign_key[6]).upper() == ondelete


def _populate_valid_graph(path: Path) -> dict[str, str]:
    ids = {
        name: str(uuid.uuid4())
        for name in (
            "workspace",
            "project",
            "session",
            "artifact",
            "job",
            "folder",
            "source_asset",
            "provider",
            "upload",
            "research_cache",
            "pronunciation",
            "voice",
            "voice_sample",
            "training",
            "usage_event",
            "voice_collection",
            "sound_item",
            "sound_variant",
            "translation_project",
        )
    }
    now = datetime.now(timezone.utc)
    database = Database(path)
    try:
        with database.session() as session:
            workspace = Workspace(id=ids["workspace"], kind="team", name="Migration test workspace")
            session.add(workspace)
            session.flush()

            project = Project(
                id=ids["project"],
                workspace_id=workspace.id,
                name="Retained project",
            )
            session.add(project)
            session.flush()

            project_session = SessionRecord(
                id=ids["session"],
                project_id=project.id,
                name="Retained session",
                storage_key=str(uuid.uuid4()),
            )
            session.add(project_session)
            session.flush()
            project.source_session_id = project_session.id
            session.flush()

            artifact = Artifact(
                id=ids["artifact"],
                workspace_id=workspace.id,
                project_id=project.id,
                session_id=project_session.id,
                kind="source",
                role="checkpoint",
                relative_path="migration-test/checkpoint.json",
                metadata_json={"retained": True},
            )
            job = Job(
                id=ids["job"],
                workspace_id=workspace.id,
                project_id=project.id,
                session_id=project_session.id,
                kind="migration.test",
                payload_json={"retained": True},
            )
            folder = ProjectFolder(
                id=ids["folder"],
                workspace_id=workspace.id,
                name="Retained folder",
            )
            source_asset = SourceAsset(
                id=ids["source_asset"],
                workspace_id=workspace.id,
                display_name="Retained source",
                kind="document",
                metadata_json={"origin": "fixture"},
            )
            provider = Provider(
                id=ids["provider"],
                workspace_id=workspace.id,
                provider_key="fixture-provider",
                label="Fixture provider",
            )
            session.add_all([artifact, job, folder, source_asset, provider])
            session.flush()

            voice = Voice(
                id=ids["voice"],
                workspace_id=workspace.id,
                name="Retained voice",
                language="en",
                metadata_json={"retained": True},
            )
            sound_item = SoundItem(
                id=ids["sound_item"],
                workspace_id=workspace.id,
                name="Retained sound",
                tags_json=["fixture"],
            )
            collection = VoiceCollection(
                id=ids["voice_collection"],
                workspace_id=workspace.id,
                name="Retained collection",
            )
            session.add_all([voice, sound_item, collection])
            session.flush()

            session.add_all(
                [
                    ProjectFolderMembership(
                        folder_id=folder.id,
                        project_session_id=project_session.id,
                        project_id=project.id,
                        position=0,
                    ),
                    UploadSessionRecord(
                        id=ids["upload"],
                        workspace_id=workspace.id,
                        project_id=project.id,
                        session_id=project_session.id,
                        filename="retained.wav",
                        size_bytes=3,
                        chunk_size=3,
                        chunk_count=1,
                        temporary_relative_path="uploads/retained.wav",
                        expires_at=now + timedelta(hours=1),
                    ),
                    ResearchCacheEntry(
                        cache_key="a" * 64,
                        workspace_id=workspace.id,
                        provider="fixture",
                        operation="search",
                        request_json={"query": "retained"},
                        response_json={"items": []},
                        expires_at=now + timedelta(hours=1),
                    ),
                    PronunciationEntry(
                        id=ids["pronunciation"],
                        workspace_id=workspace.id,
                        source_form="Pandrator",
                        normalized_form="pandrator",
                        language="en",
                        phonetic="pan-dra-tor",
                    ),
                    VoiceSample(
                        id=ids["voice_sample"],
                        workspace_id=workspace.id,
                        voice_id=voice.id,
                        artifact_id=artifact.id,
                        transcript="retained sample",
                    ),
                    TrainingRun(
                        id=ids["training"],
                        workspace_id=workspace.id,
                        voice_id=voice.id,
                        job_id=job.id,
                        model_name="fixture-model",
                        settings_json={"retained": True},
                    ),
                    UsageEvent(
                        id=ids["usage_event"],
                        workspace_id=workspace.id,
                        project_id=project.id,
                        session_id=project_session.id,
                        job_id=job.id,
                        artifact_id=artifact.id,
                        provider_key="fixture-provider",
                        model_id="fixture-model",
                        input_tokens=7,
                        output_tokens=2,
                    ),
                    VoiceCollectionMember(
                        workspace_id=workspace.id,
                        collection_id=collection.id,
                        voice_key=f"managed:{voice.id}",
                        voice_ref_json={"kind": "managed", "id": voice.id},
                        managed_voice_id=voice.id,
                    ),
                    SoundVariant(
                        id=ids["sound_variant"],
                        workspace_id=workspace.id,
                        item_id=sound_item.id,
                        job_id=job.id,
                        artifact_id=artifact.id,
                        state="ready",
                        provenance_json={"retained": True},
                    ),
                    TranslationProject(
                        id=ids["translation_project"],
                        project_root_id=project.id,
                        name="Retained translation",
                        source_session_id=project_session.id,
                        checkpoint_artifact_id=artifact.id,
                        source_content_hash="f" * 64,
                        source_language="en",
                    ),
                    SessionSettingHistory(
                        session_id=project_session.id,
                        section="tts",
                        value_json={"service": "fixture"},
                        revision=4,
                    ),
                    WorkspaceSettingHistory(
                        workspace_id=workspace.id,
                        key="defaults.tts",
                        value_json={"service": "fixture"},
                        revision=3,
                    ),
                    AppSettingHistory(
                        key="defaults.tts",
                        value_json={"service": "old"},
                        revision=2,
                    ),
                ]
            )
            session.flush()
            assert session_project(session, project_session.id).id == project.id
    finally:
        database.dispose()

    _assert_foreign_keys_clean(path)
    return ids


def _owned_sql(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    placeholders = ", ".join("?" for _ in ROOT_TABLES)
    with sqlite3.connect(path) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL "
                f"AND tbl_name IN ({placeholders}) ORDER BY type, name",
                ROOT_TABLES,
            ).fetchall()
        )


def _install_voice_checks(path: Path) -> None:
    engine = sa.create_engine(sqlite_url(path), future=True)
    try:
        with engine.begin() as connection:
            metadata = sa.MetaData()
            voice_table = sa.Table("voices", metadata, autoload_with=connection, resolve_fks=False)
            voice_table.append_constraint(sa.CheckConstraint("length(name) > 0"))
            voice_table.append_constraint(
                sa.CheckConstraint(
                    "length(name) <= 255",
                    name="ck_mandatory_test_voice_name_max",
                )
            )
            operations = Operations(
                MigrationContext.configure(connection, opts={"render_as_batch": True})
            )
            with operations.batch_alter_table("voices", copy_from=voice_table) as batch:
                batch.alter_column(
                    "name",
                    existing_type=sa.String(length=255),
                    type_=sa.String(length=255),
                )
    finally:
        engine.dispose()


def _voice_check_signature(path: Path) -> tuple[tuple[str | None, str], ...]:
    engine = sa.create_engine(sqlite_url(path), future=True)
    try:
        with engine.connect() as connection:
            constraints = sa.inspect(connection).get_check_constraints("voices")
    finally:
        engine.dispose()
    return tuple(
        sorted(
            (
                (
                    constraint.get("name"),
                    "".join(
                        str(constraint["sqltext"]).lower().replace('"', "").replace("`", "").split()
                    ),
                )
                for constraint in constraints
            ),
            key=repr,
        )
    )


def _assert_invalid_voice_names_rejected(path: Path, voice_id: str) -> None:
    with sqlite3.connect(path) as connection:
        original = connection.execute(
            "SELECT name FROM voices WHERE id = ?", (voice_id,)
        ).fetchone()
        assert original is not None
        for invalid_name in ("", "x" * 256):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE voices SET name = ? WHERE id = ?",
                    (invalid_name, voice_id),
                )
            connection.rollback()
            assert (
                connection.execute("SELECT name FROM voices WHERE id = ?", (voice_id,)).fetchone()
                == original
            )


def _root_index_and_fk_signatures(
    path: Path,
) -> tuple[tuple[str, tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]], ...]:
    with sqlite3.connect(path) as connection:
        signatures = []
        for table_name in ROOT_TABLES:
            foreign_keys = tuple(
                sorted(
                    tuple(row[1:])
                    for row in _pragma_rows(connection, "foreign_key_list", table_name)
                )
            )
            indexes: list[tuple[Any, ...]] = []
            for index in _pragma_rows(connection, "index_list", table_name):
                index_name = str(index[1])
                index_columns = tuple(
                    tuple(column) for column in _pragma_rows(connection, "index_xinfo", index_name)
                )
                indexes.append(
                    (
                        index_name if str(index[3]) == "c" else None,
                        bool(index[2]),
                        str(index[3]),
                        bool(index[4]),
                        index_columns,
                    )
                )
            signatures.append((table_name, foreign_keys, tuple(sorted(indexes, key=repr))))
    return tuple(signatures)


def _full_database_snapshot(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as connection:
        objects = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        contents = tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {_quote(table_name)} ORDER BY rowid"
                    ).fetchall()
                ),
            )
            for table_name in tables
        )
        foreign_key_check = tuple(
            tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
        )
    return objects, contents, foreign_key_check


def _table_data_snapshot(path: Path) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    with sqlite3.connect(path) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
                "ORDER BY name"
            ).fetchall()
        )
        return tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {_quote(table_name)} ORDER BY rowid"
                    ).fetchall()
                ),
            )
            for table_name in tables
        )


def _assert_foreign_keys_clean(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _root_primary_key_values(
    connection: sqlite3.Connection, table_name: str
) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    columns = _pragma_rows(connection, "table_info", table_name)
    primary_key_columns = tuple(
        str(row[1])
        for row in sorted((row for row in columns if int(row[5]) > 0), key=lambda row: int(row[5]))
    )
    assert primary_key_columns
    selected = ", ".join(_quote(column) for column in primary_key_columns)
    row = connection.execute(
        f"SELECT {selected} FROM {_quote(table_name)} ORDER BY rowid LIMIT 1"
    ).fetchone()
    assert row is not None
    return primary_key_columns, tuple(row)


def test_fresh_upgrade_reaches_mandatory_head_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    command.upgrade(_config(path), REVISION)

    assert _version(path) == REVISION
    _assert_root_shapes(path, nullable=False)
    _assert_foreign_keys_clean(path)
    before_repeat = _full_database_snapshot(path)

    command.upgrade(_config(path), REVISION)

    assert _full_database_snapshot(path) == before_repeat


def test_populated_upgrade_preserves_all_roots_indexes_foreign_keys_and_trigger(
    populated_0074: tuple[Path, dict[str, str]],
) -> None:
    path, ids = populated_0074
    assert _version(path) == PREVIOUS_REVISION
    _assert_root_shapes(path, nullable=True)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE INDEX ix_mandatory_test_voice_length "
            "ON voices(length(name)) WHERE language IS NOT NULL"
        )
        connection.execute(
            "CREATE TABLE mandatory_test_voice_changes "
            "(voice_id TEXT NOT NULL, old_name TEXT NOT NULL, new_name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER trg_mandatory_test_voice_name "
            "AFTER UPDATE OF name ON voices BEGIN "
            "INSERT INTO mandatory_test_voice_changes (voice_id, old_name, new_name) "
            "VALUES (NEW.id, OLD.name, NEW.name); END"
        )
        connection.commit()

    rows_before = _table_data_snapshot(path)
    schema_before = _root_index_and_fk_signatures(path)
    owned_sql_before = _owned_sql(path)
    trigger_sql = next(
        sql
        for object_type, name, _table, sql in owned_sql_before
        if object_type == "trigger" and name == "trg_mandatory_test_voice_name"
    )
    expression_index_sql = next(
        sql
        for object_type, name, _table, sql in owned_sql_before
        if object_type == "index" and name == "ix_mandatory_test_voice_length"
    )

    command.upgrade(_config(path), REVISION)

    assert _version(path) == REVISION
    _assert_root_shapes(path, nullable=False)
    assert _table_data_snapshot(path) == rows_before
    assert _root_index_and_fk_signatures(path) == schema_before
    owned_sql_after = _owned_sql(path)
    assert owned_sql_after == owned_sql_before
    assert ("trigger", "trg_mandatory_test_voice_name", "voices", trigger_sql) in owned_sql_after
    assert (
        "index",
        "ix_mandatory_test_voice_length",
        "voices",
        expression_index_sql,
    ) in owned_sql_after
    _assert_foreign_keys_clean(path)

    with sqlite3.connect(path) as connection:
        for table_name, column_name, _parent, _ondelete in ROOTS:
            key_columns, key_values = _root_primary_key_values(connection, table_name)
            where = " AND ".join(f"{_quote(column)} = ?" for column in key_columns)
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE {_quote(table_name)} SET {_quote(column_name)} = NULL WHERE {where}",
                    key_values,
                )
            connection.rollback()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE voices SET name = ? WHERE id = ?",
            ("Retained voice after trigger", ids["voice"]),
        )
        change = connection.execute(
            "SELECT voice_id, old_name, new_name FROM mandatory_test_voice_changes"
        ).fetchone()
        assert change == (ids["voice"], "Retained voice", "Retained voice after trigger")
        connection.commit()

    rows_before_downgrade = _table_data_snapshot(path)
    command.downgrade(_config(path), PREVIOUS_REVISION)

    assert _version(path) == PREVIOUS_REVISION
    _assert_root_shapes(path, nullable=True)
    assert _table_data_snapshot(path) == rows_before_downgrade
    assert _root_index_and_fk_signatures(path) == schema_before
    assert _owned_sql(path) == owned_sql_before
    _assert_foreign_keys_clean(path)

    command.upgrade(_config(path), REVISION)

    assert _version(path) == REVISION
    _assert_root_shapes(path, nullable=False)
    assert _table_data_snapshot(path) == rows_before_downgrade
    assert _root_index_and_fk_signatures(path) == schema_before
    assert _owned_sql(path) == owned_sql_before
    _assert_foreign_keys_clean(path)


def test_voice_checks_survive_upgrade_downgrade_and_reupgrade(
    populated_0074: tuple[Path, dict[str, str]],
) -> None:
    path, ids = populated_0074
    _install_voice_checks(path)

    expected_checks = {
        (None, "length(name)>0"),
        ("ck_mandatory_test_voice_name_max", "length(name)<=255"),
    }
    assert set(_voice_check_signature(path)) == expected_checks
    _assert_invalid_voice_names_rejected(path, ids["voice"])
    expected_rows = _table_data_snapshot(path)

    command.upgrade(_config(path), REVISION)

    assert set(_voice_check_signature(path)) == expected_checks
    assert _table_data_snapshot(path) == expected_rows
    _assert_invalid_voice_names_rejected(path, ids["voice"])

    command.downgrade(_config(path), PREVIOUS_REVISION)

    assert set(_voice_check_signature(path)) == expected_checks
    assert _table_data_snapshot(path) == expected_rows
    _assert_invalid_voice_names_rejected(path, ids["voice"])

    command.upgrade(_config(path), REVISION)

    assert set(_voice_check_signature(path)) == expected_checks
    assert _table_data_snapshot(path) == expected_rows
    _assert_invalid_voice_names_rejected(path, ids["voice"])
    _assert_foreign_keys_clean(path)


def test_missing_root_fails_before_ddl_and_preserves_database_snapshot(
    schema_at_0074: Path, tmp_path: Path
) -> None:
    path = tmp_path / "missing-root.sqlite"
    _copy_sqlite(schema_at_0074, path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE pronunciation_entries")
        connection.commit()
    before = _full_database_snapshot(path)

    with pytest.raises(RuntimeError, match="pronunciation_entries"):
        command.upgrade(_config(path), REVISION)

    assert _full_database_snapshot(path) == before


def test_malformed_mixed_root_shape_fails_before_ddl_and_preserves_snapshot(
    schema_at_0074: Path, tmp_path: Path
) -> None:
    path = tmp_path / "malformed-root.sqlite"
    _copy_sqlite(schema_at_0074, path)
    engine = sa.create_engine(sqlite_url(path), future=True)
    try:
        with engine.begin() as connection:
            operations = Operations(
                MigrationContext.configure(connection, opts={"render_as_batch": True})
            )
            with operations.batch_alter_table("pronunciation_entries") as batch:
                batch.alter_column(
                    "workspace_id",
                    existing_type=sa.String(length=36),
                    type_=sa.String(length=20),
                    existing_nullable=True,
                )
    finally:
        engine.dispose()
    before = _full_database_snapshot(path)

    with pytest.raises(RuntimeError, match="pronunciation_entries.workspace_id"):
        command.upgrade(_config(path), REVISION)

    assert _full_database_snapshot(path) == before


def test_existing_null_root_fails_preflight_without_backfill_or_schema_change(
    populated_0074: tuple[Path, dict[str, str]],
) -> None:
    path, ids = populated_0074
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE voices SET workspace_id = NULL WHERE id = ?", (ids["voice"],))
        connection.commit()
    before = _full_database_snapshot(path)

    with pytest.raises(RuntimeError, match="voices.workspace_id.*NULL"):
        command.upgrade(_config(path), REVISION)

    assert _full_database_snapshot(path) == before


def test_late_failure_rolls_back_replacements_and_retry_succeeds(
    populated_0074: tuple[Path, dict[str, str]],
) -> None:
    path, _ids = populated_0074
    rows_before = _table_data_snapshot(path)
    before = _full_database_snapshot(path)
    target_tables = set(ROOT_TABLES)
    replacements_before_failure = 0

    def fail_after_multiple_replacements(
        _connection: sa.Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal replacements_before_failure
        normalized = " ".join(statement.lower().split())
        if not normalized.startswith("drop table"):
            return
        dropped_table = normalized.removeprefix("drop table").strip().strip('"`[];')
        if dropped_table in target_tables:
            replacements_before_failure += 1
            if replacements_before_failure == 3:
                raise RuntimeError("injected mandatory ownership failure")

    event.listen(Engine, "before_cursor_execute", fail_after_multiple_replacements)
    try:
        with pytest.raises(RuntimeError, match="injected mandatory ownership failure"):
            command.upgrade(_config(path), REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_multiple_replacements)

    assert replacements_before_failure == 3
    assert _full_database_snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).fetchall()
            == []
        )

    command.upgrade(_config(path), REVISION)

    assert _version(path) == REVISION
    _assert_root_shapes(path, nullable=False)
    assert _table_data_snapshot(path) == rows_before
    _assert_foreign_keys_clean(path)
