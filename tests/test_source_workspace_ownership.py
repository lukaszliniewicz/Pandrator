from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, event, inspect

from pandrator.web import database as database_module
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import (
    SCHEMA_HEAD,
    Database,
    sqlite_url,
    upgrade_database,
)
from pandrator.web.models import (
    Artifact,
    SessionRecord,
    SessionSource,
    SourceAsset,
    SourceRecord,
)
from pandrator.web.projects.identity import create_session_project, initial_personal_workspace
from pandrator.web.projects.models import Project
from pandrator.web.projects.source_identity import (
    source_creation_workspace,
    validate_source_attachment,
)

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
WORKSPACE_INDEX = "ix_source_assets_workspace_id"
WORKSPACE_FK = "fk_source_assets_workspace_id_workspaces"
FIXED_TIME = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _new_session(
    session, session_id: str, name: str, *, workspace_id: str | None = None
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        name=name,
        storage_key=f"storage-{session_id}",
        workflow_kind="subtitles",
        source_language="en",
    )
    if workspace_id is None:
        create_session_project(session, record)
        return record

    project = Project(
        workspace_id=workspace_id,
        source_session_id=None,
        name=name,
        created_by_user_id=None,
    )
    session.add(project)
    session.flush([project])
    record.project_id = project.id
    session.add(record)
    session.flush([record])
    project.source_session_id = record.id
    session.flush([project])
    return record


def _new_workspace(session, workspace_id: str, *, state: str = "active") -> Workspace:
    workspace = Workspace(
        id=workspace_id,
        kind="team",
        name=f"Workspace {workspace_id}",
        state=state,
    )
    session.add(workspace)
    session.flush([workspace])
    return workspace


def _source_asset(
    asset_id: str,
    *,
    artifact_id: str | None = None,
    workspace_id: str | None = None,
    state: str = "current",
) -> SourceAsset:
    return SourceAsset(
        id=asset_id,
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        display_name=f"Display {asset_id}",
        kind="srt",
        mime_type="text/plain",
        external_path=f"external/{asset_id}.srt",
        size_bytes=17,
        content_hash=f"hash-{asset_id}",
        state=state,
        metadata_json={"kept": asset_id},
        revision=7,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )


