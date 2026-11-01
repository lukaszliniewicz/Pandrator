from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from pandrator.web.database import create_database_engine, sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0069_pronunciation_scope"
PREVIOUS_REVISION = "0068_usage_ownership"
TABLE = "pronunciation_entries"
NOW = "2026-10-23 13:20:00"
WORKSPACE_INDEX = "ix_pronunciation_entries_workspace_id"
PROJECT_INDEX = "ix_pronunciation_entries_project_id"
GLOBAL_INDEX = "uq_pronunciation_entries_workspace_global"
OLD_COLUMNS = (
    "id",
    "scope",
    "session_id",
    "source_form",
    "normalized_form",
    "language",
    "phonetic",
    "alphabet",
    "backend",
    "status",
    "source",
    "notes",
    "metadata_json",
    "revision",
    "created_at",
    "updated_at",
)
LEGACY_TABLE_SQL = """CREATE TABLE pronunciation_entries_legacy (
    id VARCHAR(36) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    session_id VARCHAR(36),
    source_form TEXT NOT NULL,
    normalized_form VARCHAR(512) NOT NULL,
    language VARCHAR(40) NOT NULL,
    phonetic TEXT NOT NULL,
    alphabet VARCHAR(32) NOT NULL,
    backend VARCHAR(80) NOT NULL,
    status VARCHAR(24) NOT NULL,
    source VARCHAR(40) NOT NULL,
    notes TEXT,
    metadata_json JSON NOT NULL,
    revision INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    CONSTRAINT uq_pronunciation_entry_identity UNIQUE (scope, session_id, normalized_form, language, backend)
)"""
OLD_INDEX_SQL = (
    "CREATE INDEX ix_pronunciation_entries_scope ON pronunciation_entries (scope)",
    "CREATE INDEX ix_pronunciation_entries_session_id ON pronunciation_entries (session_id)",
    "CREATE INDEX ix_pronunciation_entries_normalized_form "
    "ON pronunciation_entries (normalized_form)",
    "CREATE INDEX ix_pronunciation_entries_language ON pronunciation_entries (language)",
    "CREATE INDEX ix_pronunciation_entries_backend ON pronunciation_entries (backend)",
    "CREATE INDEX ix_pronunciation_entries_status ON pronunciation_entries (status)",
)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0068(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pronunciation-schema-base") / "at-0068.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _legacy_database(schema_at_0068: Path, path: Path) -> None:
    shutil.copy2(schema_at_0068, path)
    columns = ", ".join(f'"{column}"' for column in OLD_COLUMNS)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(LEGACY_TABLE_SQL)
        connection.execute(
            f"INSERT INTO pronunciation_entries_legacy ({columns}) "
            f"SELECT {columns} FROM pronunciation_entries"
        )
        connection.execute("DROP TABLE pronunciation_entries")
        connection.execute(
            "ALTER TABLE pronunciation_entries_legacy RENAME TO pronunciation_entries"
        )
        for statement in OLD_INDEX_SQL:
            connection.execute(statement)


def _owner_workspace(connection: sqlite3.Connection) -> tuple[str, str]:
    row = connection.execute(
        "SELECT users.id, workspaces.id FROM users JOIN workspaces "
        "ON workspaces.personal_owner_user_id = users.id "
        "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _create_workspace(
    connection: sqlite3.Connection, workspace_id: str, *, state: str = "active"
) -> None:
    connection.execute(
        "INSERT INTO workspaces "
        "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
        "VALUES (?, 'team', ?, NULL, ?, 1, ?, ?)",
        (workspace_id, workspace_id, state, NOW, NOW),
    )


def _insert_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    project_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO sessions "
        "(id, project_id, name, storage_key, workflow_kind, interface_mode, source_language, "
        "workflow_preset, included_stages_json, status, revision, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'audiobook', 'advanced', 'auto', 'custom', '[]', 'complete', "
        "1, ?, ?)",
        (session_id, project_id, session_id, f"storage-{session_id}", NOW, NOW),
    )


