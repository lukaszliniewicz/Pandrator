from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pandrator.web import database as database_module
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, sqlite_url, upgrade_database
from pandrator.web.models import Artifact, ArtifactEdge, SessionRecord, SourceAsset, SourceRecord
from pandrator.web.projects.artifact_identity import (
    ArtifactOwnership,
    resolve_artifact_ownership,
)
from pandrator.web.projects.identity import (
    create_session_project,
    initial_personal_workspace,
)
from pandrator.web.projects.models import Project

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
FIXED_TIME = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
WORKSPACE_INDEX = "ix_artifacts_workspace_id"
PROJECT_INDEX = "ix_artifacts_project_id"
WORKSPACE_FK = "fk_artifacts_workspace_id_workspaces"
PROJECT_FK = "fk_artifacts_project_id_projects"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _upgrade_to_0060(path: Path, *, strip_artifact_scope: bool) -> None:
    config = _config(path)
    command.upgrade(config, "0060_source_workspace")
    if strip_artifact_scope:
        _strip_artifact_scope(path)


def _strip_artifact_scope(path: Path) -> None:
    """Create the real 0060 artifacts shape despite current metadata in 0001."""
    engine = database_module.create_database_engine(path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        operations = Operations(
            MigrationContext.configure(connection, opts={"render_as_batch": True})
        )
        with connection.begin():
            with operations.batch_alter_table("artifacts") as batch:
                batch.drop_index(PROJECT_INDEX)
                batch.drop_index(WORKSPACE_INDEX)
                batch.drop_constraint(PROJECT_FK, type_="foreignkey")
                batch.drop_constraint(WORKSPACE_FK, type_="foreignkey")
                batch.drop_column("project_id")
                batch.drop_column("workspace_id")
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        assert violations == []
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()


def _new_session(
    session: Session,
    session_id: str,
    name: str,
    *,
    project_id: str | None = None,
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        name=name,
        storage_key=f"storage-{session_id}",
        workflow_kind="subtitles",
        source_language="en",
    )
    create_session_project(session, record, project_id=project_id)
    return record


def _new_archived_session(session: Session, *, workspace_id: str) -> SessionRecord:
    project = Project(
        id="project-archived",
        workspace_id=workspace_id,
        source_session_id=None,
        name="Archived root",
        created_by_user_id=None,
    )
    session.add(project)
    session.flush([project])
    record = SessionRecord(
        id="session-archived",
        name="Archived root",
        storage_key="storage-session-archived",
        workflow_kind="subtitles",
        source_language="en",
        project_id=project.id,
    )
    session.add(record)
    session.flush([record])
    project.source_session_id = record.id
    session.flush([project])
    return record


def _insert_historical_artifact(
    path: Path,
    artifact_id: str,
    *,
    session_id: str | None = None,
    state: str = "current",
    metadata_json: dict[str, object] | None = None,
) -> None:
    engine = database_module.create_database_engine(path)
    try:
        metadata = MetaData()
        with engine.begin() as connection:
            artifacts = Table("artifacts", metadata, autoload_with=connection)
            values = {
                "id": artifact_id,
                "session_id": session_id,
                "kind": "text",
                "role": "source",
                "relative_path": f"historical/{artifact_id}.txt",
                "mime_type": "text/plain",
                "size_bytes": 12,
                "content_hash": f"hash-{artifact_id}",
                "state": state,
                "settings_hash": None,
                "metadata_json": metadata_json or {},
                "created_at": FIXED_TIME,
                "updated_at": FIXED_TIME,
            }
            if "workspace_id" in artifacts.c:
                values["workspace_id"] = None
            if "project_id" in artifacts.c:
                values["project_id"] = None
            connection.execute(artifacts.insert().values(**values))
    finally:
        engine.dispose()


def _add_source_asset(
    session: Session,
    asset_id: str,
    *,
    artifact_id: str,
    workspace_id: str | None,
    state: str = "current",
) -> None:
    session.add(
        SourceAsset(
            id=asset_id,
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            display_name=asset_id,
            kind="text",
            mime_type="text/plain",
            external_path=None,
            size_bytes=12,
            content_hash=f"hash-{asset_id}",
            state=state,
            metadata_json={},
            revision=1,
            created_at=FIXED_TIME,
            updated_at=FIXED_TIME,
        )
    )


def _seed_0060(
    path: Path,
    *,
    strip_artifact_scope: bool = True,
    issue: str | None = None,
) -> tuple[str, str, str]:
    _upgrade_to_0060(path, strip_artifact_scope=strip_artifact_scope)
    database = Database(path)
    try:
        with database.session() as session:
            personal = initial_personal_workspace(session)
            active_session = _new_session(session, "session-active", "Active root")
            archived_workspace = Workspace(
                id="workspace-archived",
                kind="team",
                name="Archived workspace",
                state="archived",
            )
            session.add(archived_workspace)
            session.flush([archived_workspace])
            archived_session = _new_archived_session(session, workspace_id=archived_workspace.id)
            active_project_id = active_session.project_id
            assert active_project_id is not None
            personal_workspace_id = personal.id
            archived_workspace_id = archived_workspace.id

        _insert_historical_artifact(path, "artifact-active", session_id=active_session.id)
        _insert_historical_artifact(
            path,
            "artifact-archived",
            session_id=archived_session.id,
            state="stale",
        )
        _insert_historical_artifact(path, "artifact-library")
        _insert_historical_artifact(path, "artifact-unbound", state="deleted")
        _insert_historical_artifact(
            path,
            "artifact-edge-only",
            metadata_json={"project_id": "translation-project-metadata"},
        )

        with database.session() as session:
            _add_source_asset(
                session,
                "asset-active",
                artifact_id="artifact-active",
                workspace_id=personal_workspace_id,
            )
            _add_source_asset(
                session,
                "asset-archived",
                artifact_id="artifact-archived",
                workspace_id=archived_workspace_id,
            )
            _add_source_asset(
                session,
                "asset-library",
                artifact_id="artifact-library",
                workspace_id=archived_workspace_id,
                state="trashed",
            )
            if issue == "session-conflict":
                _add_source_asset(
                    session,
                    "asset-conflicting-session",
                    artifact_id="artifact-active",
                    workspace_id=archived_workspace_id,
                )
            elif issue == "source-conflict":
                _add_source_asset(
                    session,
                    "asset-conflicting-library",
                    artifact_id="artifact-library",
                    workspace_id=personal_workspace_id,
                )
            elif issue == "missing-source-workspace":
                _add_source_asset(
                    session,
                    "asset-missing-workspace",
                    artifact_id="artifact-library",
                    workspace_id=None,
                )

            session.add(
                ArtifactEdge(
                    parent_artifact_id="artifact-archived",
                    child_artifact_id="artifact-edge-only",
                    relation="derived_from",
                )
            )
            session.add(
                ArtifactEdge(
                    parent_artifact_id="artifact-active",
                    child_artifact_id="artifact-edge-only",
                    relation="derived_from",
                )
            )
            session.add(
                SourceRecord(
                    id="source-record-active",
                    session_id=active_session.id,
                    kind="text",
                    display_name="Active source",
                    artifact_id="artifact-active",
                    external_path=None,
                    content_hash="hash-artifact-active",
                    metadata_json={},
                    created_at=FIXED_TIME,
                )
            )

        if issue == "missing-session-project":
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE sessions SET project_id = NULL WHERE id = ?",
                    (active_session.id,),
                )
                connection.commit()
        elif issue == "stale-source-workspace":
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE source_assets SET workspace_id = ? WHERE id = ?",
                    ("missing-workspace", "asset-library"),
                )
                connection.commit()

        return personal_workspace_id, active_project_id, archived_workspace_id
    finally:
        database.dispose()


