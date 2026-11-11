from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event, text

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    Artifact,
    Project,
    SessionRecord,
    SessionSource,
    SourceAsset,
    SourceRecord,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.projects.identity import create_session_project
from tests.legacy_schema_support import insert_upload_session

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
TARGET_REVISION = "0077_media_scope_consistency"
PREDECESSOR_REVISION = "0076_project_graph_consistency"
MEDIA_PREFIX = "pandrator_media_scope_"
TRIGGER_NAMES = (
    "pandrator_media_scope_artifacts_insert",
    "pandrator_media_scope_artifacts_update",
    "pandrator_media_scope_source_assets_insert",
    "pandrator_media_scope_source_assets_update",
    "pandrator_media_scope_upload_sessions_insert",
    "pandrator_media_scope_upload_sessions_update",
    "pandrator_media_scope_sources_insert",
    "pandrator_media_scope_sources_update",
    "pandrator_media_scope_session_sources_insert",
    "pandrator_media_scope_session_sources_update",
)

_SqlParams: TypeAlias = dict[str, object]
_NOW = "2026-10-24T12:00:00+00:00"

_INSERT_ARTIFACT = """INSERT INTO artifacts (
    id, workspace_id, project_id, session_id, kind, role, relative_path,
    state, metadata_json, created_at, updated_at
) VALUES (
    :id, :workspace_id, :project_id, :session_id, 'txt', 'fixture',
    :relative_path, 'current', '{}', :created_at, :updated_at
)"""
_INSERT_SOURCE_ASSET = """INSERT INTO source_assets (
    id, workspace_id, artifact_id, display_name, kind, state, metadata_json,
    revision, created_at, updated_at
) VALUES (
    :id, :workspace_id, :artifact_id, 'fixture source', 'document',
    'current', '{}', 1, :created_at, :updated_at
)"""
_INSERT_UPLOAD = """INSERT INTO upload_sessions (
    id, session_id, workspace_id, project_id, filename, size_bytes,
    chunk_size, chunk_count, received_json, temporary_relative_path,
    state, created_at, updated_at, expires_at
) VALUES (
    :id, :session_id, :workspace_id, :project_id, 'fixture.txt', 1,
    1, 1, '{}', 'media-scope/fixture.upload', 'open',
    :created_at, :updated_at, :expires_at
)"""
_INSERT_SOURCE = """INSERT INTO sources (
    id, session_id, kind, display_name, artifact_id, metadata_json, created_at
) VALUES (
    :id, :session_id, 'fixture', 'fixture source', :artifact_id, '{}', :created_at
)"""
_INSERT_SESSION_SOURCE = """INSERT INTO session_sources (
    id, session_id, source_asset_id, role, is_current, revision,
    created_at, updated_at
) VALUES (
    :id, :session_id, :source_asset_id, 'fixture', 1, 1, :created_at, :updated_at
)"""

IDS = {
    "workspace_other": "11000000-0000-4000-8000-000000000001",
    "archived_root": "12000000-0000-4000-8000-000000000001",
    "archived_source": "12000000-0000-4000-8000-000000000002",
    "workspace_source": "13000000-0000-4000-8000-000000000001",
    "workspace_branch": "13000000-0000-4000-8000-000000000002",
    "workspace_other_source": "13000000-0000-4000-8000-000000000003",
    "artifact_source": "21000000-0000-4000-8000-000000000001",
    "artifact_branch": "21000000-0000-4000-8000-000000000002",
    "artifact_project": "21000000-0000-4000-8000-000000000003",
    "artifact_workspace": "21000000-0000-4000-8000-000000000004",
    "artifact_independent": "21000000-0000-4000-8000-000000000005",
    "artifact_archived": "21000000-0000-4000-8000-000000000006",
    "asset_source": "31000000-0000-4000-8000-000000000001",
    "asset_empty": "31000000-0000-4000-8000-000000000002",
    "asset_archived": "31000000-0000-4000-8000-000000000003",
    "upload_session": "41000000-0000-4000-8000-000000000001",
    "upload_project": "41000000-0000-4000-8000-000000000002",
    "upload_workspace": "41000000-0000-4000-8000-000000000003",
    "upload_archived": "41000000-0000-4000-8000-000000000004",
    "source_branch": "51000000-0000-4000-8000-000000000001",
    "source_independent": "51000000-0000-4000-8000-000000000002",
    "source_null": "51000000-0000-4000-8000-000000000003",
    "attachment_source": "61000000-0000-4000-8000-000000000001",
    "attachment_independent": "61000000-0000-4000-8000-000000000002",
    "empty_root": "71000000-0000-4000-8000-000000000001",
}


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _upgrade_to_0076(path: Path) -> Config:
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    return config