def _make_0059_fixture(
    path: Path,
    *,
    conflicting_duplicate: bool = False,
    prepopulated_owner: bool = False,
) -> dict[str, str]:
    command.upgrade(_config(path), "0059_project_ownership")
    db = Database(path)
    try:
        with db.session() as session:
            personal = initial_personal_workspace(session)
            personal_id = personal.id
            personal_session = _new_session(session, "source-personal", "Personal source")
            alternate = _new_workspace(session, "workspace-archived", state="archived")
            alternate_session = _new_session(
                session,
                "source-archived",
                "Archived source",
                workspace_id=alternate.id,
            )

            artifact = Artifact(
                id="artifact-personal",
                session_id=personal_session.id,
                kind="srt",
                role="source",
                relative_path="sessions/source-personal/source.srt",
                content_hash="a" * 64,
                state="current",
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
            )
            duplicate_artifact = Artifact(
                id="artifact-duplicate",
                session_id=None,
                kind="srt",
                role="source",
                relative_path="sources/duplicate.srt",
                content_hash="c" * 64,
                state="current",
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
            )
            legacy_artifact = Artifact(
                id="artifact-legacy",
                session_id=None,
                kind="srt",
                role="source",
                relative_path="sources/legacy.srt",
                content_hash="b" * 64,
                state="archived",
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
            )
            assets = [
                _source_asset("asset-linked", artifact_id=duplicate_artifact.id, state="trashed"),
                _source_asset("asset-detached-duplicate", artifact_id=duplicate_artifact.id),
                _source_asset("asset-artifact-session", artifact_id=artifact.id),
                _source_asset("asset-legacy", artifact_id=legacy_artifact.id),
                _source_asset("asset-external-attached"),
                _source_asset("asset-sessionless"),
            ]
            if prepopulated_owner:
                assets[0].workspace_id = personal_id
            session.add_all([artifact, duplicate_artifact, legacy_artifact, *assets])
            session.flush()

            attachments = [
                SessionSource(
                    id="attachment-duplicate",
                    session_id=alternate_session.id,
                    source_asset_id="asset-linked",
                    role="legacy-correction",
                    is_current=False,
                    revision=11,
                    created_at=FIXED_TIME,
                ),
                SessionSource(
                    id="attachment-archived",
                    session_id=alternate_session.id,
                    source_asset_id="asset-external-attached",
                    role="secondary",
                    is_current=True,
                    revision=3,
                    created_at=FIXED_TIME,
                ),
            ]
            if conflicting_duplicate:
                attachments.append(
                    SessionSource(
                        id="attachment-conflicting-duplicate",
                        session_id=personal_session.id,
                        source_asset_id="asset-detached-duplicate",
                        role="alternate",
                        is_current=False,
                        revision=2,
                        created_at=FIXED_TIME,
                    )
                )
            session.add_all(attachments)
            session.add_all(
                [
                    SourceRecord(
                        id="legacy-record-personal",
                        session_id=personal_session.id,
                        kind="srt",
                        display_name="Personal legacy source",
                        artifact_id=artifact.id,
                        external_path="legacy/personal.srt",
                        content_hash="a" * 64,
                        metadata_json={"old": True},
                        created_at=FIXED_TIME,
                    ),
                    SourceRecord(
                        id="legacy-record-archived",
                        session_id=alternate_session.id,
                        kind="srt",
                        display_name="Archived legacy source",
                        artifact_id=legacy_artifact.id,
                        external_path="legacy/archived.srt",
                        content_hash="b" * 64,
                        metadata_json={"old": "archived"},
                        created_at=FIXED_TIME,
                    ),
                ]
            )
        return {
            "personal_workspace": personal_id,
            "archived_workspace": alternate.id,
            "personal_session": personal_session.id,
            "archived_session": alternate_session.id,
        }
    finally:
        db.dispose()