def _schema_snapshot(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def _data_snapshot(
    path: Path, *, exclude_artifact_scope: bool = False, exclude_version: bool = False
) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        snapshot: dict[str, list[tuple[object, ...]]] = {}
        for table in tables:
            if exclude_version and table == "alembic_version":
                continue
            columns = [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if not (
                    exclude_artifact_scope
                    and table == "artifacts"
                    and row[1] in {"workspace_id", "project_id"}
                )
            ]
            if columns:
                selected = ", ".join(f'"{column}"' for column in columns)
                snapshot[table] = connection.execute(
                    f'SELECT {selected} FROM "{table}" ORDER BY rowid'
                ).fetchall()
        return snapshot


def _ownership_rows(path: Path) -> dict[str, tuple[str | None, str | None]]:
    engine = database_module.create_database_engine(path)
    try:
        with engine.connect() as connection:
            artifacts = Table("artifacts", MetaData(), autoload_with=connection)
            rows = connection.execute(
                select(artifacts.c.id, artifacts.c.workspace_id, artifacts.c.project_id).order_by(
                    artifacts.c.id
                )
            )
            return {
                artifact_id: (workspace_id, project_id)
                for artifact_id, workspace_id, project_id in rows
            }
    finally:
        engine.dispose()


def test_artifact_ownership_resolves_session_project_and_personal_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "resolver.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        with database.session() as session:
            personal = initial_personal_workspace(session)
            root = _new_session(session, "resolver-root", "Resolver root")
            assert root.project_id is not None
            branch = _new_session(
                session,
                "resolver-branch",
                "Resolver branch",
                project_id=root.project_id,
            )
            other_workspace = Workspace(
                id="resolver-team-workspace",
                kind="team",
                name="Resolver team",
                state="active",
            )
            session.add(other_workspace)
            session.flush([other_workspace])

            by_session = resolve_artifact_ownership(session, session_id=root.id)
            assert by_session == ArtifactOwnership(personal.id, root.project_id)
            assert resolve_artifact_ownership(session, project_id=root.project_id) == by_session
            assert resolve_artifact_ownership(session) == ArtifactOwnership(personal.id, None)
            assert resolve_artifact_ownership(
                session, workspace_id=other_workspace.id
            ) == ArtifactOwnership(other_workspace.id, None)

            with pytest.raises(ValueError, match="does not exist"):
                resolve_artifact_ownership(
                    session,
                    session_id=root.id,
                    project_id="missing-project",
                )
            with pytest.raises(ValueError, match="conflicts"):
                resolve_artifact_ownership(
                    session,
                    session_id=root.id,
                    workspace_id=other_workspace.id,
                )

            pending = Artifact(
                id="pending-artifact",
                session_id=None,
                kind="text",
                role="artifact",
                relative_path="pending/artifact.txt",
            )
            session.add(pending)
            flushes: list[bool] = []

            def before_flush(*_args: object) -> None:
                flushes.append(True)

            event.listen(session, "before_flush", before_flush)
            try:
                resolve_artifact_ownership(session, session_id=branch.id)
            finally:
                event.remove(session, "before_flush", before_flush)
            assert flushes == []
            pending.workspace_id = personal.id

            with pytest.raises(AttributeError):
                by_session.workspace_id = "changed"  # type: ignore[misc]
    finally:
        database.dispose()


def test_existing_artifact_scope_checks_assets_and_same_project_sessions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stored-resolver.sqlite3"
    # The helper must still reject retained data predating media constraints.
    command.upgrade(_config(path), "0076_project_graph_consistency")
    database = Database(path)
    try:
        with database.session() as session:
            personal = initial_personal_workspace(session)
            root = _new_session(session, "stored-root", "Stored root")
            assert root.project_id is not None
            branch = _new_session(
                session,
                "stored-branch",
                "Stored branch",
                project_id=root.project_id,
            )
            other = _new_session(session, "stored-other", "Other root")
            assert other.project_id is not None
            artifact = Artifact(
                id="stored-artifact",
                workspace_id=personal.id,
                project_id=root.project_id,
                session_id=root.id,
                kind="text",
                role="source",
                relative_path="stored/source.txt",
            )
            session.add(artifact)
            session.flush([artifact])
            asset = SourceAsset(
                id="stored-asset",
                workspace_id=personal.id,
                artifact_id=artifact.id,
                display_name="Stored source",
                kind="text",
                state="current",
                metadata_json={},
                revision=1,
            )
            session.add(asset)
            session.flush([asset])

            assert resolve_artifact_ownership(session, artifact=artifact) == ArtifactOwnership(
                personal.id, root.project_id
            )
            assert resolve_artifact_ownership(
                session, artifact=artifact, session_id=branch.id
            ) == ArtifactOwnership(personal.id, root.project_id)
            with pytest.raises(ValueError, match="requested project"):
                resolve_artifact_ownership(session, artifact=artifact, project_id=other.project_id)

            archived_workspace = Workspace(
                id="stored-archived-workspace",
                kind="team",
                name="Stored archived workspace",
                state="archived",
            )
            session.add(archived_workspace)
            session.flush([archived_workspace])
            asset.workspace_id = archived_workspace.id
            session.flush([asset])
            with pytest.raises(ValueError, match="inactive workspace"):
                resolve_artifact_ownership(session, artifact=artifact)

            asset.workspace_id = personal.id
            artifact.workspace_id = None
            with pytest.raises(ValueError, match="no stored workspace"):
                resolve_artifact_ownership(session, artifact=artifact)

            artifact.workspace_id = "missing-workspace"
            with pytest.raises(ValueError, match="missing workspace"):
                resolve_artifact_ownership(session, artifact=artifact)
            artifact.workspace_id = personal.id
            session.flush([artifact])
    finally:
        database.dispose()


def test_genuine_0060_migration_adopts_scope_and_preserves_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "historical-0060.sqlite3"
    personal_workspace_id, active_project_id, archived_workspace_id = _seed_0060(path)
    with sqlite3.connect(path) as connection:
        artifact_columns = {row[1] for row in connection.execute('PRAGMA table_info("artifacts")')}
        assert "workspace_id" not in artifact_columns
        assert "project_id" not in artifact_columns
        before = _data_snapshot(path)

    command.upgrade(_config(path), "0061_artifact_ownership")
    ownership = _ownership_rows(path)
    assert ownership["artifact-active"] == (personal_workspace_id, active_project_id)
    assert ownership["artifact-archived"] == (
        archived_workspace_id,
        "project-archived",
    )
    assert ownership["artifact-library"] == (archived_workspace_id, None)
    assert ownership["artifact-unbound"] == (personal_workspace_id, None)
    assert ownership["artifact-edge-only"] == (personal_workspace_id, None)
    assert _data_snapshot(path, exclude_artifact_scope=True, exclude_version=True) == {
        table: rows for table, rows in before.items() if table != "alembic_version"
    }

    engine = database_module.create_database_engine(path)
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("artifacts")}
        indexes = {index["name"]: index for index in inspector.get_indexes("artifacts")}
        foreign_keys = {key["name"]: key for key in inspector.get_foreign_keys("artifacts")}
        assert columns["workspace_id"]["nullable"] is True
        assert columns["project_id"]["nullable"] is True
        assert indexes[WORKSPACE_INDEX]["column_names"] == ["workspace_id"]
        assert indexes[PROJECT_INDEX]["column_names"] == ["project_id"]
        assert foreign_keys[WORKSPACE_FK]["referred_table"] == "workspaces"
        assert (foreign_keys[WORKSPACE_FK].get("options") or {}).get("ondelete") == "RESTRICT"
        assert foreign_keys[PROJECT_FK]["referred_table"] == "projects"
        assert (foreign_keys[PROJECT_FK].get("options") or {}).get("ondelete") == "SET NULL"
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT COUNT(*) FROM artifact_edges").scalar_one() == 2
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM sources WHERE id = 'source-record-active'"
                ).scalar_one()
                == 1
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()

    first_schema = _schema_snapshot(path)
    first_data = _data_snapshot(path)
    command.upgrade(_config(path), "0061_artifact_ownership")
    assert _schema_snapshot(path) == first_schema
    assert _data_snapshot(path) == first_data

    command.downgrade(_config(path), "0060_source_workspace")
    with sqlite3.connect(path) as connection:
        artifact_columns = {row[1] for row in connection.execute('PRAGMA table_info("artifacts")')}
        assert "workspace_id" not in artifact_columns
        assert "project_id" not in artifact_columns
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0060_source_workspace"
        )
    assert _data_snapshot(path, exclude_version=True) == {
        table: rows for table, rows in before.items() if table != "alembic_version"
    }