def _make_valid_predecessor(path: Path) -> dict[str, str]:
    _upgrade_to_0076(path)
    database = Database(path)
    try:
        with database.session() as session:
            workspace = initial_personal_workspace(session)
            archived_workspace = Workspace(
                id=IDS["workspace_other"],
                kind="team",
                name="Archived workspace",
                state="archived",
            )
            session.add(archived_workspace)
            session.flush()

            source = SessionRecord(
                id=IDS["workspace_source"],
                name="English source",
                source_language="en",
            )
            root = create_session_project(session, source)
            branch = SessionRecord(
                id=IDS["workspace_branch"],
                name="German branch",
                source_language="en",
                target_language="de",
            )
            create_session_project(session, branch, project_id=root.id)
            other_source = SessionRecord(
                id=IDS["workspace_other_source"],
                name="Independent source",
                source_language="pl",
            )
            other_root = create_session_project(session, other_source, workspace_id=workspace.id)

            archived_root = Project(
                id=IDS["archived_root"],
                workspace_id=archived_workspace.id,
                source_session_id=None,
                name="Archived project",
                created_by_user_id=None,
            )
            session.add(archived_root)
            session.flush()
            archived_source = SessionRecord(
                id=IDS["archived_source"],
                project_id=archived_root.id,
                name="Archived source",
                source_language="en",
            )
            session.add(archived_source)
            session.flush()
            archived_root.source_session_id = archived_source.id
            session.flush([archived_root])

            empty_root = Project(
                id=IDS["empty_root"],
                workspace_id=workspace.id,
                source_session_id=None,
                name="Retained empty root",
                created_by_user_id=None,
            )
            session.add(empty_root)
            session.flush()

            artifact_source = Artifact(
                id=IDS["artifact_source"],
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=source.id,
                kind="txt",
                role="source",
                relative_path="media-scope/source.txt",
                content_hash="a" * 64,
            )
            artifact_branch = Artifact(
                id=IDS["artifact_branch"],
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=branch.id,
                kind="txt",
                role="branch",
                relative_path="media-scope/branch.txt",
                content_hash="b" * 64,
            )
            artifact_project = Artifact(
                id=IDS["artifact_project"],
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=None,
                kind="txt",
                role="project-only",
                relative_path="media-scope/project-only.txt",
                content_hash="c" * 64,
            )
            artifact_workspace = Artifact(
                id=IDS["artifact_workspace"],
                workspace_id=workspace.id,
                project_id=None,
                session_id=None,
                kind="txt",
                role="workspace-only",
                relative_path="media-scope/workspace-only.txt",
                content_hash="d" * 64,
            )
            artifact_independent = Artifact(
                id=IDS["artifact_independent"],
                workspace_id=workspace.id,
                project_id=other_root.id,
                session_id=other_source.id,
                kind="txt",
                role="independent-project",
                relative_path="media-scope/independent.txt",
                content_hash="e" * 64,
            )
            artifact_archived = Artifact(
                id=IDS["artifact_archived"],
                workspace_id=archived_workspace.id,
                project_id=archived_root.id,
                session_id=archived_source.id,
                kind="txt",
                role="archived",
                relative_path="media-scope/archived.txt",
                content_hash="f" * 64,
            )
            session.add_all(
                [
                    artifact_source,
                    artifact_branch,
                    artifact_project,
                    artifact_workspace,
                    artifact_independent,
                    artifact_archived,
                ]
            )
            session.flush()

            translation = TranslationProject(
                project_root_id=root.id,
                name="English and German",
                source_session_id=source.id,
                checkpoint_artifact_id=artifact_source.id,
                source_content_hash=artifact_source.content_hash,
                source_language="en",
            )
            session.add(translation)
            session.flush()
            session.add(
                TranslationProjectBranch(
                    project_id=translation.id,
                    session_id=branch.id,
                    target_language="de",
                    source_checkpoint_artifact_id=artifact_source.id,
                    source_content_hash=artifact_source.content_hash,
                )
            )

            asset_source = SourceAsset(
                id=IDS["asset_source"],
                workspace_id=workspace.id,
                artifact_id=artifact_source.id,
                display_name="Shared source",
                kind="document",
            )
            asset_empty = SourceAsset(
                id=IDS["asset_empty"],
                workspace_id=workspace.id,
                artifact_id=None,
                display_name="External source",
                kind="document",
            )
            asset_archived = SourceAsset(
                id=IDS["asset_archived"],
                workspace_id=archived_workspace.id,
                artifact_id=artifact_archived.id,
                display_name="Archived source asset",
                kind="document",
            )
            session.add_all([asset_source, asset_empty, asset_archived])
            session.flush()

            expires_at = datetime.now(UTC) + timedelta(days=1)
            for upload_id, project_id, session_id, filename, relative_path in (
                (
                    IDS["upload_session"],
                    root.id,
                    source.id,
                    "source.txt",
                    "media-scope/source.upload",
                ),
                (
                    IDS["upload_project"],
                    other_root.id,
                    None,
                    "project.txt",
                    "media-scope/project.upload",
                ),
                (
                    IDS["upload_workspace"],
                    None,
                    None,
                    "workspace.txt",
                    "media-scope/workspace.upload",
                ),
                (
                    IDS["upload_archived"],
                    archived_root.id,
                    archived_source.id,
                    "archived.txt",
                    "media-scope/archived.upload",
                ),
            ):
                insert_upload_session(
                    session,
                    upload_id=upload_id,
                    workspace_id=(
                        archived_workspace.id
                        if upload_id == IDS["upload_archived"]
                        else workspace.id
                    ),
                    project_id=project_id,
                    session_id=session_id,
                    filename=filename,
                    size_bytes=10,
                    chunk_size=10,
                    chunk_count=1,
                    temporary_relative_path=relative_path,
                    expires_at=expires_at,
                )
            session.add_all(
                [
                    SourceRecord(
                        id=IDS["source_branch"],
                        session_id=source.id,
                        artifact_id=artifact_branch.id,
                        kind="source",
                        display_name="Branch artifact reused from source",
                    ),
                    SourceRecord(
                        id=IDS["source_independent"],
                        session_id=other_source.id,
                        artifact_id=artifact_source.id,
                        kind="source",
                        display_name="Same-workspace reusable artifact",
                    ),
                    SourceRecord(
                        id=IDS["source_null"],
                        session_id=source.id,
                        artifact_id=None,
                        kind="source",
                        display_name="Source without artifact",
                    ),
                    SessionSource(
                        id=IDS["attachment_source"],
                        session_id=source.id,
                        source_asset_id=asset_source.id,
                        role="primary",
                    ),
                    SessionSource(
                        id=IDS["attachment_independent"],
                        session_id=other_source.id,
                        source_asset_id=asset_source.id,
                        role="reference",
                    ),
                ]
            )

            ids = {
                "workspace": workspace.id,
                "workspace_other": archived_workspace.id,
                "root": root.id,
                "source": source.id,
                "branch": branch.id,
                "other_root": other_root.id,
                "other_source": other_source.id,
                "archived_root": archived_root.id,
                "archived_source": archived_source.id,
                "empty_root": empty_root.id,
                "artifact_source": artifact_source.id,
                "artifact_branch": artifact_branch.id,
                "artifact_project": artifact_project.id,
                "artifact_workspace": artifact_workspace.id,
                "artifact_independent": artifact_independent.id,
                "artifact_archived": artifact_archived.id,
                "asset_source": asset_source.id,
                "asset_empty": asset_empty.id,
                "asset_archived": asset_archived.id,
                "upload_session": IDS["upload_session"],
                "upload_project": IDS["upload_project"],
                "upload_workspace": IDS["upload_workspace"],
                "upload_archived": IDS["upload_archived"],
                "source_branch": IDS["source_branch"],
                "source_independent": IDS["source_independent"],
                "source_null": IDS["source_null"],
                "attachment_source": IDS["attachment_source"],
                "attachment_independent": IDS["attachment_independent"],
            }
    finally:
        database.dispose()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fixture_unrelated_media_scope_noop "
            "AFTER UPDATE OF role ON artifacts BEGIN SELECT NEW.id; END"
        )
    return ids