def _create_project_session(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    project_id: str,
    session_id: str,
) -> None:
    _insert_session(connection, session_id)
    connection.execute(
        "INSERT INTO projects "
        "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, 1, ?, ?)",
        (project_id, workspace_id, session_id, project_id, NOW, NOW),
    )
    connection.execute("UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id))


def _insert_entry(
    connection: Any,
    entry_id: str,
    *,
    scope: str = "global",
    session_id: str | None = None,
    workspace_id: str | None = None,
    project_id: str | None = None,
    source_form: str = "Original Form",
    normalized_form: str = "original form",
    language: str = "en",
    phonetic: str = "oh-rih-jih-nal",
    backend: str = "*",
    status: str = "reviewed",
    metadata_json: str = '{ "kept": [1,  2] }',
) -> None:
    table_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(pronunciation_entries)")
    }
    legacy_values: tuple[Any, ...] = (
        entry_id,
        scope,
        session_id,
        source_form,
        normalized_form,
        language,
        phonetic,
        "respelling",
        backend,
        status,
        "manual",
        "Raw notes",
        metadata_json,
        4,
        NOW,
        NOW,
    )
    if {"workspace_id", "project_id"} <= table_columns:
        connection.execute(
            "INSERT INTO pronunciation_entries "
            "(id, scope, session_id, workspace_id, project_id, source_form, normalized_form, "
            "language, phonetic, alphabet, backend, status, source, notes, metadata_json, "
            "revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 'Raw notes', ?, 4, ?, ?)",
            (
                entry_id,
                scope,
                session_id,
                workspace_id,
                project_id,
                source_form,
                normalized_form,
                language,
                phonetic,
                "respelling",
                backend,
                status,
                metadata_json,
                NOW,
                NOW,
            ),
        )
    else:
        quoted = ", ".join(f'"{column}"' for column in OLD_COLUMNS)
        placeholders = ", ".join("?" for _ in OLD_COLUMNS)
        connection.execute(
            f"INSERT INTO pronunciation_entries ({quoted}) VALUES ({placeholders})",
            legacy_values,
        )


def _entry_rows(path: Path) -> list[tuple[Any, ...]]:
    columns = ", ".join(f'"{column}"' for column in OLD_COLUMNS)
    with sqlite3.connect(path) as connection:
        return connection.execute(
            f"SELECT {columns} FROM pronunciation_entries ORDER BY id"
        ).fetchall()


def _index_sql(path: Path) -> dict[str, str | None]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'pronunciation_entries'"
        ).fetchall()
    return {str(name): sql for name, sql in rows}


def _snapshot(path: Path) -> tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]]:
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        data = {
            table: sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            for table in tables
        }
    return schema, data


def _assert_upgrade_fails_unchanged(path: Path) -> None:
    before = _snapshot(path)
    with pytest.raises((RuntimeError, SQLAlchemyError)):
        command.upgrade(_config(path), REVISION)
    assert _snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).fetchall()
            == []
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PREVIOUS_REVISION,
        )


def test_legacy_global_and_session_rows_adopt_typed_owners_and_preserve_values(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "legacy-owners.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _owner_id, personal_workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-session")
        _create_project_session(
            connection,
            workspace_id="workspace-session",
            project_id="project-session",
            session_id="session-session",
        )
        _insert_entry(
            connection,
            "entry-global",
            scope="global",
            source_form="Keep  source form",
            normalized_form="keep exact norm",
            metadata_json=' { "workspace_id": "decoy", "n": [1,  2] } ',
        )
        _insert_entry(
            connection,
            "entry-session",
            scope="session",
            session_id="session-session",
            source_form="Session form",
            normalized_form="session norm",
            metadata_json=' { "project_id": "decoy" } ',
        )
    original_rows = _entry_rows(path)
    old_indexes = _index_sql(path)

    command.upgrade(_config(path), REVISION)

    with sqlite3.connect(path) as connection:
        owners = dict(
            connection.execute(
                "SELECT id, workspace_id || ':' || COALESCE(project_id, '') "
                "FROM pronunciation_entries"
            ).fetchall()
        )
        assert owners["entry-global"] == f"{personal_workspace_id}:"
        assert owners["entry-session"] == "workspace-session:project-session"
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REVISION,
        )
    assert _entry_rows(path) == original_rows
    upgraded_indexes = _index_sql(path)
    for name, sql in old_indexes.items():
        assert upgraded_indexes[name] == sql
    assert WORKSPACE_INDEX in upgraded_indexes
    assert PROJECT_INDEX in upgraded_indexes
    assert GLOBAL_INDEX in upgraded_indexes