@pytest.mark.parametrize(
    "issue",
    [
        "session-conflict",
        "source-conflict",
        "missing-source-workspace",
        "missing-session-project",
        "stale-source-workspace",
    ],
)
def test_0060_migration_rejects_invalid_ownership_without_partial_schema(
    tmp_path: Path, issue: str
) -> None:
    path = tmp_path / f"invalid-{issue}.sqlite3"
    _seed_0060(path, issue=issue)
    before_schema = _schema_snapshot(path)
    before_data = _data_snapshot(path)

    with pytest.raises(RuntimeError):
        upgrade_database(path)

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_data
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0060_source_workspace"
        )


def test_0061_late_failure_rolls_back_exactly_and_allows_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollback-retry.sqlite3"
    personal_workspace_id, active_project_id, _ = _seed_0060(path)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER injected_late_failure BEFORE UPDATE OF version_num "
            "ON alembic_version BEGIN SELECT RAISE(ABORT, "
            "'injected late adoption failure'); END"
        )
        connection.commit()
    before_schema = _schema_snapshot(path)
    before_data = _data_snapshot(path)
    with pytest.raises(IntegrityError, match="injected late"):
        command.upgrade(_config(path), "head")

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_data
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0060_source_workspace"
        )

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER injected_late_failure")
        connection.commit()
    upgrade_database(path)
    ownership = _ownership_rows(path)
    assert ownership["artifact-active"] == (personal_workspace_id, active_project_id)


