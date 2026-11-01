from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from pandrator.web.database import sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0065_upload_ownership"
PREVIOUS_REVISION = "0064_voice_annotations"
TIMESTAMP = "2026-10-23 12:34:56"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0064(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("upload-ownership-base") / "at-0064.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _connect(path: Path) -> Engine:
    return sa.create_engine(sqlite_url(path), future=True)


def _make_legacy_upload_table(connection: Connection) -> None:
    connection.exec_driver_sql("DROP INDEX ix_upload_sessions_workspace_id")
    connection.exec_driver_sql("DROP INDEX ix_upload_sessions_project_id")
    connection.exec_driver_sql("DROP TABLE upload_sessions")
    connection.exec_driver_sql(
        """
        CREATE TABLE upload_sessions (
            id VARCHAR(36) NOT NULL,
            session_id VARCHAR(36),
            filename VARCHAR(255) NOT NULL,
            mime_type VARCHAR(160),
            size_bytes INTEGER NOT NULL,
            chunk_size INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL,
            received_json JSON NOT NULL,
            expected_hash VARCHAR(128),
            temporary_relative_path TEXT NOT NULL,
            state VARCHAR(32) NOT NULL,
            result_json JSON,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_upload_sessions_session_id ON upload_sessions (session_id)"
    )
    connection.exec_driver_sql("CREATE INDEX ix_upload_sessions_state ON upload_sessions (state)")
    connection.exec_driver_sql(
        "CREATE INDEX ix_upload_sessions_expires_at ON upload_sessions (expires_at)"
    )


def _legacy_database(schema_at_0064: Path, path: Path) -> None:
    shutil.copy2(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _make_legacy_upload_table(connection)
    finally:
        engine.dispose()


def _owner_workspace(connection: Connection) -> str:
    return str(
        connection.execute(
            sa.text(
                "SELECT workspaces.id FROM workspaces JOIN users "
                "ON users.id = workspaces.personal_owner_user_id "
                "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
            )
        ).scalar_one()
    )


def _create_team_workspace(
    connection: Connection, workspace_id: str, *, state: str = "active"
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
            "VALUES (:id, 'team', :name, NULL, :state, 1, :now, :now)"
        ),
        {"id": workspace_id, "name": workspace_id, "state": state, "now": TIMESTAMP},
    )


def _create_project_session(
    connection: Connection,
    *,
    workspace_id: str,
    project_id: str = "project-test",
    root_session_id: str = "session-root-test",
    upload_session_id: str | None = None,
    trashed: bool = False,
) -> tuple[str, str]:
    for session_id in dict.fromkeys((root_session_id, upload_session_id)):
        if session_id is None:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO sessions "
                "(id, project_id, name, storage_key, workflow_kind, interface_mode, "
                "source_language, workflow_preset, included_stages_json, status, revision, "
                "created_at, updated_at, trashed_at) "
                "VALUES (:id, NULL, :name, :storage_key, 'audiobook', 'advanced', 'auto', "
                "'custom', '[]', 'complete', 1, :now, :now, :trashed_at)"
            ),
            {
                "id": session_id,
                "name": session_id,
                "storage_key": f"storage-{session_id}",
                "now": TIMESTAMP,
                "trashed_at": TIMESTAMP if trashed else None,
            },
        )
    connection.execute(
        sa.text(
            "INSERT INTO projects "
            "(id, workspace_id, source_session_id, name, revision, created_at, updated_at) "
            "VALUES (:id, :workspace_id, :source_session_id, :name, 1, :now, :now)"
        ),
        {
            "id": project_id,
            "workspace_id": workspace_id,
            "source_session_id": root_session_id,
            "name": project_id,
            "now": TIMESTAMP,
        },
    )
    connection.execute(
        sa.text("UPDATE sessions SET project_id = :project_id WHERE id = :session_id"),
        {"project_id": project_id, "session_id": root_session_id},
    )
    if upload_session_id is not None and upload_session_id != root_session_id:
        connection.execute(
            sa.text("UPDATE sessions SET project_id = :project_id WHERE id = :session_id"),
            {"project_id": project_id, "session_id": upload_session_id},
        )
    return project_id, upload_session_id or root_session_id


def _create_artifact(
    connection: Connection,
    *,
    artifact_id: str,
    workspace_id: str,
    project_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO artifacts "
            "(id, workspace_id, project_id, session_id, kind, role, relative_path, mime_type, "
            "size_bytes, content_hash, state, settings_hash, metadata_json, created_at, updated_at) "
            "VALUES (:id, :workspace_id, :project_id, NULL, 'audio', 'upload', :path, "
            "'audio/wav', 7, 'hash', 'ready', NULL, '{}', :now, :now)"
        ),
        {
            "id": artifact_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "path": f"artifacts/{artifact_id}.wav",
            "now": TIMESTAMP,
        },
    )


def _create_source_asset(
    connection: Connection,
    *,
    asset_id: str,
    workspace_id: str,
    artifact_id: str | None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO source_assets "
            "(id, workspace_id, artifact_id, display_name, kind, mime_type, external_path, "
            "size_bytes, content_hash, state, metadata_json, revision, created_at, updated_at) "
            "VALUES (:id, :workspace_id, :artifact_id, :name, 'audio', 'audio/wav', NULL, "
            "7, 'hash', 'ready', '{}', 1, :now, :now)"
        ),
        {
            "id": asset_id,
            "workspace_id": workspace_id,
            "artifact_id": artifact_id,
            "name": asset_id,
            "now": TIMESTAMP,
        },
    )


def _insert_upload(
    connection: Connection,
    upload_id: str,
    *,
    session_id: str | None = None,
    result_json: str | None = None,
    state: str = "complete",
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO upload_sessions "
            "(id, session_id, filename, mime_type, size_bytes, chunk_size, chunk_count, "
            "received_json, expected_hash, temporary_relative_path, state, result_json, "
            "created_at, updated_at, expires_at) "
            "VALUES (:id, :session_id, 'archive.wav', 'audio/wav', 7, 4, 2, '{}', 'hash', "
            ":path, :state, :result_json, :now, :now, :expires_at)"
        ),
        {
            "id": upload_id,
            "session_id": session_id,
            "path": f"uploads/{upload_id}.part",
            "state": state,
            "result_json": result_json,
            "now": TIMESTAMP,
            "expires_at": "2026-10-24 12:34:56",
        },
    )


def _upload_columns(connection: Connection) -> list[str]:
    return [column["name"] for column in sa.inspect(connection).get_columns("upload_sessions")]


def _upload_rows(connection: Connection, columns: list[str] | None = None) -> list[tuple[Any, ...]]:
    selected = columns or _upload_columns(connection)
    quoted = ", ".join(f'"{name}"' for name in selected)
    return [
        tuple(row)
        for row in connection.exec_driver_sql(
            f"SELECT {quoted} FROM upload_sessions ORDER BY id"
        ).all()
    ]


def _version(connection: Connection) -> str:
    return str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())


def _database_snapshot(connection: Connection) -> tuple[Any, ...]:
    objects = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).all()
    table_names = (
        connection.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        .scalars()
        .all()
    )
    table_data = []
    for table_name in table_names:
        quoted_table = '"' + str(table_name).replace('"', '""') + '"'
        columns = connection.exec_driver_sql(f"PRAGMA table_info({quoted_table})").all()
        order_by = ", ".join('"' + str(column[1]).replace('"', '""') + '"' for column in columns)
        rows = connection.exec_driver_sql(f"SELECT * FROM {quoted_table} ORDER BY {order_by}").all()
        table_data.append((table_name, tuple(tuple(row) for row in rows)))
    return tuple(objects), tuple(table_data), _version(connection)


def _add_session_owned_history(
    connection: Connection, *, state: str = "complete"
) -> tuple[str, str, str]:
    workspace_id = "workspace-archived-test"
    _create_team_workspace(connection, workspace_id, state="archived")
    project_id, session_id = _create_project_session(
        connection,
        workspace_id=workspace_id,
        project_id="project-archived-test",
        root_session_id="session-trashed-root-test",
        upload_session_id="session-bound-test",
        trashed=True,
    )
    _insert_upload(connection, "upload-bound-test", session_id=session_id, state=state)
    return workspace_id, project_id, session_id


def test_fresh_upgrade_recognizes_complete_nullable_early_metadata_shape(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-metadata.sqlite"
    shutil.copy2(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.connect() as connection:
            before_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'upload_sessions'"
            ).scalar_one()
            _insert_upload(connection, "upload-empty-result", result_json="{}")
            connection.commit()
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            after_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'upload_sessions'"
            ).scalar_one()
            assert before_sql == after_sql
            assert _version(connection) == REVISION
            assert _upload_columns(connection).count("workspace_id") == 1
            row = connection.execute(
                sa.text(
                    "SELECT workspace_id, project_id, result_json FROM upload_sessions "
                    "WHERE id = 'upload-empty-result'"
                )
            ).one()
            assert row.workspace_id == _owner_workspace(connection)
            assert row.project_id is None
            assert row.result_json == "{}"
    finally:
        engine.dispose()


def test_legacy_session_upload_inherits_archived_project_and_keeps_history(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "archived-session.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id, project_id, session_id = _add_session_owned_history(
                connection, state="complete"
            )
            _insert_upload(connection, "upload-null-result", session_id=session_id)
            before = _upload_rows(connection)
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            columns = _upload_columns(connection)
            rows = {
                row.id: row
                for row in connection.execute(
                    sa.text(
                        "SELECT id, session_id, workspace_id, project_id, result_json, "
                        "state, filename, temporary_relative_path FROM upload_sessions"
                    )
                )
            }
            assert _version(connection) == REVISION
            assert rows["upload-bound-test"].session_id == session_id
            assert rows["upload-bound-test"].workspace_id == workspace_id
            assert rows["upload-bound-test"].project_id == project_id
            assert rows["upload-bound-test"].result_json is None
            assert rows["upload-bound-test"].state == "complete"
            assert rows["upload-null-result"].workspace_id == workspace_id
            assert rows["upload-null-result"].project_id == project_id
            assert (
                _upload_rows(
                    connection,
                    [name for name in columns if name not in {"workspace_id", "project_id"}],
                )
                == before
            )
    finally:
        engine.dispose()


def test_unbound_result_artifact_sets_workspace_without_project_grant(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "unbound-artifact.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-result-archived"
            _create_team_workspace(connection, workspace_id, state="archived")
            project_id, _ = _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-result-archived",
                root_session_id="session-result-archived",
            )
            _create_artifact(
                connection,
                artifact_id="artifact-unbound-result",
                workspace_id=workspace_id,
                project_id=project_id,
            )
            _insert_upload(
                connection,
                "upload-unbound-artifact",
                result_json=' { "artifact_id" : "artifact-unbound-result", "upload_id" : "upload-unbound-artifact" } ',
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT workspace_id, project_id, result_json FROM upload_sessions "
                    "WHERE id = 'upload-unbound-artifact'"
                )
            ).one()
            assert row.workspace_id == workspace_id
            assert row.project_id is None
            assert row.result_json == (
                ' { "artifact_id" : "artifact-unbound-result", '
                '"upload_id" : "upload-unbound-artifact" } '
            )
    finally:
        engine.dispose()


def test_unbound_result_source_asset_uses_its_backing_artifact_workspace(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "unbound-source-asset.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-source-archived"
            _create_team_workspace(connection, workspace_id, state="archived")
            _create_artifact(
                connection,
                artifact_id=" artifact-source-backed ",
                workspace_id=workspace_id,
            )
            _create_source_asset(
                connection,
                asset_id="source-asset-result",
                workspace_id=workspace_id,
                artifact_id=" artifact-source-backed ",
            )
            _insert_upload(
                connection,
                "upload-source-asset",
                result_json=(
                    '{"artifact_id":" artifact-source-backed ",'
                    '"source_asset_id":"source-asset-result"}'
                ),
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT workspace_id, project_id FROM upload_sessions "
                    "WHERE id = 'upload-source-asset'"
                )
            ).one()
            assert row.workspace_id == workspace_id
            assert row.project_id is None
    finally:
        engine.dispose()


def test_unbound_fallback_ignores_metadata_and_arbitrary_json_fields(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "unbound-fallback.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            fallback_workspace = _owner_workspace(connection)
            _create_team_workspace(connection, "workspace-decoy")
            _insert_upload(
                connection,
                "upload-fallback",
                result_json='{"workspace_id":"workspace-decoy","metadata":{"owner":"workspace-decoy"}}',
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT workspace_id, project_id FROM upload_sessions "
                    "WHERE id = 'upload-fallback'"
                )
            ).one()
            assert row.workspace_id == fallback_workspace
            assert row.project_id is None
    finally:
        engine.dispose()


def test_session_result_artifact_must_keep_its_project_after_detachment(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "session-result-artifact.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id, project_id, session_id = _add_session_owned_history(connection)
            _create_artifact(
                connection,
                artifact_id="artifact-bound-result",
                workspace_id=workspace_id,
                project_id=project_id,
            )
            connection.execute(
                sa.text("UPDATE artifacts SET session_id = NULL WHERE id = :id"),
                {"id": "artifact-bound-result"},
            )
            connection.execute(
                sa.text("UPDATE upload_sessions SET result_json = :result WHERE id = :id"),
                {
                    "id": "upload-bound-test",
                    "result": json.dumps({"artifact_id": "artifact-bound-result"}),
                },
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT workspace_id, project_id FROM upload_sessions "
                    "WHERE id = 'upload-bound-test'"
                )
            ).one()
            assert row.workspace_id == workspace_id
            assert row.project_id == project_id
    finally:
        engine.dispose()


def test_bound_result_artifact_with_conflicting_project_fails_before_ddl(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "bound-result-project.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id, project_id, session_id = _add_session_owned_history(connection)
            other_project_id, _ = _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-other-bound-result",
                root_session_id="session-other-bound-result",
            )
            _create_artifact(
                connection,
                artifact_id="artifact-bound-result",
                workspace_id=workspace_id,
                project_id=other_project_id,
            )
            connection.execute(
                sa.text("UPDATE upload_sessions SET result_json = :result WHERE id = :id"),
                {
                    "id": "upload-bound-test",
                    "result": json.dumps({"artifact_id": "artifact-bound-result"}),
                },
            )
            assert project_id != other_project_id and session_id
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match="does not retain project"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_result_source_asset_and_backing_artifact_workspace_must_agree_before_ddl(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "source-backing-workspace-conflict.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _create_team_workspace(connection, "workspace-source-asset")
            _create_team_workspace(connection, "workspace-backing-artifact")
            _create_artifact(
                connection,
                artifact_id="artifact-source-asset-backing",
                workspace_id="workspace-backing-artifact",
            )
            _create_source_asset(
                connection,
                asset_id="asset-with-conflicting-backing-workspace",
                workspace_id="workspace-source-asset",
                artifact_id="artifact-source-asset-backing",
            )
            _insert_upload(
                connection,
                "upload-source-backing-conflict",
                result_json='{"source_asset_id":"asset-with-conflicting-backing-workspace"}',
            )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match="different workspaces"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("corruption", ["project_root", "session_project"])
def test_result_artifact_retained_project_and_session_must_agree_before_ddl(
    schema_at_0064: Path, tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / f"artifact-retained-graph-{corruption}.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-artifact-retained-graph"
            _create_team_workspace(connection, workspace_id)
            project_id, _ = _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-artifact-retained-graph",
                root_session_id="session-artifact-retained-root",
            )
            artifact_session_id = None
            if corruption == "session_project":
                other_project_id, artifact_session_id = _create_project_session(
                    connection,
                    workspace_id=workspace_id,
                    project_id="project-artifact-retained-other",
                    root_session_id="session-artifact-retained-other",
                )
                assert other_project_id != project_id
            _create_artifact(
                connection,
                artifact_id="artifact-retained-graph",
                workspace_id=workspace_id,
                project_id=project_id,
            )
            if artifact_session_id is not None:
                connection.execute(
                    sa.text("UPDATE artifacts SET session_id = :session_id WHERE id = :id"),
                    {"session_id": artifact_session_id, "id": "artifact-retained-graph"},
                )
            else:
                connection.execute(
                    sa.text("UPDATE projects SET source_session_id = NULL WHERE id = :id"),
                    {"id": project_id},
                )
            _insert_upload(
                connection,
                "upload-artifact-retained-graph",
                result_json='{"artifact_id":"artifact-retained-graph"}',
            )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_bound_source_asset_backing_artifact_must_retain_session_project_before_ddl(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "bound-source-asset-project-conflict.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id, project_id, session_id = _add_session_owned_history(connection)
            other_project_id, _ = _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-other-source-backing",
                root_session_id="session-other-source-backing",
            )
            _create_artifact(
                connection,
                artifact_id="artifact-other-source-backing",
                workspace_id=workspace_id,
                project_id=other_project_id,
            )
            _create_source_asset(
                connection,
                asset_id="asset-bound-other-project-backing",
                workspace_id=workspace_id,
                artifact_id="artifact-other-source-backing",
            )
            connection.execute(
                sa.text("UPDATE upload_sessions SET result_json = :result WHERE id = :id"),
                {
                    "id": "upload-bound-test",
                    "result": json.dumps({"source_asset_id": "asset-bound-other-project-backing"}),
                },
            )
            assert project_id != other_project_id and session_id
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match="does not retain project"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_sql_null_and_empty_object_results_remain_valid(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "empty-history.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_upload(connection, "upload-sql-null", result_json=None, state="complete")
            _insert_upload(connection, "upload-empty-object", result_json="{}", state="complete")
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            fallback = _owner_workspace(connection)
            rows = connection.execute(
                sa.text("SELECT workspace_id, project_id FROM upload_sessions ORDER BY id")
            ).all()
            assert rows == [(fallback, None), (fallback, None)]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("result_json", "message"),
    [
        ("[]", "must be an object"),
        ("null", "must be an object"),
        ('""', "must be an object"),
        ("{broken", "malformed"),
        ('{"artifact_id":null}', "artifact_id must be a non-empty string"),
        ('{"artifact_id":"  "}', "artifact_id must be a non-empty string"),
        ('{"source_asset_id":17}', "source_asset_id must be a non-empty string"),
    ],
)
def test_malformed_or_non_object_result_fails_before_ddl(
    schema_at_0064: Path,
    tmp_path: Path,
    result_json: str,
    message: str,
) -> None:
    path = tmp_path / "invalid-result.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_upload(connection, "upload-invalid-result", result_json=result_json)
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match=message):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "result_json",
    [
        '{"upload_id":"different-upload"}',
        '{"artifact_id":"missing-artifact"}',
        '{"source_asset_id":"missing-asset"}',
    ],
)
def test_dangling_or_mismatched_known_result_reference_fails_before_ddl(
    schema_at_0064: Path, tmp_path: Path, result_json: str
) -> None:
    path = tmp_path / "dangling-result.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_upload(connection, "upload-known-result", result_json=result_json)
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("source_asset_artifact", "message"),
    [(None, "conflicting workspaces"), ("artifact-b", "does not reference its result artifact")],
)
def test_unbound_result_conflict_and_asset_backing_mismatch_fail_before_ddl(
    schema_at_0064: Path,
    tmp_path: Path,
    source_asset_artifact: str | None,
    message: str,
) -> None:
    path = tmp_path / "conflicting-result.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _create_team_workspace(connection, "workspace-a")
            _create_team_workspace(connection, "workspace-b")
            _create_artifact(connection, artifact_id="artifact-a", workspace_id="workspace-a")
            _create_artifact(connection, artifact_id="artifact-b", workspace_id="workspace-b")
            _create_source_asset(
                connection,
                asset_id="asset-b",
                workspace_id="workspace-b",
                artifact_id=source_asset_artifact,
            )
            _insert_upload(
                connection,
                "upload-conflicting-result",
                result_json='{"artifact_id":"artifact-a","source_asset_id":"asset-b"}',
            )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match=message):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_root", ["missing_project", "missing_source", "wrong_source_project"])
def test_session_project_root_graph_must_be_valid_before_ddl(
    schema_at_0064: Path, tmp_path: Path, bad_root: str
) -> None:
    path = tmp_path / f"bad-root-{bad_root}.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-root-test"
            _create_team_workspace(connection, workspace_id)
            _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-root-test",
                root_session_id="session-root-test",
                upload_session_id="session-upload-test",
            )
            _insert_upload(connection, "upload-bad-root", session_id="session-upload-test")
            if bad_root == "missing_project":
                connection.execute(
                    sa.text("UPDATE sessions SET project_id = 'missing-project' WHERE id = :id"),
                    {"id": "session-upload-test"},
                )
            elif bad_root == "missing_source":
                connection.execute(
                    sa.text("UPDATE projects SET source_session_id = NULL WHERE id = :id"),
                    {"id": "project-root-test"},
                )
            else:
                connection.execute(
                    sa.text("UPDATE sessions SET project_id = NULL WHERE id = :id"),
                    {"id": "session-root-test"},
                )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_non_null_translation_root_must_agree_before_ddl(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "translation-root.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-translation-test"
            _create_team_workspace(connection, workspace_id)
            project_id, session_id = _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-translation-test",
                root_session_id="session-translation-root",
            )
            _insert_upload(connection, "upload-translation-root", session_id=session_id)
            _create_artifact(
                connection,
                artifact_id="translation-checkpoint",
                workspace_id=workspace_id,
            )
            connection.execute(
                sa.text(
                    "INSERT INTO translation_projects "
                    "(id, project_root_id, name, source_session_id, checkpoint_artifact_id, "
                    "source_content_hash, source_language, revision, created_at, updated_at) "
                    "VALUES ('translation-test', 'wrong-project', 'Translation', :session_id, "
                    "'translation-checkpoint', 'hash', 'en', 1, :now, :now)"
                ),
                {"session_id": session_id, "now": TIMESTAMP},
            )
            assert project_id == "project-translation-test"
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match="conflicts with translation ownership"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("malformation", ["partial", "wrong_type", "populated"])
def test_partial_malformed_or_populated_early_target_fails_atomically(
    schema_at_0064: Path, tmp_path: Path, malformation: str
) -> None:
    path = tmp_path / f"bad-target-{malformation}.sqlite"
    shutil.copy2(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            if malformation == "partial":
                connection.exec_driver_sql("DROP INDEX ix_upload_sessions_workspace_id")
                connection.exec_driver_sql("DROP INDEX ix_upload_sessions_project_id")
                connection.exec_driver_sql("DROP TABLE upload_sessions")
                connection.exec_driver_sql(
                    """CREATE TABLE upload_sessions (
                        id VARCHAR(36) NOT NULL, session_id VARCHAR(36),
                        filename VARCHAR(255) NOT NULL, mime_type VARCHAR(160),
                        size_bytes INTEGER NOT NULL, chunk_size INTEGER NOT NULL,
                        chunk_count INTEGER NOT NULL, received_json JSON NOT NULL,
                        expected_hash VARCHAR(128), temporary_relative_path TEXT NOT NULL,
                        state VARCHAR(32) NOT NULL, result_json JSON, created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL, expires_at DATETIME NOT NULL,
                        workspace_id VARCHAR(36), PRIMARY KEY (id),
                        FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
                    )"""
                )
                for index_name, column in (
                    ("ix_upload_sessions_session_id", "session_id"),
                    ("ix_upload_sessions_state", "state"),
                    ("ix_upload_sessions_expires_at", "expires_at"),
                ):
                    connection.exec_driver_sql(
                        f"CREATE INDEX {index_name} ON upload_sessions ({column})"
                    )
            elif malformation == "wrong_type":
                _make_legacy_upload_table(connection)
                connection.exec_driver_sql(
                    "ALTER TABLE upload_sessions ADD COLUMN workspace_id VARCHAR(20) "
                    "REFERENCES workspaces(id) ON DELETE RESTRICT"
                )
                connection.exec_driver_sql(
                    "ALTER TABLE upload_sessions ADD COLUMN project_id VARCHAR(36) "
                    "REFERENCES projects(id) ON DELETE RESTRICT"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_upload_sessions_workspace_id ON upload_sessions (workspace_id)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_upload_sessions_project_id ON upload_sessions (project_id)"
                )
            else:
                _insert_upload(connection, "upload-populated-target")
                connection.execute(
                    sa.text(
                        "UPDATE upload_sessions SET workspace_id = :workspace_id "
                        "WHERE id = :upload_id"
                    ),
                    {
                        "workspace_id": _owner_workspace(connection),
                        "upload_id": "upload-populated-target",
                    },
                )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_late_failure_rolls_back_ddl_and_partial_backfill_then_retry_repeat_and_downgrade(
    schema_at_0064: Path, tmp_path: Path
) -> None:
    path = tmp_path / "late-rollback.sqlite"
    _legacy_database(schema_at_0064, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX ix_custom_upload_filename ON upload_sessions (filename)"
            )
            workspace_id = _owner_workspace(connection)
            project_id, session_id = _create_project_session(
                connection,
                workspace_id=workspace_id,
                project_id="project-late-test",
                root_session_id="session-late-test",
            )
            _insert_upload(connection, "upload-a", session_id=session_id)
            _insert_upload(connection, "upload-b")
        with engine.connect() as connection:
            before = _database_snapshot(connection)
            old_columns = _upload_columns(connection)
            old_rows = _upload_rows(connection)
            old_foreign_keys = sa.inspect(connection).get_foreign_keys("upload_sessions")
            old_indexes = sa.inspect(connection).get_indexes("upload_sessions")

        updated_rows = 0

        def fail_after_first_backfill(
            connection: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            nonlocal updated_rows
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("update upload_sessions set workspace_id"):
                updated_rows += 1
                if updated_rows == 2:
                    raise RuntimeError("injected late upload backfill failure")

        event.listen(Engine, "before_cursor_execute", fail_after_first_backfill)
        try:
            with pytest.raises(RuntimeError, match="injected late upload backfill failure"):
                command.upgrade(_config(path), REVISION)
        finally:
            event.remove(Engine, "before_cursor_execute", fail_after_first_backfill)

        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
            assert not any(
                name.startswith("_alembic_tmp")
                for (name,) in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
                )
            )

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            rows = connection.execute(
                sa.text("SELECT id, workspace_id, project_id FROM upload_sessions ORDER BY id")
            ).all()
            assert rows == [
                ("upload-a", workspace_id, project_id),
                ("upload-b", workspace_id, None),
            ]
        with engine.connect() as connection:
            upgraded_snapshot = _database_snapshot(connection)
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == upgraded_snapshot

        command.downgrade(_config(path), PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert _version(connection) == PREVIOUS_REVISION
            assert _upload_columns(connection) == old_columns
            assert _upload_rows(connection) == old_rows
            current_fks = sa.inspect(connection).get_foreign_keys("upload_sessions")
            current_indexes = sa.inspect(connection).get_indexes("upload_sessions")
            assert len(current_fks) == len(old_foreign_keys) == 1
            assert {index["name"] for index in current_indexes} == {
                index["name"] for index in old_indexes
            }
            assert not {"workspace_id", "project_id"} & set(_upload_columns(connection))
    finally:
        engine.dispose()