def _make_lifecycle_predecessor(path: Path) -> dict[str, str]:
    _upgrade_to_0076(path)
    database = Database(path)
    try:
        with database.session() as session:
            workspace = initial_personal_workspace(session)
            source = SessionRecord(
                id="15000000-0000-4000-8000-000000000001",
                name="Purge source",
                source_language="en",
            )
            root = create_session_project(session, source)
            artifact_deleted = Artifact(
                id="25000000-0000-4000-8000-000000000001",
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=None,
                kind="txt",
                role="delete-test",
                relative_path="media-scope/lifecycle/delete.txt",
            )
            artifact_owned = Artifact(
                id="25000000-0000-4000-8000-000000000002",
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=source.id,
                kind="txt",
                role="session-owned",
                relative_path="media-scope/lifecycle/session.txt",
            )
            artifact_retained = Artifact(
                id="25000000-0000-4000-8000-000000000003",
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=None,
                kind="txt",
                role="detached-retained",
                relative_path="media-scope/lifecycle/retained.txt",
            )
            session.add_all([artifact_deleted, artifact_owned, artifact_retained])
            session.flush()

            asset_deleted = SourceAsset(
                id="35000000-0000-4000-8000-000000000001",
                workspace_id=workspace.id,
                artifact_id=artifact_deleted.id,
                display_name="Delete target asset",
                kind="document",
            )
            asset_owned = SourceAsset(
                id="35000000-0000-4000-8000-000000000002",
                workspace_id=workspace.id,
                artifact_id=artifact_owned.id,
                display_name="Session-owned asset",
                kind="document",
            )
            asset_retained = SourceAsset(
                id="35000000-0000-4000-8000-000000000003",
                workspace_id=workspace.id,
                artifact_id=artifact_retained.id,
                display_name="Retained asset",
                kind="document",
            )
            session.add_all([asset_deleted, asset_owned, asset_retained])
            session.flush()

            expires_at = datetime.now(UTC) + timedelta(days=1)
            insert_upload_session(
                session,
                upload_id="45000000-0000-4000-8000-000000000001",
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=source.id,
                filename="purge.txt",
                size_bytes=1,
                chunk_size=1,
                chunk_count=1,
                temporary_relative_path="media-scope/lifecycle/purge.upload",
                expires_at=expires_at,
            )
            session.add_all(
                [
                    SourceRecord(
                        id="55000000-0000-4000-8000-000000000001",
                        session_id=source.id,
                        artifact_id=artifact_deleted.id,
                        kind="fixture",
                        display_name="Artifact delete reference",
                    ),
                    SourceRecord(
                        id="55000000-0000-4000-8000-000000000002",
                        session_id=source.id,
                        artifact_id=artifact_owned.id,
                        kind="fixture",
                        display_name="Purged session artifact reference",
                    ),
                    SessionSource(
                        id="65000000-0000-4000-8000-000000000001",
                        session_id=source.id,
                        source_asset_id=asset_deleted.id,
                        role="delete-test",
                    ),
                    SessionSource(
                        id="65000000-0000-4000-8000-000000000002",
                        session_id=source.id,
                        source_asset_id=asset_owned.id,
                        role="purge-test",
                    ),
                ]
            )
            ids = {
                "workspace": workspace.id,
                "root": root.id,
                "source": source.id,
                "artifact_deleted": artifact_deleted.id,
                "artifact_owned": artifact_owned.id,
                "artifact_retained": artifact_retained.id,
                "asset_deleted": asset_deleted.id,
                "asset_owned": asset_owned.id,
                "asset_retained": asset_retained.id,
            }
    finally:
        database.dispose()
    return ids