def test_0061_accepts_empty_early_metadata_and_rejects_prepopulated_scope(
    tmp_path: Path,
) -> None:
    accepted_path = tmp_path / "early-empty.sqlite3"
    _seed_0060(accepted_path, strip_artifact_scope=False)
    upgrade_database(accepted_path)
    assert _ownership_rows(accepted_path)["artifact-library"] == (
        "workspace-archived",
        None,
    )

    rejected_path = tmp_path / "early-prepopulated.sqlite3"
    personal_workspace_id, _, _ = _seed_0060(rejected_path, strip_artifact_scope=False)
    with sqlite3.connect(rejected_path) as connection:
        connection.execute(
            "UPDATE artifacts SET workspace_id = ? WHERE id = ?",
            (personal_workspace_id, "artifact-active"),
        )
        connection.commit()
    before_schema = _schema_snapshot(rejected_path)
    before_data = _data_snapshot(rejected_path)
    with pytest.raises(RuntimeError, match="preexisting scope"):
        upgrade_database(rejected_path)
    assert _schema_snapshot(rejected_path) == before_schema
    assert _data_snapshot(rejected_path) == before_data


def test_0061_rejects_malformed_early_metadata_shape(tmp_path: Path) -> None:
    path = tmp_path / "malformed-early.sqlite3"
    _upgrade_to_0060(path, strip_artifact_scope=False)
    engine = database_module.create_database_engine(path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        operations = Operations(
            MigrationContext.configure(connection, opts={"render_as_batch": True})
        )
        with connection.begin():
            with operations.batch_alter_table("artifacts") as batch:
                batch.drop_constraint(WORKSPACE_FK, type_="foreignkey")
        before_schema = _schema_snapshot(path)
        before_data = _data_snapshot(path)
        with pytest.raises(RuntimeError, match="exactly one ownership foreign key"):
            upgrade_database(path)
        assert _schema_snapshot(path) == before_schema
        assert _data_snapshot(path) == before_data
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()