def _strip_source_workspace(path: Path) -> None:
    """Model the source_assets shape of a real pre-0060 database at revision 0059."""
    engine = database_module.create_database_engine(path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        operations = Operations(
            MigrationContext.configure(connection, opts={"render_as_batch": True})
        )
        with connection.begin():
            with operations.batch_alter_table("source_assets") as batch:
                batch.drop_index(WORKSPACE_INDEX)
                batch.drop_constraint(WORKSPACE_FK, type_="foreignkey")
                batch.drop_column("workspace_id")
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()


def _schema_snapshot(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def _data_snapshot(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        snapshot: dict[str, list[tuple]] = {}
        for table in tables:
            if table == "alembic_version":
                continue
            columns = [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if not (table == "source_assets" and row[1] == "workspace_id")
            ]
            if not columns:
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(
                f'SELECT {column_sql} FROM "{table}" ORDER BY rowid'
            ).fetchall()
            snapshot[table] = rows
        return snapshot


def _source_workspace_map(path: Path) -> dict[str, str | None]:
    with sqlite3.connect(path) as connection:
        return dict(
            connection.execute("SELECT id, workspace_id FROM source_assets ORDER BY id").fetchall()
        )


def _drop_personal_project_assignment(path: Path, session_id: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET project_id = NULL WHERE id = ?", (session_id,))
        connection.commit()


def test_fresh_install_guards_current_source_workspace_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    upgrade_database(path)

    engine = database_module.create_database_engine(path)
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("source_assets")}
        indexes = {index["name"]: index for index in inspector.get_indexes("source_assets")}
        foreign_keys = inspector.get_foreign_keys("source_assets")
        assert _version(path) == SCHEMA_HEAD
        assert columns["workspace_id"]["nullable"] is False
        assert indexes[WORKSPACE_INDEX]["column_names"] == ["workspace_id"]
        workspace_fk = next(key for key in foreign_keys if key["name"] == WORKSPACE_FK)
        assert workspace_fk["referred_table"] == "workspaces"
        assert workspace_fk["referred_columns"] == ["id"]
        assert (workspace_fk.get("options") or {}).get("ondelete", "").upper() == "RESTRICT"
    finally:
        engine.dispose()


def test_source_creation_resolves_existing_and_explicit_workspaces(tmp_path: Path) -> None:
    path = tmp_path / "resolver.sqlite3"
    upgrade_database(path)
    db = Database(path)
    try:
        with db.session() as session:
            personal = initial_personal_workspace(session)
            source_session = _new_session(session, "resolver-session", "Resolver")
            artifact = Artifact(
                id="resolver-artifact",
                workspace_id=personal.id,
                project_id=source_session.project_id,
                session_id=source_session.id,
                kind="srt",
                role="source",
                relative_path="resolver/source.srt",
            )
            session.add(artifact)
            session.flush()

            assert source_creation_workspace(session, artifact).id == personal.id
            assert source_creation_workspace(session, artifact).id == personal.id

            alternate = _new_workspace(session, "resolver-alternate")
            sessionless = Artifact(
                id="resolver-sessionless",
                workspace_id=personal.id,
                session_id=None,
                kind="srt",
                role="source",
                relative_path="resolver/sessionless.srt",
            )
            session.add(sessionless)
            session.flush()
            assert source_creation_workspace(session, sessionless).id == personal.id
            with pytest.raises(ValueError, match="not requested workspace"):
                source_creation_workspace(session, sessionless, workspace_id=alternate.id)

            alternate_artifact = Artifact(
                id="resolver-alternate-artifact",
                workspace_id=alternate.id,
                session_id=None,
                kind="srt",
                role="source",
                relative_path="resolver/alternate.srt",
            )
            session.add(alternate_artifact)
            session.flush()
            assert (
                source_creation_workspace(
                    session, alternate_artifact, workspace_id=alternate.id
                ).id
                == alternate.id
            )

            existing = _source_asset(
                "resolver-existing",
                artifact_id=alternate_artifact.id,
                workspace_id=alternate.id,
            )
            session.add(existing)
            session.flush()
            assert source_creation_workspace(session, alternate_artifact).id == alternate.id
    finally:
        db.dispose()


def test_source_creation_rejects_missing_inactive_and_conflicting_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "resolver-invalid.sqlite3"
    # Persisted missing roots are a legacy state. Current schema rejects them;
    # retain the helper's defensive legacy coverage against the real predecessor.
    command.upgrade(_config(path), "0074_actor_attribution")
    db = Database(path)
    try:
        with db.session() as session:
            personal = initial_personal_workspace(session)
            source_session = _new_session(session, "resolver-owner-session", "Owner")
            artifact = Artifact(
                id="resolver-conflict-artifact",
                workspace_id=personal.id,
                project_id=source_session.project_id,
                session_id=source_session.id,
                kind="srt",
                role="source",
                relative_path="resolver/conflict.srt",
            )
            session.add(artifact)
            session.flush()
            alternate = _new_workspace(session, "resolver-conflict-workspace")
            session.add(
                _source_asset(
                    "resolver-conflict-asset",
                    artifact_id=artifact.id,
                    workspace_id=alternate.id,
                )
            )
            session.flush()

            with pytest.raises(ValueError, match="not artifact workspace"):
                source_creation_workspace(session, artifact)
            with pytest.raises(ValueError, match="not artifact workspace"):
                source_creation_workspace(session, artifact, workspace_id=alternate.id)

            null_owner_artifact = Artifact(
                id="resolver-null-owner-artifact",
                workspace_id=personal.id,
                session_id=None,
                kind="srt",
                role="source",
                relative_path="resolver/null-owner.srt",
            )
            session.add(null_owner_artifact)
            session.flush()
            session.add(
                _source_asset(
                    "resolver-null-owner-asset",
                    artifact_id=null_owner_artifact.id,
                    workspace_id=None,
                )
            )
            session.flush()
            with pytest.raises(ValueError, match="has no workspace ownership"):
                source_creation_workspace(session, null_owner_artifact)

            inactive_artifact = Artifact(
                id="resolver-inactive-artifact",
                workspace_id=personal.id,
                session_id=None,
                kind="srt",
                role="source",
                relative_path="resolver/inactive.srt",
            )
            session.add(inactive_artifact)
            session.flush()
            alternate.state = "archived"
            session.flush()
            with pytest.raises(ValueError, match="inactive workspace"):
                source_creation_workspace(
                    session,
                    inactive_artifact,
                    workspace_id=alternate.id,
                )
            assert personal.state == "active"
    finally:
        db.dispose()


def test_source_attachment_requires_the_active_matching_workspace(tmp_path: Path) -> None:
    path = tmp_path / "attachment.sqlite3"
    # Construct the inconsistent legacy link on its genuine predecessor schema.
    command.upgrade(_config(path), "0076_project_graph_consistency")
    db = Database(path)
    try:
        with db.session() as session:
            personal = initial_personal_workspace(session)
            target = _new_session(session, "attachment-target", "Target")
            alternate = _new_workspace(session, "attachment-alternate")
            alternate_session = _new_session(
                session,
                "attachment-alternate-session",
                "Alternate",
                workspace_id=alternate.id,
            )
            external = _source_asset("attachment-external", workspace_id=personal.id)
            session.add(external)
            session.flush()
            validate_source_attachment(session, target.id, external)

            wrong_owner = _source_asset("attachment-wrong-owner", workspace_id=alternate.id)
            session.add(wrong_owner)
            session.flush()
            with pytest.raises(ValueError, match="not target workspace"):
                validate_source_attachment(session, target.id, wrong_owner)

            alternate_artifact = Artifact(
                id="attachment-alternate-artifact",
                workspace_id=alternate.id,
                project_id=alternate_session.project_id,
                session_id=alternate_session.id,
                kind="srt",
                role="source",
                relative_path="attachment/alternate.srt",
            )
            session.add(alternate_artifact)
            session.flush()
            inconsistent = _source_asset(
                "attachment-inconsistent-artifact",
                artifact_id=alternate_artifact.id,
                workspace_id=personal.id,
            )
            session.add(inconsistent)
            session.flush()
            with pytest.raises(ValueError, match="not artifact workspace"):
                validate_source_attachment(session, target.id, inconsistent)

            unassigned = _source_asset("attachment-unassigned")
            with pytest.raises(ValueError, match="has no workspace ownership"):
                validate_source_attachment(session, target.id, unassigned)
            with pytest.raises(KeyError):
                validate_source_attachment(session, "missing-session", external)

            target_project = session.get(Project, target.project_id)
            assert target_project is not None
            personal.state = "archived"
            session.flush()
            with pytest.raises(ValueError, match="inactive workspace"):
                validate_source_attachment(session, target.id, external)
    finally:
        db.dispose()


def test_0059_migration_adopts_workspace_graph_and_duplicate_artifacts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "genuine-0059.sqlite3"
    expected = _make_0059_fixture(path)
    _strip_source_workspace(path)
    with sqlite3.connect(path) as connection:
        assert "workspace_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(source_assets)")
        }
    before = _data_snapshot(path)

    command.upgrade(_config(path), "0060_source_workspace")

    assert _data_snapshot(path) == before
    assert _version(path) == "0060_source_workspace"
    owners = _source_workspace_map(path)
    assert owners == {
        "asset-linked": expected["archived_workspace"],
        "asset-detached-duplicate": expected["archived_workspace"],
        "asset-artifact-session": expected["personal_workspace"],
        "asset-legacy": expected["archived_workspace"],
        "asset-external-attached": expected["archived_workspace"],
        "asset-sessionless": expected["personal_workspace"],
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT session_id FROM artifacts WHERE id = 'artifact-personal'"
        ).fetchone() == (expected["personal_session"],)

    engine = database_module.create_database_engine(path)
    try:
        inspector = inspect(engine)
        indexes = {index["name"]: index for index in inspector.get_indexes("source_assets")}
        foreign_keys = inspector.get_foreign_keys("source_assets")
        workspace_fk = next(key for key in foreign_keys if key["name"] == WORKSPACE_FK)
        assert indexes[WORKSPACE_INDEX]["column_names"] == ["workspace_id"]
        assert workspace_fk["referred_table"] == "workspaces"
        assert workspace_fk["referred_columns"] == ["id"]
        assert (workspace_fk.get("options") or {}).get("ondelete", "").upper() == "RESTRICT"
    finally:
        engine.dispose()

    counts = {table: len(rows) for table, rows in _data_snapshot(path).items()}
    command.upgrade(_config(path), "0060_source_workspace")
    assert _source_workspace_map(path) == owners
    assert {table: len(rows) for table, rows in _data_snapshot(path).items()} == counts


def test_duplicate_artifact_workspace_conflict_fails_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "conflicting-0059.sqlite3"
    _make_0059_fixture(path, conflicting_duplicate=True)
    _strip_source_workspace(path)
    before_schema = _schema_snapshot(path)
    before_rows = _data_snapshot(path)

    with pytest.raises(RuntimeError, match="conflicting workspaces"):
        command.upgrade(_config(path), "0060_source_workspace")

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_rows
    assert _version(path) == "0059_project_ownership"


def test_unassigned_linked_session_fails_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "unassigned-session-0059.sqlite3"
    expected = _make_0059_fixture(path)
    _strip_source_workspace(path)
    _drop_personal_project_assignment(path, expected["personal_session"])
    before_schema = _schema_snapshot(path)
    before_rows = _data_snapshot(path)

    with pytest.raises(RuntimeError, match="without a valid project/workspace"):
        command.upgrade(_config(path), "0060_source_workspace")

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_rows
    assert _version(path) == "0059_project_ownership"


def test_prepopulated_source_ownership_is_rejected_without_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prepopulated-0059.sqlite3"
    expected = _make_0059_fixture(path, prepopulated_owner=True)
    before_schema = _schema_snapshot(path)
    before_rows = _data_snapshot(path)

    with pytest.raises(RuntimeError, match="already contains data"):
        command.upgrade(_config(path), "0060_source_workspace")

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_rows
    assert _version(path) == "0059_project_ownership"
    assert _source_workspace_map(path)["asset-linked"] == expected["personal_workspace"]


def test_late_failure_rolls_back_schema_rows_version_and_allows_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "late-failure-0059.sqlite3"
    _make_0059_fixture(path)
    _strip_source_workspace(path)
    before_schema = _schema_snapshot(path)
    before_rows = _data_snapshot(path)
    update_count = 0

    def fail_after_first_adoption(
        _connection, _cursor, statement: str, _parameters, _context, _executemany
    ) -> None:
        nonlocal update_count
        if statement.startswith("UPDATE source_assets SET workspace_id"):
            update_count += 1
            if update_count == 2:
                raise RuntimeError("Injected source adoption failure")

    event.listen(Engine, "before_cursor_execute", fail_after_first_adoption)
    try:
        with pytest.raises(RuntimeError, match="Injected source adoption failure"):
            command.upgrade(_config(path), "0060_source_workspace")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_first_adoption)

    assert update_count == 2
    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_rows
    assert _version(path) == "0059_project_ownership"

    command.upgrade(_config(path), "0060_source_workspace")
    assert _version(path) == "0060_source_workspace"
    assert all(owner is not None for owner in _source_workspace_map(path).values())


def test_downgrade_removes_only_source_workspace_metadata(tmp_path: Path) -> None:
    path = tmp_path / "downgrade-0059.sqlite3"
    _make_0059_fixture(path)
    _strip_source_workspace(path)
    before = _data_snapshot(path)

    command.upgrade(_config(path), "0060_source_workspace")
    command.downgrade(_config(path), "0059_project_ownership")

    assert _data_snapshot(path) == before
    assert _version(path) == "0059_project_ownership"
    engine = database_module.create_database_engine(path)
    try:
        inspector = inspect(engine)
        assert "workspace_id" not in {
            column["name"] for column in inspector.get_columns("source_assets")
        }
        assert WORKSPACE_INDEX not in {
            index["name"] for index in inspector.get_indexes("source_assets")
        }
        assert all(
            key["name"] != WORKSPACE_FK for key in inspector.get_foreign_keys("source_assets")
        )
    finally:
        engine.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
