from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, event, inspect, select
from sqlalchemy.orm import Session as SQLAlchemySession

from pandrator.web import database
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.database import (
    SCHEMA_HEAD,
    Database,
    sqlite_url,
    upgrade_database,
)
from pandrator.web.model_base import Base, new_id, utcnow
from pandrator.web.models import (
    AppSetting,
    Artifact,
    Job,
    ProjectFolder,
    ProjectFolderMembership,
    SessionRecord,
    SessionSetting,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.projects.identity import (
    create_session_project,
    delete_empty_project,
    initial_personal_workspace,
    session_project,
    sync_project_name,
)
from pandrator.web.projects.models import Project, ProjectMembership

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _legacy_snapshot(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as connection:
        return {
            "sessions": connection.execute(
                "SELECT id, name, storage_key, revision, created_at, updated_at, "
                "trashed_at, purge_after FROM sessions ORDER BY id"
            ).fetchall(),
            "translations": connection.execute(
                "SELECT id, name, source_session_id, revision, created_at, updated_at "
                "FROM translation_projects ORDER BY id"
            ).fetchall(),
            "branches": connection.execute(
                "SELECT id, project_id, session_id, target_language, "
                "source_content_hash, created_at FROM translation_project_branches "
                "ORDER BY id"
            ).fetchall(),
            "settings": connection.execute(
                "SELECT session_id, section, value_json, revision, updated_at "
                "FROM session_settings ORDER BY session_id, section"
            ).fetchall(),
            "jobs": connection.execute(
                "SELECT id, kind, session_id, status, payload_json, created_at "
                "FROM jobs ORDER BY id"
            ).fetchall(),
            "artifacts": connection.execute(
                "SELECT id, session_id, relative_path, content_hash, state "
                "FROM artifacts ORDER BY id"
            ).fetchall(),
            "folders": connection.execute(
                "SELECT id, name, parent_id, revision, created_at, updated_at "
                "FROM project_folders ORDER BY id"
            ).fetchall(),
            "folder_memberships": connection.execute(
                "SELECT folder_id, project_session_id, position "
                "FROM project_folder_memberships ORDER BY folder_id, position"
            ).fetchall(),
        }


def _strip_ownership_schema(path: Path) -> None:
    """Turn a fresh 0058 database into its exact pre-0059 schema shape."""
    engine = database.create_database_engine(path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        context = MigrationContext.configure(connection, opts={"render_as_batch": True})
        operations = Operations(context)
        with connection.begin():
            for table_name, index_name in (
                ("sessions", "ix_sessions_project_id"),
                ("project_folders", "ix_project_folders_workspace_id"),
            ):
                indexes = {item["name"] for item in inspect(connection).get_indexes(table_name)}
                if index_name in indexes:
                    with operations.batch_alter_table(table_name) as batch:
                        batch.drop_index(index_name)
                        batch.drop_column(
                            "project_id" if table_name == "sessions" else "workspace_id"
                        )
            for table_name, column_name in (
                ("translation_projects", "project_root_id"),
                ("project_folder_memberships", "project_id"),
            ):
                with operations.batch_alter_table(table_name) as batch:
                    batch.drop_column(column_name)
            for table_name in (
                "project_memberships",
                "projects",
                "workspace_memberships",
                "workspaces",
                "users",
            ):
                if table_name in inspect(connection).get_table_names():
                    operations.drop_table(table_name)
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()


def _make_fixture(
    path: Path,
    *,
    branch_folder_membership: bool = False,
    overlapping_translation_source: bool = False,
) -> dict[str, str]:
    command.upgrade(_config(path), "0058_project_folders")
    database = Database(path)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    trashed_at = now + timedelta(days=1)
    purge_after = now + timedelta(days=31)
    with database.session() as session:
        source = SessionRecord(
            id="legacy-source",
            name="Source workflow name",
            storage_key="legacy-source-storage",
            workflow_kind="subtitles",
            source_language="en",
            revision=7,
            created_at=now,
            updated_at=now + timedelta(hours=1),
        )
        branch = SessionRecord(
            id="legacy-branch",
            name="German branch name",
            storage_key="legacy-branch-storage",
            workflow_kind="subtitles",
            source_language="en",
            target_language="de",
            revision=4,
            created_at=now,
            updated_at=now + timedelta(hours=2),
        )
        standalone = SessionRecord(
            id="legacy-standalone",
            name="Standalone workflow name",
            storage_key="legacy-standalone-storage",
            workflow_kind="audiobook",
            source_language="pl",
            revision=9,
            trashed_at=trashed_at,
            purge_after=purge_after,
            created_at=now,
            updated_at=now + timedelta(hours=3),
        )
        session.add_all([source, branch, standalone])
        session.flush()
        artifact = Artifact(
            id="legacy-checkpoint",
            session_id=source.id,
            kind="srt",
            role="correction",
            relative_path="sessions/legacy-source/checkpoint.srt",
            content_hash="a" * 64,
            state="current",
            created_at=now,
            updated_at=now,
        )
        translation = TranslationProject(
            id="legacy-translation",
            name="Existing translation title",
            source_session_id=source.id,
            checkpoint_artifact_id=artifact.id,
            source_content_hash=artifact.content_hash,
            source_language="en",
            revision=12,
            created_at=now,
            updated_at=now + timedelta(hours=4),
        )
        session.add_all(
            [
                artifact,
                translation,
                TranslationProjectBranch(
                    id="legacy-branch-row",
                    project_id=translation.id,
                    session_id=branch.id,
                    target_language="de",
                    source_checkpoint_artifact_id=artifact.id,
                    source_content_hash=artifact.content_hash,
                    created_at=now,
                ),
                SessionSetting(
                    session_id=standalone.id,
                    section="tts",
                    value_json={"voice": "preserved"},
                    revision=6,
                    updated_at=now + timedelta(hours=5),
                ),
                AppSetting(key="legacy.setting", value_json={"kept": True}),
                Job(
                    id="legacy-queued-job",
                    kind="test.queued",
                    session_id=standalone.id,
                    status="queued",
                    payload_json={"retained": "verbatim"},
                    created_at=now + timedelta(hours=6),
                    updated_at=now + timedelta(hours=6),
                ),
                ProjectFolder(
                    id="legacy-folder",
                    name="Folder order",
                    revision=3,
                    created_at=now,
                    updated_at=now + timedelta(hours=7),
                ),
                ProjectFolder(
                    id="legacy-child-folder",
                    name="Child folder",
                    parent_id="legacy-folder",
                    revision=2,
                    created_at=now,
                    updated_at=now + timedelta(hours=8),
                ),
            ]
        )
        session.flush()
        memberships = [
            ProjectFolderMembership(
                folder_id="legacy-folder",
                project_session_id=(branch.id if branch_folder_membership else source.id),
                position=4,
            ),
            ProjectFolderMembership(
                folder_id="legacy-folder",
                project_session_id=standalone.id,
                position=9,
            ),
        ]
        session.add_all(memberships)
        if overlapping_translation_source:
            session.add(
                TranslationProject(
                    id="legacy-overlap-translation",
                    name="Invalid overlapping source",
                    source_session_id=branch.id,
                    checkpoint_artifact_id=artifact.id,
                    source_content_hash=artifact.content_hash,
                    source_language="de",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
    database.dispose()
    _strip_ownership_schema(path)
    return {
        "source": "legacy-source",
        "branch": "legacy-branch",
        "standalone": "legacy-standalone",
        "translation": "legacy-translation",
        "checkpoint": "legacy-checkpoint",
        "workspace_folder": "legacy-folder",
    }


def _ownership_columns(path: Path) -> dict[str, set[str]]:
    with sqlite3.connect(path) as connection:
        return {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in (
                "sessions",
                "translation_projects",
                "project_folders",
                "project_folder_memberships",
            )
        }


def test_models_register_leaf_tables_and_preserve_base_aliases() -> None:
    from pandrator.web import models

    assert models.Base is Base
    assert models.new_id is new_id
    assert models.utcnow is utcnow
    assert models.User is User
    assert models.Workspace is Workspace
    assert models.WorkspaceMembership is WorkspaceMembership
    assert models.Project is Project
    assert models.ProjectMembership is ProjectMembership
    assert {
        "users",
        "workspaces",
        "workspace_memberships",
        "projects",
        "project_memberships",
    } <= set(Base.metadata.tables)
    assert "project_id" in SessionRecord.__table__.c
    assert "project_root_id" in TranslationProject.__table__.c


@pytest.mark.parametrize(
    "imports",
    [
        "from pandrator.web.projects.models import Project; "
        "from pandrator.web.accounts.models import User; "
        "from pandrator.web.models import Base",
        "from pandrator.web.accounts.models import User; "
        "from pandrator.web.projects.models import Project; "
        "from pandrator.web.models import Base",
        "from pandrator.web.models import Base, User, Project",
    ],
)
def test_leaf_model_import_order_is_cycle_free(imports: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_migration_adopts_legacy_groups_and_preserves_workflow_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy-0058.sqlite3"
    ids = _make_fixture(path)
    before = _legacy_snapshot(path)

    command.upgrade(_config(path), "0059_project_ownership")

    database = Database(path)
    try:
        with database.session() as session:
            owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
            assert owner is not None
            assert owner.display_name == "Owner"
            assert owner.is_active is True
            assert owner.is_instance_admin is True
            workspace = initial_personal_workspace(session)
            assert workspace.name == "Personal"
            assert workspace.state == "active"
            membership = session.get(WorkspaceMembership, (workspace.id, owner.id))
            assert membership is not None and membership.role == "owner"

            source = session.get(SessionRecord, ids["source"])
            branch = session.get(SessionRecord, ids["branch"])
            standalone = session.get(SessionRecord, ids["standalone"])
            assert source is not None and branch is not None and standalone is not None
            source_project = session_project(session, source.id)
            assert branch.project_id == source_project.id
            assert source_project.source_session_id == source.id
            assert source_project.name == "Existing translation title"
            assert source_project.created_by_user_id is None
            assert standalone.project_id is not None
            standalone_project = session.get(Project, standalone.project_id)
            assert standalone_project is not None
            assert standalone_project.source_session_id == standalone.id
            assert standalone_project.name == standalone.name
            assert standalone_project.created_by_user_id is None

            translation = session.get(TranslationProject, ids["translation"])
            assert translation is not None
            assert translation.project_root_id == source_project.id
            folder = session.get(ProjectFolder, ids["workspace_folder"])
            assert folder is not None and folder.workspace_id == workspace.id
            folder_rows = session.scalars(
                select(ProjectFolderMembership)
                .where(ProjectFolderMembership.folder_id == folder.id)
                .order_by(ProjectFolderMembership.position)
            ).all()
            assert [row.position for row in folder_rows] == [4, 9]
            assert [row.project_session_id for row in folder_rows] == [
                ids["source"],
                ids["standalone"],
            ]
            assert [row.project_id for row in folder_rows] == [
                source_project.id,
                standalone_project.id,
            ]

            setting = session.get(SessionSetting, (ids["standalone"], "tts"))
            job = session.get(Job, "legacy-queued-job")
            checkpoint = session.get(Artifact, ids["checkpoint"])
            app_setting = session.get(AppSetting, "legacy.setting")
            assert setting is not None and setting.value_json == {"voice": "preserved"}
            assert job is not None and job.status == "queued"
            assert checkpoint is not None and checkpoint.content_hash == "a" * 64
            assert app_setting is not None and app_setting.value_json == {"kept": True}
            assert standalone.trashed_at is not None and standalone.purge_after is not None

        with database.engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "0059_project_ownership"
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        database.dispose()

    assert _legacy_snapshot(path) == before
    with sqlite3.connect(path) as connection:
        project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        owner_count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE legacy_subject = 'owner'"
        ).fetchone()[0]
        workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    assert project_count == 2
    assert owner_count == workspace_count == 1

    command.upgrade(_config(path), "0059_project_ownership")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("fixture_options", "message"),
    [
        ({"overlapping_translation_source": True}, "both a source and a branch"),
        ({"branch_folder_membership": True}, "not canonical"),
    ],
)
def test_malformed_legacy_graph_rolls_back_before_adoption(
    tmp_path: Path, fixture_options: dict[str, bool], message: str
) -> None:
    path = tmp_path / "invalid-0058.sqlite3"
    _make_fixture(path, **fixture_options)
    before = _legacy_snapshot(path)
    before_columns = _ownership_columns(path)

    with pytest.raises(RuntimeError, match=message):
        command.upgrade(_config(path), "head")

    assert _legacy_snapshot(path) == before
    assert _ownership_columns(path) == before_columns
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0058_project_folders"
        )
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert (
            not {
                "users",
                "workspaces",
                "workspace_memberships",
                "projects",
                "project_memberships",
            }
            & tables
        )


def test_downgrade_removes_only_ownership_metadata(tmp_path: Path) -> None:
    path = tmp_path / "downgrade-copy.sqlite3"
    _make_fixture(path)
    before = _legacy_snapshot(path)
    command.upgrade(_config(path), "head")

    command.downgrade(_config(path), "0058_project_folders")

    assert _legacy_snapshot(path) == before
    ownership = _ownership_columns(path)
    assert "project_id" not in ownership["sessions"]
    assert "project_root_id" not in ownership["translation_projects"]
    assert "workspace_id" not in ownership["project_folders"]
    assert "project_id" not in ownership["project_folder_memberships"]
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0058_project_folders"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert (
            not {
                "users",
                "workspaces",
                "workspace_memberships",
                "projects",
                "project_memberships",
            }
            & tables
        )


@pytest.mark.parametrize(
    "failed_statement",
    ["INSERT INTO users", "UPDATE project_folder_memberships SET project_id"],
)
def test_late_migration_failure_rolls_back_schema_and_can_retry(
    tmp_path: Path, failed_statement: str
) -> None:
    path = tmp_path / "late-failure.sqlite3"
    _make_fixture(path)
    before_rows = _legacy_snapshot(path)
    with sqlite3.connect(path) as connection:
        before_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    def fail_during_upgrade(
        _connection, _cursor, statement: str, _parameters, _context, _executemany
    ) -> None:
        if statement.startswith(failed_statement):
            raise RuntimeError("Injected migration failure after ownership DDL")

    event.listen(Engine, "before_cursor_execute", fail_during_upgrade)
    try:
        with pytest.raises(RuntimeError, match="Injected migration failure"):
            command.upgrade(_config(path), "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_during_upgrade)

    assert _legacy_snapshot(path) == before_rows
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            == before_schema
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0058_project_folders",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.upgrade(_config(path), "head")
    assert _legacy_snapshot(path) == before_rows
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            SCHEMA_HEAD,
        )
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (2,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _session_record(session_id: str, name: str) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=name,
        storage_key=f"storage-{session_id}",
        workflow_kind="subtitles",
        source_language="en",
    )


def test_session_project_creation_explicit_branch_and_missing_seed(tmp_path: Path) -> None:
    path = tmp_path / "helpers.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        with database.session() as session:
            owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
            assert owner is not None
            workspace = initial_personal_workspace(session)
            root = create_session_project(session, _session_record("root-session", "Root"))
            assert root.workspace_id == workspace.id
            assert root.created_by_user_id == owner.id
            assert root.source_session_id == "root-session"
            branch = _session_record("branch-session", "Branch")
            branch_root = create_session_project(session, branch, project_id=root.id)
            assert branch_root.id == root.id
            assert branch.project_id == root.id
            assert root.source_session_id == "root-session"
            assert root.name == "Root"

            legacy_root = create_session_project(
                session, _session_record("legacy-session", "Imported"), legacy=True
            )
            assert legacy_root.created_by_user_id is None

            with pytest.raises(ValueError, match="does not exist"):
                create_session_project(
                    session,
                    _session_record("missing-root-session", "Missing"),
                    project_id="missing-project",
                )
            conflicting = _session_record("conflicting-session", "Conflict")
            conflicting.project_id = "other-project"
            with pytest.raises(ValueError, match="already assigned"):
                create_session_project(session, conflicting, project_id=root.id)

            with pytest.raises(KeyError, match="missing-session"):
                session_project(session, "missing-session")
    finally:
        database.dispose()

    empty_path = tmp_path / "missing-seed.sqlite3"
    from sqlalchemy import create_engine

    engine = create_engine(sqlite_url(empty_path))
    try:
        Base.metadata.create_all(engine)
        with SQLAlchemySession(bind=engine) as session:
            with pytest.raises(RuntimeError, match="owner user is missing"):
                initial_personal_workspace(session)
            assert session.scalar(select(User)) is None
    finally:
        engine.dispose()


def test_project_name_precedence_revision_and_empty_root_safety(tmp_path: Path) -> None:
    path = tmp_path / "helper-names.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        with database.session() as session:
            source = _session_record("name-source", "Original source name")
            source.legacy_name = "legacy alias remains"
            root = create_session_project(session, source)
            branch = _session_record("name-branch", "Independent branch name")
            create_session_project(session, branch, project_id=root.id)
            artifact = Artifact(
                id="name-checkpoint",
                workspace_id=root.workspace_id,
                project_id=root.id,
                session_id=source.id,
                kind="srt",
                role="correction",
                relative_path="sessions/name-source/checkpoint.srt",
                content_hash="b" * 64,
            )
            session.add(artifact)
            session.flush()
            translation = TranslationProject(
                id="name-translation",
                name="Translation title",
                source_session_id=source.id,
                project_root_id=root.id,
                checkpoint_artifact_id=artifact.id,
                source_content_hash=artifact.content_hash,
                source_language="en",
            )
            session.add(translation)
            session.flush()

            updated_at = root.updated_at
            sync_project_name(session, source.id)
            assert root.name == "Translation title"
            assert root.revision == 2
            assert root.updated_at != updated_at
            changed_at = root.updated_at

            source.name = "Renamed source"
            sync_project_name(session, source.id)
            assert root.name == "Translation title"
            assert source.legacy_name == "legacy alias remains"
            assert root.revision == 2
            assert root.updated_at == changed_at

            translation.name = "Renamed translation title"
            sync_project_name(session, source.id)
            assert root.name == "Renamed translation title"
            assert root.revision == 3
            changed_at = root.updated_at
            sync_project_name(session, source.id)
            assert root.revision == 3
            assert root.updated_at == changed_at

            branch.name = "Branch rename does not rename root"
            sync_project_name(session, branch.id)
            assert root.name == "Renamed translation title"

            delete_empty_project(session, root.id)
            assert session.get(Project, root.id) is not None
            session.delete(translation)
            session.flush()
            session.delete(source)
            delete_empty_project(session, root.id)
            assert session.get(Project, root.id) is not None
            session.delete(branch)
            delete_empty_project(session, root.id)
            assert session.get(Project, root.id) is None
            delete_empty_project(session, None)
            delete_empty_project(session, "missing-project")
    finally:
        database.dispose()