def test_archived_project_workspace_is_valid_for_historical_session_rows(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "archived-workspace.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _create_workspace(connection, "workspace-archived", state="archived")
        _create_project_session(
            connection,
            workspace_id="workspace-archived",
            project_id="project-archived",
            session_id="session-archived",
        )
        _insert_entry(
            connection,
            "entry-archived",
            scope="session",
            session_id="session-archived",
        )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id, project_id FROM pronunciation_entries WHERE id = 'entry-archived'"
        ).fetchone() == ("workspace-archived", "project-archived")


def test_seeded_owner_is_validated_even_when_dictionary_is_empty(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "invalid-seed.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "UPDATE workspace_memberships SET role = 'member' "
            "WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, owner_id),
        )
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize(
    ("scope", "session_id"),
    [("unknown", None), ("global", "session-not-null"), ("session", None)],
)
def test_invalid_scope_session_pairs_fail_before_ddl(
    schema_at_0068: Path,
    tmp_path: Path,
    scope: str,
    session_id: str | None,
) -> None:
    path = tmp_path / f"invalid-pair-{scope}-{session_id}.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _insert_entry(connection, "entry-invalid", scope=scope, session_id=session_id)
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize("corruption", ["missing_project", "missing_workspace", "crossed_source"])
def test_missing_or_crossed_session_project_roots_fail_before_ddl(
    schema_at_0068: Path, tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / f"invalid-root-{corruption}.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _create_workspace(connection, "workspace-root")
        if corruption == "missing_project":
            _insert_session(connection, "session-root", project_id="project-missing")
        elif corruption == "missing_workspace":
            _create_project_session(
                connection,
                workspace_id="workspace-missing",
                project_id="project-root",
                session_id="session-root",
            )
        else:
            _create_project_session(
                connection,
                workspace_id="workspace-root",
                project_id="project-root-a",
                session_id="session-root-a",
            )
            _create_project_session(
                connection,
                workspace_id="workspace-root",
                project_id="project-root-b",
                session_id="session-root-b",
            )
            _insert_session(connection, "session-root-b-branch", project_id="project-root-b")
            connection.execute(
                "UPDATE projects SET source_session_id = 'session-root-b-branch' "
                "WHERE id = 'project-root-a'"
            )
        _insert_entry(
            connection,
            "entry-invalid-root",
            scope="session",
            session_id="session-root" if corruption != "crossed_source" else "session-root-a",
        )
    _assert_upgrade_fails_unchanged(path)


def test_translation_source_and_project_roots_must_agree_before_ddl(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "translation-root-conflict.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _create_workspace(connection, "workspace-translation")
        _create_project_session(
            connection,
            workspace_id="workspace-translation",
            project_id="project-translation-source",
            session_id="session-translation-source",
        )
        _create_project_session(
            connection,
            workspace_id="workspace-translation",
            project_id="project-translation-wrong",
            session_id="session-translation-wrong",
        )
        connection.execute(
            "INSERT INTO translation_projects "
            "(id, project_root_id, name, source_session_id, checkpoint_artifact_id, "
            "source_content_hash, source_language, revision, created_at, updated_at) "
            "VALUES ('translation-conflict', 'project-translation-wrong', 'translation', "
            "'session-translation-source', 'artifact-missing', 'hash', 'en', 1, ?, ?)",
            (NOW, NOW),
        )
        _insert_entry(
            connection,
            "entry-translation-conflict",
            scope="session",
            session_id="session-translation-source",
        )
    _assert_upgrade_fails_unchanged(path)


def test_duplicate_global_identity_fails_before_ddl_without_selecting_a_winner(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "duplicate-global.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _insert_entry(connection, "entry-global-a", normalized_form="duplicate")
        _insert_entry(connection, "entry-global-b", normalized_form="duplicate")
    _assert_upgrade_fails_unchanged(path)


def test_correct_early_target_with_both_null_scope_fields_is_adopted(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-target.sqlite"
    shutil.copy2(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _owner_id, personal_workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-early")
        _create_project_session(
            connection,
            workspace_id="workspace-early",
            project_id="project-early",
            session_id="session-early",
        )
        _insert_entry(connection, "entry-early-global", scope="global")
        _insert_entry(
            connection,
            "entry-early-session",
            scope="session",
            session_id="session-early",
        )
        assert connection.execute(
            "SELECT workspace_id, project_id FROM pronunciation_entries ORDER BY id"
        ).fetchall() == [(None, None), (None, None)]
    original_rows = _entry_rows(path)

    command.upgrade(_config(path), REVISION)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT id, workspace_id, project_id FROM pronunciation_entries ORDER BY id"
        ).fetchall() == [
            ("entry-early-global", personal_workspace_id, None),
            ("entry-early-session", "workspace-early", "project-early"),
        ]
    assert _entry_rows(path) == original_rows


def test_partial_target_columns_fail_atomically(schema_at_0068: Path, tmp_path: Path) -> None:
    path = tmp_path / "early-target-partial.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE pronunciation_entries ADD COLUMN workspace_id VARCHAR(36)")
    _assert_upgrade_fails_unchanged(path)


def test_malformed_target_column_definition_fails_atomically(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-target-malformed-column.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE pronunciation_entries ADD COLUMN workspace_id TEXT")
        connection.execute("ALTER TABLE pronunciation_entries ADD COLUMN project_id VARCHAR(36)")
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize("literal", ["session", "GLOBAL", "glo(bal)"])
def test_malformed_target_global_index_predicate_fails_atomically(
    schema_at_0068: Path, tmp_path: Path, literal: str
) -> None:
    path = tmp_path / f"early-target-malformed-index-{literal}.sqlite"
    shutil.copy2(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP INDEX {GLOBAL_INDEX}")
        connection.execute(
            f"CREATE UNIQUE INDEX {GLOBAL_INDEX} ON pronunciation_entries "
            "(workspace_id, normalized_form, language, backend) "
            f"WHERE scope = '{literal}' AND session_id IS NULL"
        )
    _assert_upgrade_fails_unchanged(path)


def test_populated_early_target_scope_fails_atomically(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-target-populated.sqlite"
    shutil.copy2(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _insert_entry(
            connection,
            "entry-already-owned",
            workspace_id=workspace_id,
        )
    _assert_upgrade_fails_unchanged(path)


def test_global_unique_index_allows_cross_workspace_and_rejects_same_workspace(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "global-unique-index.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _owner_id, personal_workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-global-other")
    command.upgrade(_config(path), REVISION)

    engine = create_database_engine(path)
    try:
        with engine.begin() as connection:
            _insert_entry(
                connection.connection.driver_connection,
                "global-personal",
                workspace_id=personal_workspace_id,
                normalized_form="shared-key",
            )
            _insert_entry(
                connection.connection.driver_connection,
                "global-team",
                workspace_id="workspace-global-other",
                normalized_form="shared-key",
            )
        with engine.begin() as connection:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(
                    connection.connection.driver_connection,
                    "global-personal-duplicate",
                    workspace_id=personal_workspace_id,
                    normalized_form="shared-key",
                )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM pronunciation_entries")
                ).scalar_one()
                == 2
            )
    finally:
        engine.dispose()


def test_named_scope_foreign_keys_and_session_delete_cascade(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "session-cascade.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _create_workspace(connection, "workspace-session-cascade")
        _create_project_session(
            connection,
            workspace_id="workspace-session-cascade",
            project_id="project-session-cascade",
            session_id="session-cascade",
        )
        _insert_entry(
            connection,
            "entry-session-cascade",
            scope="session",
            session_id="session-cascade",
        )
    command.upgrade(_config(path), REVISION)

    engine = create_database_engine(path)
    try:
        with engine.begin() as connection:
            foreign_keys = sa.inspect(connection).get_foreign_keys(TABLE)
            named = {
                item["name"]: (
                    item["referred_table"],
                    (item.get("options") or {}).get("ondelete"),
                )
                for item in foreign_keys
            }
            assert named["fk_pronunciation_entries_workspace_id_workspaces"] == (
                "workspaces",
                "RESTRICT",
            )
            assert named["fk_pronunciation_entries_project_id_projects"] == (
                "projects",
                "CASCADE",
            )
            assert named[None] == ("sessions", "CASCADE")
            connection.execute(sa.text("DELETE FROM sessions WHERE id = 'session-cascade'"))
            assert (
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM pronunciation_entries "
                        "WHERE id = 'entry-session-cascade'"
                    )
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_project_foreign_key_cascades_when_project_is_deleted(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "project-cascade.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "INSERT INTO projects "
            "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
            "created_at, updated_at) VALUES ('project-retained', ?, NULL, 'retained', NULL, 1, ?, ?)",
            (workspace_id, NOW, NOW),
        )
    command.upgrade(_config(path), REVISION)

    engine = create_database_engine(path)
    try:
        with engine.begin() as connection:
            # Direct SQL isolates the declared project FK cascade from application scope checks.
            connection.execute(
                sa.text(
                    "INSERT INTO pronunciation_entries "
                    "(id, scope, session_id, workspace_id, project_id, source_form, "
                    "normalized_form, language, phonetic, alphabet, backend, status, source, "
                    "notes, metadata_json, revision, created_at, updated_at) VALUES "
                    "('entry-project-cascade', 'global', NULL, :workspace_id, 'project-retained', "
                    "'Form', 'form', 'en', 'form', 'respelling', '*', 'reviewed', 'manual', "
                    "NULL, '{}', 1, :now, :now)"
                ),
                {"workspace_id": workspace_id, "now": NOW},
            )
            connection.execute(sa.text("DELETE FROM projects WHERE id = 'project-retained'"))
            assert (
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM pronunciation_entries "
                        "WHERE id = 'entry-project-cascade'"
                    )
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_late_failure_rolls_back_full_schema_and_data_then_retry_repeat_and_downgrade(
    schema_at_0068: Path, tmp_path: Path
) -> None:
    path = tmp_path / "late-failure.sqlite"
    _legacy_database(schema_at_0068, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-late")
        _create_project_session(
            connection,
            workspace_id="workspace-late",
            project_id="project-late",
            session_id="session-late",
        )
        _insert_entry(connection, "entry-global-late")
        _insert_entry(
            connection,
            "entry-session-late",
            scope="session",
            session_id="session-late",
        )
        connection.execute(
            "CREATE INDEX custom_pronunciation_desc ON pronunciation_entries "
            "(normalized_form DESC, language ASC, id DESC) WHERE status = 'reviewed'"
        )
        connection.execute(
            "CREATE TRIGGER fail_pronunciation_version "
            "BEFORE UPDATE OF version_num ON alembic_version "
            "WHEN NEW.version_num = '0069_pronunciation_scope' BEGIN "
            "SELECT RAISE(ABORT, 'late pronunciation migration failure'); END"
        )
    original_rows = _entry_rows(path)
    before = _snapshot(path)
    with pytest.raises(SQLAlchemyError, match="late pronunciation migration failure"):
        command.upgrade(_config(path), REVISION)
    assert _snapshot(path) == before
    assert _entry_rows(path) == original_rows
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).fetchall()
            == []
        )

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_pronunciation_version")
    command.upgrade(_config(path), REVISION)
    upgraded_snapshot = _snapshot(path)
    assert _entry_rows(path) == original_rows
    custom_sql = (
        "CREATE INDEX custom_pronunciation_desc ON pronunciation_entries "
        "(normalized_form DESC, language ASC, id DESC) WHERE status = 'reviewed'"
    )
    assert _index_sql(path)["custom_pronunciation_desc"] == custom_sql
    command.upgrade(_config(path), REVISION)
    assert _snapshot(path) == upgraded_snapshot

    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _entry_rows(path) == original_rows
    downgraded_indexes = _index_sql(path)
    assert downgraded_indexes["custom_pronunciation_desc"] == custom_sql
    assert WORKSPACE_INDEX not in downgraded_indexes
    assert PROJECT_INDEX not in downgraded_indexes
    assert GLOBAL_INDEX not in downgraded_indexes
    command.upgrade(_config(path), REVISION)
    assert _entry_rows(path) == original_rows