def _snapshot(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        schema = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        )
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        rows: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            values = connection.execute(f"SELECT * FROM {quoted_name}").fetchall()
            rows[table_name] = tuple(sorted((tuple(row) for row in values), key=repr))
    return {"schema": schema, "rows": rows}


def _rows_without_alembic_version(snapshot: dict[str, object]) -> dict[str, object]:
    rows = snapshot["rows"]
    assert isinstance(rows, dict)
    return {name: value for name, value in rows.items() if name != "alembic_version"}


def _media_triggers(path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    return tuple(
        (str(name), str(table_name), str(sql))
        for name, table_name, sql in rows
        if str(name).startswith(MEDIA_PREFIX)
    )


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _assert_abort(
    connection: sqlite3.Connection,
    statement: str,
    parameters: _SqlParams,
    message: str,
) -> None:
    before = _connection_rows(connection)
    with pytest.raises(sqlite3.IntegrityError, match=re.escape(message)):
        connection.execute(statement, parameters)
    assert _connection_rows(connection) == before


def _connection_rows(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    rows: dict[str, tuple[tuple[object, ...], ...]] = {}
    for table_name in table_names:
        quoted_name = '"' + table_name.replace('"', '""') + '"'
        values = connection.execute(f"SELECT * FROM {quoted_name}").fetchall()
        rows[table_name] = tuple(sorted((tuple(row) for row in values), key=repr))
    return rows


def _artifact_parameters(
    row_id: str, workspace_id: str, project_id: str | None, session_id: str | None
) -> _SqlParams:
    return {
        "id": row_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "session_id": session_id,
        "relative_path": f"media-scope/raw/{row_id}.txt",
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _source_asset_parameters(row_id: str, workspace_id: str, artifact_id: str | None) -> _SqlParams:
    return {
        "id": row_id,
        "workspace_id": workspace_id,
        "artifact_id": artifact_id,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _upload_parameters(
    row_id: str,
    workspace_id: str,
    project_id: str | None,
    session_id: str | None,
) -> _SqlParams:
    return {
        "id": row_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "session_id": session_id,
        "created_at": _NOW,
        "updated_at": _NOW,
        "expires_at": "2026-10-25T12:00:00+00:00",
    }


def _source_parameters(row_id: str, session_id: str, artifact_id: str | None) -> _SqlParams:
    return {
        "id": row_id,
        "session_id": session_id,
        "artifact_id": artifact_id,
        "created_at": _NOW,
    }


def _session_source_parameters(row_id: str, session_id: str, source_asset_id: str) -> _SqlParams:
    return {
        "id": row_id,
        "session_id": session_id,
        "source_asset_id": source_asset_id,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _foreign_key_violations(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def test_fresh_upgrade_installs_exact_media_triggers_and_repeats_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh-media-scope.sqlite3"
    config = _config(path)

    command.upgrade(config, TARGET_REVISION)

    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _media_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )
    assert _foreign_key_violations(path) == []
    before_repeat = _snapshot(path)
    command.upgrade(config, TARGET_REVISION)
    assert _snapshot(path) == before_repeat


def test_populated_upgrade_downgrade_and_reupgrade_preserve_prior_objects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "populated-media-scope.sqlite3"
    config = _config(path)
    ids = _make_valid_predecessor(path)
    before = _snapshot(path)

    command.upgrade(config, TARGET_REVISION)

    after = _snapshot(path)
    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _media_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )
    before_schema = before["schema"]
    after_schema = after["schema"]
    assert isinstance(before_schema, tuple) and isinstance(after_schema, tuple)
    assert tuple(row for row in after_schema if row[1] not in TRIGGER_NAMES) == before_schema
    assert _rows_without_alembic_version(after) == _rows_without_alembic_version(before)
    assert _foreign_key_violations(path) == []
    assert ids["workspace_other"] != ids["workspace"]

    with sqlite3.connect(path) as connection:
        unrelated = connection.execute(
            "SELECT tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'fixture_unrelated_media_scope_noop'"
        ).fetchone()
    assert unrelated == (
        "artifacts",
        "CREATE TRIGGER fixture_unrelated_media_scope_noop "
        "AFTER UPDATE OF role ON artifacts BEGIN SELECT NEW.id; END",
    )

    command.downgrade(config, PREDECESSOR_REVISION)
    downgraded = _snapshot(path)
    assert _version(path) == PREDECESSOR_REVISION
    assert _media_triggers(path) == ()
    assert _rows_without_alembic_version(downgraded) == _rows_without_alembic_version(before)
    downgraded_schema = downgraded["schema"]
    assert isinstance(downgraded_schema, tuple)
    assert tuple(row for row in after_schema if row[1] not in TRIGGER_NAMES) == downgraded_schema

    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _media_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )
    reupgraded = _snapshot(path)
    assert reupgraded == after
    assert _rows_without_alembic_version(reupgraded) == _rows_without_alembic_version(before)
    assert _foreign_key_violations(path) == []


def test_raw_media_triggers_reject_invalid_roots_and_allow_supported_edits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw-media-scope.sqlite3"
    config = _upgrade_to_0076(path)
    ids = _make_valid_predecessor(path)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        _assert_abort(
            connection,
            _INSERT_ARTIFACT,
            _artifact_parameters(
                "91000000-0000-4000-8000-000000000001",
                "90000000-0000-4000-8000-000000000001",
                None,
                None,
            ),
            "artifact workspace does not exist",
        )
        _assert_abort(
            connection,
            _INSERT_ARTIFACT,
            _artifact_parameters(
                "91000000-0000-4000-8000-000000000002",
                ids["workspace"],
                ids["archived_root"],
                None,
            ),
            "artifact project must share its workspace",
        )
        _assert_abort(
            connection,
            _INSERT_ARTIFACT,
            _artifact_parameters(
                "91000000-0000-4000-8000-000000000003",
                ids["workspace"],
                None,
                ids["source"],
            ),
            "artifact session must belong to its project",
        )
        _assert_abort(
            connection,
            _INSERT_ARTIFACT,
            _artifact_parameters(
                "91000000-0000-4000-8000-000000000004",
                ids["workspace"],
                ids["root"],
                ids["archived_source"],
            ),
            "artifact session must belong to its project",
        )

        artifact_before = connection.execute(
            "SELECT workspace_id, project_id, session_id FROM artifacts WHERE id = ?",
            (ids["artifact_workspace"],),
        ).fetchone()
        _assert_abort(
            connection,
            "UPDATE artifacts SET workspace_id = :workspace WHERE id = :id",
            {"workspace": ids["workspace_other"], "id": ids["artifact_workspace"]},
            "artifact workspace is immutable",
        )
        _assert_abort(
            connection,
            "UPDATE artifacts SET project_id = :project WHERE id = :id",
            {"project": ids["root"], "id": ids["artifact_workspace"]},
            "artifact project is immutable",
        )
        assert (
            connection.execute(
                "SELECT workspace_id, project_id, session_id FROM artifacts WHERE id = ?",
                (ids["artifact_workspace"],),
            ).fetchone()
            == artifact_before
        )

        _assert_abort(
            connection,
            "UPDATE artifacts SET project_id = NULL WHERE id = :id",
            {"id": ids["artifact_project"]},
            "artifact project is immutable",
        )
        _assert_abort(
            connection,
            "UPDATE artifacts SET project_id = :project WHERE id = :id",
            {"project": ids["other_root"], "id": ids["artifact_project"]},
            "artifact project is immutable",
        )
        connection.execute(
            "UPDATE artifacts SET session_id = :session WHERE id = :id",
            {"session": ids["branch"], "id": ids["artifact_project"]},
        )
        connection.execute(
            "UPDATE artifacts SET session_id = NULL WHERE id = :id",
            {"id": ids["artifact_project"]},
        )
        connection.execute(
            "UPDATE artifacts SET session_id = :session WHERE id = :id",
            {"session": ids["source"], "id": ids["artifact_project"]},
        )
        _assert_abort(
            connection,
            "UPDATE artifacts SET session_id = :session WHERE id = :id",
            {"session": ids["other_source"], "id": ids["artifact_project"]},
            "artifact session must belong to its project",
        )
        connection.execute(
            "UPDATE artifacts SET workspace_id = workspace_id, project_id = project_id, "
            "session_id = session_id, role = 'metadata-edit' WHERE id = :id",
            {"id": ids["artifact_project"]},
        )

        _assert_abort(
            connection,
            _INSERT_SOURCE_ASSET,
            _source_asset_parameters(
                "92000000-0000-4000-8000-000000000001",
                "90000000-0000-4000-8000-000000000001",
                None,
            ),
            "source asset workspace does not exist",
        )
        _assert_abort(
            connection,
            _INSERT_SOURCE_ASSET,
            _source_asset_parameters(
                "92000000-0000-4000-8000-000000000002",
                ids["workspace"],
                ids["artifact_archived"],
            ),
            "source asset artifact must share its workspace",
        )
        asset_before = connection.execute(
            "SELECT workspace_id, artifact_id FROM source_assets WHERE id = ?",
            (ids["asset_source"],),
        ).fetchone()
        _assert_abort(
            connection,
            "UPDATE source_assets SET workspace_id = :workspace WHERE id = :id",
            {"workspace": ids["workspace_other"], "id": ids["asset_source"]},
            "source asset workspace is immutable",
        )
        _assert_abort(
            connection,
            "UPDATE source_assets SET artifact_id = :artifact WHERE id = :id",
            {"artifact": ids["artifact_archived"], "id": ids["asset_source"]},
            "source asset artifact must share its workspace",
        )
        assert (
            connection.execute(
                "SELECT workspace_id, artifact_id FROM source_assets WHERE id = ?",
                (ids["asset_source"],),
            ).fetchone()
            == asset_before
        )
        connection.execute(
            "UPDATE source_assets SET artifact_id = :artifact WHERE id = :id",
            {"artifact": ids["artifact_branch"], "id": ids["asset_source"]},
        )
        connection.execute(
            "UPDATE source_assets SET artifact_id = NULL WHERE id = :id",
            {"id": ids["asset_source"]},
        )
        connection.execute(
            "UPDATE source_assets SET artifact_id = :artifact WHERE id = :id",
            {"artifact": ids["artifact_source"], "id": ids["asset_source"]},
        )

        _assert_abort(
            connection,
            _INSERT_UPLOAD,
            _upload_parameters(
                "93000000-0000-4000-8000-000000000001",
                "90000000-0000-4000-8000-000000000001",
                None,
                None,
            ),
            "upload workspace does not exist",
        )
        _assert_abort(
            connection,
            _INSERT_UPLOAD,
            _upload_parameters(
                "93000000-0000-4000-8000-000000000002",
                ids["workspace"],
                ids["archived_root"],
                None,
            ),
            "upload project must share its workspace",
        )
        _assert_abort(
            connection,
            _INSERT_UPLOAD,
            _upload_parameters(
                "93000000-0000-4000-8000-000000000003",
                ids["workspace"],
                None,
                ids["source"],
            ),
            "upload session must belong to its project",
        )
        _assert_abort(
            connection,
            _INSERT_UPLOAD,
            _upload_parameters(
                "93000000-0000-4000-8000-000000000004",
                ids["workspace"],
                ids["other_root"],
                ids["source"],
            ),
            "upload session must belong to its project",
        )
        upload_before = connection.execute(
            "SELECT workspace_id, project_id, session_id FROM upload_sessions WHERE id = ?",
            (ids["upload_session"],),
        ).fetchone()
        ownership_updates: tuple[tuple[str, _SqlParams], ...] = (
            (
                "UPDATE upload_sessions SET workspace_id = :value WHERE id = :id",
                {"value": ids["workspace_other"], "id": ids["upload_session"]},
            ),
            (
                "UPDATE upload_sessions SET project_id = NULL WHERE id = :id",
                {"id": ids["upload_session"]},
            ),
            (
                "UPDATE upload_sessions SET session_id = NULL WHERE id = :id",
                {"id": ids["upload_session"]},
            ),
            (
                "UPDATE upload_sessions SET project_id = :value WHERE id = :id",
                {"value": ids["other_root"], "id": ids["upload_session"]},
            ),
        )
        for statement, parameters in ownership_updates:
            _assert_abort(connection, statement, parameters, "upload ownership is immutable")
            assert (
                connection.execute(
                    "SELECT workspace_id, project_id, session_id FROM upload_sessions WHERE id = ?",
                    (ids["upload_session"],),
                ).fetchone()
                == upload_before
            )

        _assert_abort(
            connection,
            "UPDATE upload_sessions SET project_id = :project WHERE id = :id",
            {"project": ids["other_root"], "id": ids["upload_workspace"]},
            "upload ownership is immutable",
        )
        _assert_abort(
            connection,
            "UPDATE upload_sessions SET session_id = :session WHERE id = :id",
            {"session": ids["source"], "id": ids["upload_workspace"]},
            "upload ownership is immutable",
        )
        connection.execute(
            "UPDATE upload_sessions SET workspace_id = workspace_id, "
            "project_id = project_id, session_id = session_id, state = 'complete' "
            "WHERE id = :id",
            {"id": ids["upload_workspace"]},
        )
        connection.execute(
            "UPDATE upload_sessions SET state = 'complete', result_json = '{}' WHERE id = :id",
            {"id": ids["upload_archived"]},
        )

        _assert_abort(
            connection,
            _INSERT_SOURCE,
            _source_parameters(
                "94000000-0000-4000-8000-000000000001",
                ids["source"],
                ids["artifact_archived"],
            ),
            "source artifact must share its session workspace",
        )
        _assert_abort(
            connection,
            _INSERT_SOURCE,
            _source_parameters(
                "94000000-0000-4000-8000-000000000002",
                ids["source"],
                "90000000-0000-4000-8000-000000000002",
            ),
            "source artifact must share its session workspace",
        )
        _assert_abort(
            connection,
            _INSERT_SOURCE,
            _source_parameters(
                "94000000-0000-4000-8000-000000000003",
                "90000000-0000-4000-8000-000000000003",
                None,
            ),
            "FOREIGN KEY constraint failed",
        )
        source_before = connection.execute(
            "SELECT session_id, artifact_id FROM sources WHERE id = ?",
            (ids["source_independent"],),
        ).fetchone()
        _assert_abort(
            connection,
            "UPDATE sources SET artifact_id = :artifact WHERE id = :id",
            {"artifact": ids["artifact_archived"], "id": ids["source_independent"]},
            "source artifact must share its session workspace",
        )
        _assert_abort(
            connection,
            "UPDATE sources SET session_id = :session WHERE id = :id",
            {"session": ids["archived_source"], "id": ids["source_independent"]},
            "source artifact must share its session workspace",
        )
        assert (
            connection.execute(
                "SELECT session_id, artifact_id FROM sources WHERE id = ?",
                (ids["source_independent"],),
            ).fetchone()
            == source_before
        )
        _assert_abort(
            connection,
            "UPDATE sources SET session_id = :session, artifact_id = NULL WHERE id = :id",
            {"session": "90000000-0000-4000-8000-000000000004", "id": ids["source_null"]},
            "FOREIGN KEY constraint failed",
        )
        connection.execute(
            "UPDATE sources SET session_id = :session, artifact_id = :artifact WHERE id = :id",
            {
                "session": ids["other_source"],
                "artifact": ids["artifact_source"],
                "id": ids["source_branch"],
            },
        )
        connection.execute(
            "UPDATE sources SET artifact_id = NULL WHERE id = :id",
            {"id": ids["source_branch"]},
        )
        connection.execute(
            "UPDATE sources SET session_id = :session, artifact_id = :artifact WHERE id = :id",
            {
                "session": ids["branch"],
                "artifact": ids["artifact_branch"],
                "id": ids["source_branch"],
            },
        )
        connection.execute(
            "UPDATE sources SET session_id = session_id, artifact_id = artifact_id, "
            "display_name = 'metadata edit' WHERE id = :id",
            {"id": ids["source_branch"]},
        )

        _assert_abort(
            connection,
            _INSERT_SESSION_SOURCE,
            _session_source_parameters(
                "95000000-0000-4000-8000-000000000001",
                ids["source"],
                ids["asset_archived"],
            ),
            "session source asset must share its session workspace",
        )
        _assert_abort(
            connection,
            _INSERT_SESSION_SOURCE,
            _session_source_parameters(
                "95000000-0000-4000-8000-000000000002",
                "90000000-0000-4000-8000-000000000005",
                ids["asset_source"],
            ),
            "session source session must resolve to a workspace",
        )
        _assert_abort(
            connection,
            _INSERT_SESSION_SOURCE,
            _session_source_parameters(
                "95000000-0000-4000-8000-000000000003",
                ids["source"],
                "90000000-0000-4000-8000-000000000006",
            ),
            "session source asset must share its session workspace",
        )
        attachment_before = connection.execute(
            "SELECT session_id, source_asset_id FROM session_sources WHERE id = ?",
            (ids["attachment_source"],),
        ).fetchone()
        _assert_abort(
            connection,
            "UPDATE session_sources SET source_asset_id = :asset WHERE id = :id",
            {"asset": ids["asset_archived"], "id": ids["attachment_source"]},
            "session source asset must share its session workspace",
        )
        assert (
            connection.execute(
                "SELECT session_id, source_asset_id FROM session_sources WHERE id = ?",
                (ids["attachment_source"],),
            ).fetchone()
            == attachment_before
        )
        connection.execute(
            "UPDATE session_sources SET session_id = :session WHERE id = :id",
            {"session": ids["other_source"], "id": ids["attachment_source"]},
        )
        connection.execute(
            "UPDATE session_sources SET source_asset_id = :asset WHERE id = :id",
            {"asset": ids["asset_empty"], "id": ids["attachment_source"]},
        )
        connection.execute(
            "UPDATE session_sources SET session_id = session_id, "
            "source_asset_id = source_asset_id, role = 'metadata edit' WHERE id = :id",
            {"id": ids["attachment_source"]},
        )

        connection.execute(
            "UPDATE artifacts SET role = 'archived metadata edit' WHERE id = :id",
            {"id": ids["artifact_archived"]},
        )
        connection.execute(
            "UPDATE source_assets SET display_name = 'archived metadata edit' WHERE id = :id",
            {"id": ids["asset_archived"]},
        )

    assert _foreign_key_violations(path) == []


def test_fk_deletes_clear_links_and_cascade_session_owned_media_in_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "media-scope-lifecycle.sqlite3"
    config = _upgrade_to_0076(path)
    ids = _make_lifecycle_predecessor(path)
    command.upgrade(config, TARGET_REVISION)
    database = Database(path)
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        with database.session() as session:
            session.delete(session.get(Artifact, ids["artifact_deleted"]))
        with database.session() as session:
            asset = session.get(SourceAsset, ids["asset_deleted"])
            source = session.get(SourceRecord, "55000000-0000-4000-8000-000000000001")
            assert asset is not None and asset.artifact_id is None
            assert source is not None and source.artifact_id is None

        with database.session() as session:
            session.delete(session.get(SessionRecord, ids["source"]))
        with database.session() as session:
            assert session.get(SessionSource, "65000000-0000-4000-8000-000000000001") is None
            assert session.get(SessionSource, "65000000-0000-4000-8000-000000000002") is None
            assert (
                session.scalar(
                    text("SELECT COUNT(*) FROM upload_sessions WHERE id = :id"),
                    {"id": "45000000-0000-4000-8000-000000000001"},
                )
                == 0
            )
            assert session.get(Artifact, ids["artifact_owned"]) is None
            assert session.get(SourceRecord, "55000000-0000-4000-8000-000000000002") is None
            retained = session.get(Artifact, ids["artifact_retained"])
            root = session.get(Project, ids["root"])
            assert retained is not None
            assert retained.workspace_id == ids["workspace"]
            assert retained.project_id == ids["root"]
            assert retained.session_id is None
            assert root is not None and root.source_session_id is None

        with database.session() as session:
            root = session.get(Project, ids["root"])
            assert root is not None
            session.delete(root)
        with database.session() as session:
            retained = session.get(Artifact, ids["artifact_retained"])
            assert retained is not None
            assert retained.workspace_id == ids["workspace"]
            assert retained.project_id is None
            assert retained.session_id is None
    finally:
        database.dispose()

    assert _foreign_key_violations(path) == []


@pytest.mark.parametrize(
    "case",
    [
        "artifact_session_without_project",
        "artifact_missing_workspace",
        "source_asset_cross_workspace",
        "source_asset_missing_artifact",
        "upload_session_without_project",
        "upload_missing_project",
        "source_missing_session_with_null_artifact",
        "source_cross_workspace_artifact",
        "session_source_cross_workspace",
        "session_source_missing_asset",
    ],
)
def test_invalid_0076_media_rows_fail_preflight_without_changes(tmp_path: Path, case: str) -> None:
    path = tmp_path / f"invalid-{case}.sqlite3"
    config = _config(path)
    ids = _make_valid_predecessor(path)
    missing = "99000000-0000-4000-8000-000000000099"

    corruptions: dict[str, tuple[str, _SqlParams, str]] = {
        "artifact_session_without_project": (
            "UPDATE artifacts SET project_id = NULL WHERE id = :id",
            {"id": ids["artifact_source"]},
            "artifacts have inconsistent roots",
        ),
        "artifact_missing_workspace": (
            "UPDATE artifacts SET workspace_id = :value WHERE id = :id",
            {"value": missing, "id": ids["artifact_workspace"]},
            "artifacts have inconsistent roots",
        ),
        "source_asset_cross_workspace": (
            "UPDATE source_assets SET workspace_id = :value WHERE id = :id",
            {"value": ids["workspace_other"], "id": ids["asset_source"]},
            "source assets have inconsistent roots",
        ),
        "source_asset_missing_artifact": (
            "UPDATE source_assets SET artifact_id = :value WHERE id = :id",
            {"value": missing, "id": ids["asset_empty"]},
            "source assets have inconsistent roots",
        ),
        "upload_session_without_project": (
            "UPDATE upload_sessions SET project_id = NULL WHERE id = :id",
            {"id": ids["upload_session"]},
            "upload sessions have inconsistent roots",
        ),
        "upload_missing_project": (
            "UPDATE upload_sessions SET project_id = :value WHERE id = :id",
            {"value": missing, "id": ids["upload_project"]},
            "upload sessions have inconsistent roots",
        ),
        "source_missing_session_with_null_artifact": (
            "UPDATE sources SET session_id = :value, artifact_id = NULL WHERE id = :id",
            {"value": missing, "id": ids["source_branch"]},
            "sources have inconsistent roots",
        ),
        "source_cross_workspace_artifact": (
            "UPDATE sources SET artifact_id = :value WHERE id = :id",
            {"value": ids["artifact_archived"], "id": ids["source_branch"]},
            "sources have inconsistent roots",
        ),
        "session_source_cross_workspace": (
            "UPDATE session_sources SET source_asset_id = :value WHERE id = :id",
            {"value": ids["asset_archived"], "id": ids["attachment_source"]},
            "session sources have inconsistent roots",
        ),
        "session_source_missing_asset": (
            "UPDATE session_sources SET source_asset_id = :value WHERE id = :id",
            {"value": missing, "id": ids["attachment_source"]},
            "session sources have inconsistent roots",
        ),
    }
    statement, parameters, message_fragment = corruptions[case]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(statement, parameters)
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match=re.escape(message_fragment)):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_malformed_schema_fails_before_ddl_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "malformed-media-scope.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "CREATE TABLE source_assets_replacement ("
            "id VARCHAR(36) PRIMARY KEY NOT NULL, "
            "workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT, "
            "artifact_id VARCHAR(36) REFERENCES artifacts(id) ON DELETE CASCADE)"
        )
        connection.execute(
            "INSERT INTO source_assets_replacement (id, workspace_id, artifact_id) "
            "SELECT id, workspace_id, artifact_id FROM source_assets"
        )
        connection.execute("DROP TABLE source_assets")
        connection.execute("ALTER TABLE source_assets_replacement RENAME TO source_assets")
    before = _snapshot(path)

    with pytest.raises(
        RuntimeError,
        match="media ownership foreign key has an unexpected shape",
    ):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_missing_required_table_fails_before_ddl_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "missing-table-media-scope.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE session_sources")
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="a required table is missing"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


@pytest.mark.parametrize("prerequisite", ["missing", "altered"])
def test_missing_or_altered_0076_trigger_fails_before_media_ddl(
    tmp_path: Path, prerequisite: str
) -> None:
    path = tmp_path / f"prerequisite-{prerequisite}.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER pandrator_project_graph_projects_update")
        if prerequisite == "altered":
            connection.execute(
                "CREATE TRIGGER pandrator_project_graph_projects_update "
                "BEFORE UPDATE OF workspace_id, source_session_id ON projects "
                "BEGIN SELECT RAISE(ABORT, 'altered project trigger'); END"
            )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="project graph trigger is missing or altered"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_case_insensitive_trigger_name_collision_fails_before_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "media-trigger-collision.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER PANDRATOR_MEDIA_SCOPE_ARTIFACTS_INSERT "
            "AFTER UPDATE OF role ON artifacts BEGIN SELECT NEW.id; END"
        )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="an owned trigger name is already in use"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_trigger_ddl_failure_rolls_back_and_retry_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "media-trigger-ddl-failure.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    before = _snapshot(path)
    create_count = 0

    def fail_after_four_triggers(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal create_count
        if statement.lstrip().upper().startswith(f"CREATE TRIGGER {MEDIA_PREFIX.upper()}"):
            create_count += 1
            if create_count == 5:
                raise RuntimeError("Injected media scope trigger creation failure")

    event.listen(Engine, "before_cursor_execute", fail_after_four_triggers)
    try:
        with pytest.raises(RuntimeError, match="Injected media scope trigger creation failure"):
            command.upgrade(config, TARGET_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_four_triggers)

    assert create_count == 5
    assert _snapshot(path) == before
    assert _media_triggers(path) == ()
    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _media_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )


@pytest.mark.parametrize("corruption", ["missing", "altered"])
def test_downgrade_preflights_every_owned_trigger_before_dropping(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / f"downgrade-{corruption}-media-trigger.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    command.upgrade(config, TARGET_REVISION)
    trigger_name = TRIGGER_NAMES[0]
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")
        if corruption == "altered":
            connection.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON artifacts "
                "BEGIN SELECT RAISE(ABORT, 'altered media trigger'); END"
            )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="an owned trigger is missing or altered"):
        command.downgrade(config, PREDECESSOR_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == TARGET_REVISION
