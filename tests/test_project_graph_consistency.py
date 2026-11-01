from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TypeAlias

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import IntegrityError

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.database import Database, create_database_engine, sqlite_url
from pandrator.web.models import (
    Artifact,
    Job,
    ProjectFolder,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
    UsageEvent,
    Workspace,
)
from pandrator.web.project_folders import (
    create_project_folder,
    delete_project_folder,
    replace_folder_projects,
    update_project_folder,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import Project

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
TARGET_REVISION = "0076_project_graph_consistency"
PREDECESSOR_REVISION = "0075_mandatory_ownership"
TRIGGER_PREFIX = "pandrator_project_graph_"
TRIGGER_NAMES = (
    "pandrator_project_graph_projects_insert",
    "pandrator_project_graph_projects_update",
    "pandrator_project_graph_sessions_update",
    "pandrator_project_graph_translation_projects_insert",
    "pandrator_project_graph_translation_projects_update",
    "pandrator_project_graph_translation_project_branches_insert",
    "pandrator_project_graph_translation_project_branches_update",
    "pandrator_project_graph_project_folders_insert",
    "pandrator_project_graph_project_folders_update",
    "pandrator_project_graph_project_folder_memberships_insert",
    "pandrator_project_graph_project_folder_memberships_update",
)
_SqlStatement: TypeAlias = tuple[str, dict[str, object]]
_SqlStatements: TypeAlias = tuple[_SqlStatement, ...]
_InvalidPredecessor: TypeAlias = tuple[str, _SqlStatements, _SqlStatements, str]

IDS = {
    "source": "10000000-0000-4000-8000-000000000001",
    "branch": "10000000-0000-4000-8000-000000000002",
    "branch_two": "10000000-0000-4000-8000-000000000003",
    "standalone": "10000000-0000-4000-8000-000000000004",
    "foreign_source": "10000000-0000-4000-8000-000000000005",
    "foreign_branch": "10000000-0000-4000-8000-000000000006",
    "workspace": "20000000-0000-4000-8000-000000000001",
    "artifact": "30000000-0000-4000-8000-000000000001",
    "foreign_artifact": "30000000-0000-4000-8000-000000000002",
    "translation": "40000000-0000-4000-8000-000000000001",
    "foreign_translation": "40000000-0000-4000-8000-000000000002",
    "branch_link": "50000000-0000-4000-8000-000000000001",
    "folder_root": "60000000-0000-4000-8000-000000000001",
    "folder_child": "60000000-0000-4000-8000-000000000002",
    "folder_sibling": "60000000-0000-4000-8000-000000000003",
    "foreign_folder": "60000000-0000-4000-8000-000000000004",
    "retained_job": "70000000-0000-4000-8000-000000000001",
    "standalone_job": "70000000-0000-4000-8000-000000000002",
    "retained_usage": "80000000-0000-4000-8000-000000000001",
    "standalone_usage": "80000000-0000-4000-8000-000000000002",
}


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _upgrade_to_0075(path: Path) -> Config:
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    return config


def _make_valid_predecessor(path: Path) -> dict[str, str]:
    _upgrade_to_0075(path)
    database = Database(path)
    try:
        with database.session() as session:
            workspace = initial_personal_workspace(session)
            source = SessionRecord(id=IDS["source"], name="Source", source_language="en")
            root = create_session_project(session, source)
            branch = SessionRecord(
                id=IDS["branch"],
                name="German branch",
                source_language="en",
                target_language="de",
            )
            create_session_project(session, branch, project_id=root.id)
            branch_two = SessionRecord(
                id=IDS["branch_two"],
                name="French branch",
                source_language="en",
                target_language="fr",
            )
            create_session_project(session, branch_two, project_id=root.id)

            standalone = SessionRecord(
                id=IDS["standalone"],
                name="Standalone source",
                source_language="pl",
            )
            standalone_root = create_session_project(session, standalone)

            foreign_workspace = Workspace(id=IDS["workspace"], kind="team", name="Other workspace")
            session.add(foreign_workspace)
            session.flush()
            foreign_source = SessionRecord(
                id=IDS["foreign_source"], name="Other source", source_language="en"
            )
            foreign_root = create_session_project(
                session, foreign_source, workspace_id=foreign_workspace.id
            )
            foreign_branch = SessionRecord(
                id=IDS["foreign_branch"],
                name="Other branch",
                source_language="en",
                target_language="es",
            )
            create_session_project(session, foreign_branch, project_id=foreign_root.id)

            artifact = Artifact(
                id=IDS["artifact"],
                workspace_id=workspace.id,
                project_id=root.id,
                session_id=source.id,
                kind="txt",
                role="correction",
                relative_path="project-graph/source.txt",
                content_hash="a" * 64,
            )
            foreign_artifact = Artifact(
                id=IDS["foreign_artifact"],
                workspace_id=foreign_workspace.id,
                project_id=foreign_root.id,
                session_id=foreign_source.id,
                kind="txt",
                role="correction",
                relative_path="project-graph/other-source.txt",
                content_hash="b" * 64,
            )
            session.add_all([artifact, foreign_artifact])
            session.flush()

            translation = TranslationProject(
                id=IDS["translation"],
                project_root_id=root.id,
                name="Languages",
                source_session_id=source.id,
                checkpoint_artifact_id=artifact.id,
                source_content_hash=artifact.content_hash,
                source_language="en",
            )
            foreign_translation = TranslationProject(
                id=IDS["foreign_translation"],
                project_root_id=foreign_root.id,
                name="Other languages",
                source_session_id=foreign_source.id,
                checkpoint_artifact_id=foreign_artifact.id,
                source_content_hash=foreign_artifact.content_hash,
                source_language="en",
            )
            session.add_all([translation, foreign_translation])
            session.flush()
            session.add(
                TranslationProjectBranch(
                    id=IDS["branch_link"],
                    project_id=translation.id,
                    session_id=branch.id,
                    target_language="de",
                    source_checkpoint_artifact_id=artifact.id,
                    source_content_hash=artifact.content_hash,
                )
            )
            session.flush()

            folder_root = ProjectFolder(
                id=IDS["folder_root"],
                name="Folder root",
                workspace_id=workspace.id,
            )
            folder_child = ProjectFolder(
                id=IDS["folder_child"],
                name="Nested folder",
                workspace_id=workspace.id,
                parent_id=folder_root.id,
            )
            folder_sibling = ProjectFolder(
                id=IDS["folder_sibling"],
                name="Sibling folder",
                workspace_id=workspace.id,
            )
            foreign_folder = ProjectFolder(
                id=IDS["foreign_folder"],
                name="Other workspace folder",
                workspace_id=foreign_workspace.id,
            )
            session.add_all([folder_root, folder_child, folder_sibling, foreign_folder])
            session.flush()

            retained_root = Project(
                workspace_id=workspace.id,
                source_session_id=None,
                name="Retained receipt root",
                created_by_user_id=None,
            )
            session.add(retained_root)
            session.flush()
            session.add_all(
                [
                    Job(
                        id=IDS["retained_job"],
                        kind="fixture.retained",
                        workspace_id=workspace.id,
                        project_id=retained_root.id,
                        session_id=None,
                    ),
                    Job(
                        id=IDS["standalone_job"],
                        kind="fixture.standalone",
                        workspace_id=workspace.id,
                        project_id=standalone_root.id,
                        session_id=None,
                    ),
                    UsageEvent(
                        id=IDS["retained_usage"],
                        workspace_id=workspace.id,
                        project_id=retained_root.id,
                        session_id=None,
                        provider_key="fixture",
                        model_id="fixture-model",
                        raw_usage_json={"receipt": "retained"},
                    ),
                    UsageEvent(
                        id=IDS["standalone_usage"],
                        workspace_id=workspace.id,
                        project_id=standalone_root.id,
                        session_id=standalone.id,
                        provider_key="fixture",
                        model_id="fixture-model",
                        raw_usage_json={"receipt": "standalone"},
                    ),
                ]
            )
            replace_folder_projects(
                session,
                folder_root.id,
                expected_revision=1,
                project_ids=[source.id, branch.id, standalone.id],
            )

            ids = {
                "workspace": workspace.id,
                "foreign_workspace": foreign_workspace.id,
                "root": root.id,
                "source": source.id,
                "branch": branch.id,
                "branch_two": branch_two.id,
                "standalone_root": standalone_root.id,
                "standalone": standalone.id,
                "foreign_root": foreign_root.id,
                "foreign_source": foreign_source.id,
                "foreign_branch": foreign_branch.id,
                "artifact": artifact.id,
                "translation": translation.id,
                "foreign_translation": foreign_translation.id,
                "folder_root": folder_root.id,
                "folder_child": folder_child.id,
                "folder_sibling": folder_sibling.id,
                "foreign_folder": foreign_folder.id,
                "retained_root": retained_root.id,
            }
    finally:
        database.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fixture_unrelated_project_name_noop "
            "AFTER UPDATE OF name ON projects BEGIN SELECT NEW.id; END"
        )
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
        rows = {}
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            values = connection.execute(f"SELECT * FROM {quoted_name}").fetchall()
            rows[table_name] = tuple(sorted((tuple(row) for row in values), key=repr))
    return {"schema": schema, "rows": rows}


def _rows_without_alembic_version(snapshot: dict[str, object]) -> dict[str, object]:
    rows = snapshot["rows"]
    assert isinstance(rows, dict)
    return {name: value for name, value in rows.items() if name != "alembic_version"}


def _owned_triggers(path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    return tuple(
        (str(name), str(table_name), str(sql))
        for name, table_name, sql in rows
        if str(name).startswith(TRIGGER_PREFIX)
    )


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def _apply_sqlite(path: Path, statements: tuple[tuple[str, dict[str, object]], ...]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for statement, parameters in statements:
            connection.execute(statement, parameters)


def _reject_statement(
    connection: sa.Connection,
    statement: str,
    parameters: dict[str, object],
    message: str,
    check_statement: str,
    check_parameters: dict[str, object],
    expected_value: object,
) -> None:
    with pytest.raises(IntegrityError) as raised:
        connection.execute(text(statement), parameters)
    assert str(raised.value.orig) == message
    assert (
        connection.execute(text(check_statement), check_parameters).scalar_one() == expected_value
    )


def test_fresh_upgrade_creates_exact_triggers_and_repeated_head_is_stable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh.sqlite3"
    config = _config(path)

    command.upgrade(config, TARGET_REVISION)

    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _owned_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before_repeat = _snapshot(path)
    command.upgrade(config, TARGET_REVISION)
    assert _snapshot(path) == before_repeat


def test_populated_graph_upgrade_preserves_rows_and_normal_producers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "populated.sqlite3"
    config = _config(path)
    ids = _make_valid_predecessor(path)
    before = _snapshot(path)

    command.upgrade(config, TARGET_REVISION)

    after = _snapshot(path)
    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _owned_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )
    before_schema = before["schema"]
    after_schema = after["schema"]
    assert isinstance(before_schema, tuple) and isinstance(after_schema, tuple)
    assert (
        tuple(
            row
            for row in after_schema
            if not (row[0] == "trigger" and str(row[1]).startswith(TRIGGER_PREFIX))
        )
        == before_schema
    )
    assert _rows_without_alembic_version(after) == _rows_without_alembic_version(before)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    database = Database(path)
    try:
        with database.session() as session:
            new_source = SessionRecord(
                id="10000000-0000-4000-8000-000000000007",
                name="Post-migration root",
                source_language="it",
            )
            new_root = create_session_project(session, new_source)
            new_branch = SessionRecord(
                id="10000000-0000-4000-8000-000000000008",
                name="Post-migration branch",
                source_language="it",
                target_language="de",
            )
            create_session_project(session, new_branch, project_id=new_root.id)
            independent = SessionRecord(
                id="10000000-0000-4000-8000-000000000009",
                name="Independent fork",
                source_language="pl",
            )
            independent_root = create_session_project(session, independent)
            session.add(
                TranslationProjectBranch(
                    id="50000000-0000-4000-8000-000000000002",
                    project_id=ids["translation"],
                    session_id=ids["branch_two"],
                    target_language="fr",
                    source_checkpoint_artifact_id=ids["artifact"],
                    source_content_hash="a" * 64,
                )
            )
            assert new_branch.project_id == new_root.id
            assert independent.project_id == independent_root.id
            assert independent_root.id != new_root.id

            cascade_folder = create_project_folder(session, "Cascade check")["folder"]
            replace_folder_projects(
                session,
                cascade_folder["id"],
                expected_revision=1,
                project_ids=[ids["source"]],
            )

        with database.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM project_folders WHERE id = :id"),
                {"id": cascade_folder["id"]},
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM project_folder_memberships WHERE folder_id = :id"),
                    {"id": cascade_folder["id"]},
                ).scalar_one()
                == 0
            )

        with database.session() as session:
            changed_child = update_project_folder(
                session,
                ids["folder_child"],
                expected_revision=1,
                parent_id=ids["folder_sibling"],
                change_parent=True,
            )["folder"]
            assert changed_child["parent_id"] == ids["folder_sibling"]
            grandchild = create_project_folder(session, "Promoted folder", ids["folder_child"])[
                "folder"
            ]
            replace_folder_projects(
                session,
                ids["folder_child"],
                expected_revision=2,
                project_ids=[ids["source"]],
            )
            deleted = delete_project_folder(session, ids["folder_child"], expected_revision=3)
            promoted = next(item for item in deleted["items"] if item["id"] == grandchild["id"])
            assert promoted["parent_id"] == ids["folder_sibling"]
            assert all(item["folder_id"] != ids["folder_child"] for item in deleted["memberships"])

        with database.session() as session:
            session.delete(session.get(SessionRecord, ids["standalone"]))
        with database.session() as session:
            standalone_root = session.get(Project, ids["standalone_root"])
            assert standalone_root is not None
            assert standalone_root.source_session_id is None
            standalone_job = session.get(Job, IDS["standalone_job"])
            assert standalone_job is not None
            assert standalone_job.project_id == standalone_root.id
            usage = session.get(UsageEvent, IDS["standalone_usage"])
            assert usage is not None and usage.session_id is None
            assert usage.project_id == standalone_root.id
            retained_root = session.get(Project, ids["retained_root"])
            assert retained_root is not None and retained_root.source_session_id is None
            retained_job = session.get(Job, IDS["retained_job"])
            assert retained_job is not None
            assert retained_job.project_id == retained_root.id
            retained_usage = session.get(UsageEvent, IDS["retained_usage"])
            assert retained_usage is not None
            assert retained_usage.project_id == retained_root.id
            assert initial_personal_workspace(session).id == ids["workspace"]
    finally:
        database.dispose()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_raw_sql_guards_reject_invalid_writes_and_allow_valid_edits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw-writes.sqlite3"
    config = _upgrade_to_0075(path)
    ids = _make_valid_predecessor(path)
    command.upgrade(config, TARGET_REVISION)
    database = Database(path)
    missing = "90000000-0000-4000-8000-000000000001"
    inserted_project = "90000000-0000-4000-8000-000000000002"
    inserted_translation = "90000000-0000-4000-8000-000000000003"
    inserted_branch_wrong_root = "90000000-0000-4000-8000-000000000004"
    inserted_branch_source = "90000000-0000-4000-8000-000000000005"
    inserted_folder_cross = "90000000-0000-4000-8000-000000000006"
    inserted_folder_cycle = "90000000-0000-4000-8000-000000000007"
    try:
        with database.engine.begin() as connection:
            _reject_statement(
                connection,
                """INSERT INTO projects (
                       id, workspace_id, source_session_id, name,
                       created_by_user_id, revision, created_at, updated_at
                   )
                   SELECT :id, workspace_id, :source_id, name,
                          created_by_user_id, revision, created_at, updated_at
                   FROM projects WHERE id = :template""",
                {
                    "id": inserted_project,
                    "source_id": ids["foreign_branch"],
                    "template": ids["root"],
                },
                "project source must belong to project",
                "SELECT COUNT(*) FROM projects WHERE id = :id",
                {"id": inserted_project},
                0,
            )

            _reject_statement(
                connection,
                """INSERT INTO sessions (
                       id, project_id, name, storage_key, legacy_name, legacy_path,
                       workflow_kind, interface_mode, source_language, target_language,
                       workflow_preset, included_stages_json, status, revision,
                       created_at, updated_at, trashed_at, purge_after
                   )
                   SELECT :id, :project_id, name, :storage_key, legacy_name, legacy_path,
                          workflow_kind, interface_mode, source_language, target_language,
                          workflow_preset, included_stages_json, status, revision,
                          created_at, updated_at, trashed_at, purge_after
                   FROM sessions WHERE id = :template""",
                {
                    "id": "90000000-0000-4000-8000-000000000008",
                    "project_id": missing,
                    "storage_key": "90000000-0000-4000-8000-000000000009",
                    "template": ids["branch"],
                },
                "FOREIGN KEY constraint failed",
                "SELECT COUNT(*) FROM sessions WHERE id = :id",
                {"id": "90000000-0000-4000-8000-000000000008"},
                0,
            )

            _reject_statement(
                connection,
                "UPDATE projects SET workspace_id = :workspace WHERE id = :project",
                {"workspace": ids["foreign_workspace"], "project": ids["root"]},
                "project workspace is immutable",
                "SELECT workspace_id FROM projects WHERE id = :project",
                {"project": ids["root"]},
                ids["workspace"],
            )
            _reject_statement(
                connection,
                "UPDATE projects SET source_session_id = :source WHERE id = :project",
                {"source": ids["branch"], "project": ids["root"]},
                "project source is immutable after attachment",
                "SELECT source_session_id FROM projects WHERE id = :project",
                {"project": ids["root"]},
                ids["source"],
            )
            _reject_statement(
                connection,
                "UPDATE projects SET source_session_id = NULL WHERE id = :project",
                {"project": ids["root"]},
                "project source cannot be cleared while its session exists",
                "SELECT source_session_id FROM projects WHERE id = :project",
                {"project": ids["root"]},
                ids["source"],
            )
            connection.execute(
                text(
                    "UPDATE projects SET workspace_id = workspace_id, "
                    "source_session_id = source_session_id, name = :name WHERE id = :project"
                ),
                {"name": "Renamed source root", "project": ids["root"]},
            )
            assert (
                connection.execute(
                    text("SELECT name FROM projects WHERE id = :project"),
                    {"project": ids["root"]},
                ).scalar_one()
                == "Renamed source root"
            )

            _reject_statement(
                connection,
                "UPDATE sessions SET project_id = :project WHERE id = :session",
                {"project": ids["standalone_root"], "session": ids["branch"]},
                "session project is immutable",
                "SELECT project_id FROM sessions WHERE id = :session",
                {"session": ids["branch"]},
                ids["root"],
            )
            connection.execute(
                text(
                    "UPDATE sessions SET project_id = project_id, name = :name WHERE id = :session"
                ),
                {"name": "Renamed branch", "session": ids["branch"]},
            )
            assert (
                connection.execute(
                    text("SELECT name FROM sessions WHERE id = :session"),
                    {"session": ids["branch"]},
                ).scalar_one()
                == "Renamed branch"
            )

            _reject_statement(
                connection,
                """INSERT INTO translation_projects (
                       id, project_root_id, name, source_session_id,
                       checkpoint_artifact_id, source_content_hash, source_language,
                       revision, created_at, updated_at
                   )
                   SELECT :id, :root, name, :source, checkpoint_artifact_id,
                          source_content_hash, source_language, revision,
                          created_at, updated_at
                   FROM translation_projects WHERE id = :template""",
                {
                    "id": inserted_translation,
                    "root": ids["standalone_root"],
                    "source": ids["branch_two"],
                    "template": ids["translation"],
                },
                "translation project root and source do not match",
                "SELECT COUNT(*) FROM translation_projects WHERE id = :id",
                {"id": inserted_translation},
                0,
            )
            _reject_statement(
                connection,
                "UPDATE translation_projects SET project_root_id = :root WHERE id = :id",
                {"root": ids["standalone_root"], "id": ids["translation"]},
                "translation project root and source are immutable",
                "SELECT project_root_id FROM translation_projects WHERE id = :id",
                {"id": ids["translation"]},
                ids["root"],
            )
            connection.execute(
                text(
                    "UPDATE translation_projects SET project_root_id = project_root_id, "
                    "source_session_id = source_session_id, name = :name WHERE id = :id"
                ),
                {"name": "Renamed translation project", "id": ids["translation"]},
            )

            for inserted_id, branch_session in (
                (inserted_branch_wrong_root, ids["standalone"]),
                (inserted_branch_source, ids["source"]),
            ):
                _reject_statement(
                    connection,
                    """INSERT INTO translation_project_branches (
                           id, project_id, session_id, target_language,
                           source_checkpoint_artifact_id, source_content_hash, created_at
                       )
                       SELECT :id, project_id, :session_id, :language,
                              source_checkpoint_artifact_id, source_content_hash, created_at
                       FROM translation_project_branches WHERE id = :template""",
                    {
                        "id": inserted_id,
                        "session_id": branch_session,
                        "language": "it" if branch_session == ids["standalone"] else "nl",
                        "template": IDS["branch_link"],
                    },
                    "translation branch does not belong to its project root",
                    "SELECT COUNT(*) FROM translation_project_branches WHERE id = :id",
                    {"id": inserted_id},
                    0,
                )
            _reject_statement(
                connection,
                "UPDATE translation_project_branches SET project_id = :project WHERE id = :id",
                {"project": ids["foreign_translation"], "id": IDS["branch_link"]},
                "translation branch project and session are immutable",
                "SELECT project_id FROM translation_project_branches WHERE id = :id",
                {"id": IDS["branch_link"]},
                ids["translation"],
            )
            _reject_statement(
                connection,
                "UPDATE translation_project_branches SET session_id = :session WHERE id = :id",
                {"session": ids["branch_two"], "id": IDS["branch_link"]},
                "translation branch project and session are immutable",
                "SELECT session_id FROM translation_project_branches WHERE id = :id",
                {"id": IDS["branch_link"]},
                ids["branch"],
            )
            connection.execute(
                text(
                    "UPDATE translation_project_branches SET project_id = project_id, "
                    "session_id = session_id, target_language = :language WHERE id = :id"
                ),
                {"language": "de-AT", "id": IDS["branch_link"]},
            )

            _reject_statement(
                connection,
                """INSERT INTO project_folders (
                       id, name, workspace_id, parent_id, revision, created_at, updated_at
                   )
                   SELECT :id, name, workspace_id, :parent, revision, created_at, updated_at
                   FROM project_folders WHERE id = :template""",
                {
                    "id": inserted_folder_cross,
                    "parent": ids["foreign_folder"],
                    "template": ids["folder_root"],
                },
                "project folder parent must share its workspace",
                "SELECT COUNT(*) FROM project_folders WHERE id = :id",
                {"id": inserted_folder_cross},
                0,
            )
            _reject_statement(
                connection,
                """INSERT INTO project_folders (
                       id, name, workspace_id, parent_id, revision, created_at, updated_at
                   )
                   SELECT :id, name, workspace_id, :id, revision, created_at, updated_at
                   FROM project_folders WHERE id = :template""",
                {"id": inserted_folder_cycle, "template": ids["folder_root"]},
                "project folder hierarchy contains a cycle",
                "SELECT COUNT(*) FROM project_folders WHERE id = :id",
                {"id": inserted_folder_cycle},
                0,
            )
            _reject_statement(
                connection,
                "UPDATE project_folders SET workspace_id = :workspace WHERE id = :id",
                {"workspace": ids["foreign_workspace"], "id": ids["folder_root"]},
                "project folder workspace is immutable",
                "SELECT workspace_id FROM project_folders WHERE id = :id",
                {"id": ids["folder_root"]},
                ids["workspace"],
            )
            _reject_statement(
                connection,
                "UPDATE project_folders SET parent_id = :parent WHERE id = :id",
                {"parent": ids["foreign_folder"], "id": ids["folder_child"]},
                "project folder parent must share its workspace",
                "SELECT parent_id FROM project_folders WHERE id = :id",
                {"id": ids["folder_child"]},
                ids["folder_root"],
            )
            _reject_statement(
                connection,
                "UPDATE project_folders SET parent_id = :parent WHERE id = :id",
                {"parent": ids["folder_child"], "id": ids["folder_root"]},
                "project folder hierarchy contains a cycle",
                "SELECT parent_id FROM project_folders WHERE id = :id",
                {"id": ids["folder_root"]},
                None,
            )
            connection.execute(
                text(
                    "UPDATE project_folders SET workspace_id = workspace_id, "
                    "parent_id = parent_id, name = :name WHERE id = :id"
                ),
                {"name": "Renamed folder root", "id": ids["folder_root"]},
            )

            for folder_id, project_id, session_id, position in (
                (
                    ids["foreign_folder"],
                    ids["root"],
                    ids["source"],
                    20,
                ),
                (
                    ids["folder_root"],
                    ids["retained_root"],
                    ids["branch_two"],
                    21,
                ),
                (
                    ids["folder_root"],
                    ids["root"],
                    ids["branch"],
                    22,
                ),
            ):
                _reject_statement(
                    connection,
                    """INSERT INTO project_folder_memberships (
                           folder_id, project_session_id, project_id, position
                       ) VALUES (:folder, :session, :project, :position)""",
                    {
                        "folder": folder_id,
                        "session": session_id,
                        "project": project_id,
                        "position": position,
                    },
                    "project folder membership roots do not match",
                    "SELECT COUNT(*) FROM project_folder_memberships "
                    "WHERE folder_id = :folder AND project_session_id = :session",
                    {"folder": folder_id, "session": session_id},
                    0,
                )
            _reject_statement(
                connection,
                """UPDATE project_folder_memberships
                   SET project_session_id = :session
                   WHERE folder_id = :folder AND project_session_id = :old_session""",
                {
                    "session": ids["branch"],
                    "folder": ids["folder_root"],
                    "old_session": ids["source"],
                },
                "project folder membership roots do not match",
                "SELECT project_session_id FROM project_folder_memberships "
                "WHERE folder_id = :folder AND project_session_id = :session",
                {"folder": ids["folder_root"], "session": ids["source"]},
                ids["source"],
            )
            _reject_statement(
                connection,
                """UPDATE project_folder_memberships
                   SET project_id = :project
                   WHERE folder_id = :folder AND project_session_id = :session""",
                {
                    "project": ids["retained_root"],
                    "folder": ids["folder_root"],
                    "session": ids["source"],
                },
                "project folder membership roots do not match",
                "SELECT project_id FROM project_folder_memberships "
                "WHERE folder_id = :folder AND project_session_id = :session",
                {"folder": ids["folder_root"], "session": ids["source"]},
                ids["root"],
            )
            _reject_statement(
                connection,
                """UPDATE project_folder_memberships
                   SET folder_id = :folder
                   WHERE folder_id = :old_folder AND project_session_id = :session""",
                {
                    "folder": ids["foreign_folder"],
                    "old_folder": ids["folder_root"],
                    "session": ids["source"],
                },
                "project folder membership roots do not match",
                "SELECT folder_id FROM project_folder_memberships "
                "WHERE folder_id = :folder AND project_session_id = :session",
                {"folder": ids["folder_root"], "session": ids["source"]},
                ids["folder_root"],
            )
            connection.execute(
                text(
                    "UPDATE project_folder_memberships SET folder_id = folder_id, "
                    "project_id = project_id, project_session_id = project_session_id, "
                    "position = :position WHERE folder_id = :folder "
                    "AND project_session_id = :session"
                ),
                {
                    "position": 7,
                    "folder": ids["folder_root"],
                    "session": ids["source"],
                },
            )
            connection.execute(
                text(
                    "UPDATE project_folder_memberships SET folder_id = :new_folder "
                    "WHERE folder_id = :old_folder AND project_session_id = :session"
                ),
                {
                    "new_folder": ids["folder_child"],
                    "old_folder": ids["folder_root"],
                    "session": ids["source"],
                },
            )
            connection.execute(
                text(
                    "UPDATE project_folder_memberships SET folder_id = :new_folder "
                    "WHERE folder_id = :old_folder AND project_session_id = :session"
                ),
                {
                    "new_folder": ids["folder_root"],
                    "old_folder": ids["folder_child"],
                    "session": ids["source"],
                },
            )
    finally:
        database.dispose()


def test_invalid_0075_graphs_fail_before_ddl_and_preserve_exact_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-predecessors.sqlite3"
    config = _upgrade_to_0075(path)
    ids = _make_valid_predecessor(path)
    missing = "90000000-0000-4000-8000-000000000010"
    cases: tuple[_InvalidPredecessor, ...] = (
        (
            "project foreign source",
            (
                (
                    "UPDATE projects SET source_session_id = :value WHERE id = :id",
                    {"value": ids["foreign_branch"], "id": ids["root"]},
                ),
            ),
            (
                (
                    "UPDATE projects SET source_session_id = :value WHERE id = :id",
                    {"value": ids["source"], "id": ids["root"]},
                ),
            ),
            "Cannot enforce project graph consistency; projects contain invalid workspace or source roots.",
        ),
        (
            "project missing source",
            (
                (
                    "UPDATE projects SET source_session_id = :value WHERE id = :id",
                    {"value": missing, "id": ids["root"]},
                ),
            ),
            (
                (
                    "UPDATE projects SET source_session_id = :value WHERE id = :id",
                    {"value": ids["source"], "id": ids["root"]},
                ),
            ),
            "Cannot enforce project graph consistency; projects contain invalid workspace or source roots.",
        ),
        (
            "null source with sessions",
            (("UPDATE projects SET source_session_id = NULL WHERE id = :id", {"id": ids["root"]}),),
            (
                (
                    "UPDATE projects SET source_session_id = :value WHERE id = :id",
                    {"value": ids["source"], "id": ids["root"]},
                ),
            ),
            "Cannot enforce project graph consistency; projects contain invalid workspace or source roots.",
        ),
        (
            "session missing project",
            (
                (
                    "UPDATE sessions SET project_id = :value WHERE id = :id",
                    {"value": missing, "id": ids["branch"]},
                ),
            ),
            (
                (
                    "UPDATE sessions SET project_id = :value WHERE id = :id",
                    {"value": ids["root"], "id": ids["branch"]},
                ),
            ),
            "Cannot enforce project graph consistency; sessions reference missing projects.",
        ),
        (
            "translation wrong root",
            (
                (
                    "UPDATE translation_projects SET project_root_id = :value WHERE id = :id",
                    {"value": ids["standalone_root"], "id": ids["translation"]},
                ),
            ),
            (
                (
                    "UPDATE translation_projects SET project_root_id = :value WHERE id = :id",
                    {"value": ids["root"], "id": ids["translation"]},
                ),
            ),
            "Cannot enforce project graph consistency; translation projects have inconsistent roots.",
        ),
        (
            "translation missing root",
            (
                (
                    "UPDATE translation_projects SET project_root_id = :value WHERE id = :id",
                    {"value": missing, "id": ids["translation"]},
                ),
            ),
            (
                (
                    "UPDATE translation_projects SET project_root_id = :value WHERE id = :id",
                    {"value": ids["root"], "id": ids["translation"]},
                ),
            ),
            "Cannot enforce project graph consistency; translation projects have inconsistent roots.",
        ),
        (
            "branch wrong root",
            (
                (
                    "UPDATE translation_project_branches SET session_id = :value WHERE id = :id",
                    {"value": ids["standalone"], "id": IDS["branch_link"]},
                ),
            ),
            (
                (
                    "UPDATE translation_project_branches SET session_id = :value WHERE id = :id",
                    {"value": ids["branch"], "id": IDS["branch_link"]},
                ),
            ),
            "Cannot enforce project graph consistency; translation branches have inconsistent roots.",
        ),
        (
            "branch uses source session",
            (
                (
                    "UPDATE translation_project_branches SET session_id = :value WHERE id = :id",
                    {"value": ids["source"], "id": IDS["branch_link"]},
                ),
            ),
            (
                (
                    "UPDATE translation_project_branches SET session_id = :value WHERE id = :id",
                    {"value": ids["branch"], "id": IDS["branch_link"]},
                ),
            ),
            "Cannot enforce project graph consistency; translation branches have inconsistent roots.",
        ),
        (
            "branch missing translation",
            (
                (
                    "UPDATE translation_project_branches SET project_id = :value WHERE id = :id",
                    {"value": missing, "id": IDS["branch_link"]},
                ),
            ),
            (
                (
                    "UPDATE translation_project_branches SET project_id = :value WHERE id = :id",
                    {"value": ids["translation"], "id": IDS["branch_link"]},
                ),
            ),
            "Cannot enforce project graph consistency; translation branches have inconsistent roots.",
        ),
        (
            "folder cross-workspace parent",
            (
                (
                    "UPDATE project_folders SET parent_id = :value WHERE id = :id",
                    {"value": ids["foreign_folder"], "id": ids["folder_child"]},
                ),
            ),
            (
                (
                    "UPDATE project_folders SET parent_id = :value WHERE id = :id",
                    {"value": ids["folder_root"], "id": ids["folder_child"]},
                ),
            ),
            "Cannot enforce project graph consistency; folders have invalid workspace parents.",
        ),
        (
            "folder missing parent",
            (
                (
                    "UPDATE project_folders SET parent_id = :value WHERE id = :id",
                    {"value": missing, "id": ids["folder_child"]},
                ),
            ),
            (
                (
                    "UPDATE project_folders SET parent_id = :value WHERE id = :id",
                    {"value": ids["folder_root"], "id": ids["folder_child"]},
                ),
            ),
            "Cannot enforce project graph consistency; folders have invalid workspace parents.",
        ),
        (
            "folder cycle",
            (
                (
                    "UPDATE project_folders SET parent_id = :value WHERE id = :id",
                    {"value": ids["folder_child"], "id": ids["folder_root"]},
                ),
            ),
            (
                (
                    "UPDATE project_folders SET parent_id = NULL WHERE id = :id",
                    {"id": ids["folder_root"]},
                ),
            ),
            "Cannot enforce project graph consistency; folder hierarchy contains a cycle.",
        ),
        (
            "membership cross-workspace",
            (
                (
                    "UPDATE project_folder_memberships SET folder_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {
                        "value": ids["foreign_folder"],
                        "folder": ids["folder_root"],
                        "session": ids["source"],
                    },
                ),
            ),
            (
                (
                    "UPDATE project_folder_memberships SET folder_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {
                        "value": ids["folder_root"],
                        "folder": ids["foreign_folder"],
                        "session": ids["source"],
                    },
                ),
            ),
            "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        ),
        (
            "membership wrong project root",
            (
                (
                    "UPDATE project_folder_memberships SET project_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {
                        "value": ids["retained_root"],
                        "folder": ids["folder_root"],
                        "session": ids["source"],
                    },
                ),
            ),
            (
                (
                    "UPDATE project_folder_memberships SET project_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": ids["root"], "folder": ids["folder_root"], "session": ids["source"]},
                ),
            ),
            "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        ),
        (
            "membership branch session alias",
            (
                (
                    "UPDATE project_folder_memberships SET project_session_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {
                        "value": ids["branch"],
                        "folder": ids["folder_root"],
                        "session": ids["source"],
                    },
                ),
            ),
            (
                (
                    "UPDATE project_folder_memberships SET project_session_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {
                        "value": ids["source"],
                        "folder": ids["folder_root"],
                        "session": ids["branch"],
                    },
                ),
            ),
            "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        ),
        (
            "membership missing folder",
            (
                (
                    "UPDATE project_folder_memberships SET folder_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": missing, "folder": ids["folder_root"], "session": ids["source"]},
                ),
            ),
            (
                (
                    "UPDATE project_folder_memberships SET folder_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": ids["folder_root"], "folder": missing, "session": ids["source"]},
                ),
            ),
            "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        ),
        (
            "membership missing project",
            (
                (
                    "UPDATE project_folder_memberships SET project_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": missing, "folder": ids["folder_root"], "session": ids["source"]},
                ),
            ),
            (
                (
                    "UPDATE project_folder_memberships SET project_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": ids["root"], "folder": ids["folder_root"], "session": ids["source"]},
                ),
            ),
            "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        ),
        (
            "membership missing session",
            (
                (
                    "UPDATE project_folder_memberships SET project_session_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": missing, "folder": ids["folder_root"], "session": ids["source"]},
                ),
            ),
            (
                (
                    "UPDATE project_folder_memberships SET project_session_id = :value WHERE folder_id = :folder AND project_session_id = :session",
                    {"value": ids["source"], "folder": ids["folder_root"], "session": missing},
                ),
            ),
            "Cannot enforce project graph consistency; folder memberships have inconsistent roots.",
        ),
    )

    baseline = _snapshot(path)
    for _label, changes, repairs, expected_error in cases:
        _apply_sqlite(path, changes)
        invalid_snapshot = _snapshot(path)
        with pytest.raises(RuntimeError, match=re.escape(expected_error)):
            command.upgrade(config, TARGET_REVISION)
        assert _snapshot(path) == invalid_snapshot
        _apply_sqlite(path, repairs)
    assert _snapshot(path) == baseline


def _make_nullable_project_root(path: Path) -> None:
    engine = create_database_engine(path)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
            context = MigrationContext.configure(connection, opts={"render_as_batch": True})
            operations = Operations(context)
            reflected = sa.Table(
                "sessions",
                sa.MetaData(),
                autoload_with=connection,
                resolve_fks=False,
            )
            connection.commit()
            with connection.begin():
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                with operations.batch_alter_table("sessions", copy_from=reflected) as batch:
                    batch.alter_column(
                        "project_id",
                        existing_type=sa.String(length=36),
                        existing_nullable=False,
                        nullable=True,
                    )
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.parametrize("malformation", ["missing_table", "nullable_root"])
def test_malformed_or_missing_schema_fails_before_ddl(tmp_path: Path, malformation: str) -> None:
    path = tmp_path / f"malformed-{malformation}.sqlite3"
    config = _upgrade_to_0075(path)
    if malformation == "missing_table":
        _apply_sqlite(
            path,
            (("DROP TABLE project_folder_memberships", {}),),
        )
        expected = "Cannot enforce project graph consistency; a required table is missing."
    else:
        _make_nullable_project_root(path)
        expected = (
            "Cannot enforce project graph consistency; an ownership root has an unexpected shape."
        )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before


def test_trigger_name_collision_fails_before_ddl_and_preserves_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "name-collision.sqlite3"
    config = _upgrade_to_0075(path)
    _make_valid_predecessor(path)
    _apply_sqlite(
        path,
        (
            (
                "CREATE TRIGGER pandrator_project_graph_projects_insert "
                "BEFORE UPDATE ON projects BEGIN SELECT 1; END",
                {},
            ),
        ),
    )
    before = _snapshot(path)
    expected = "Cannot enforce project graph consistency; an owned trigger name is already in use."

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before


def test_trigger_ddl_failure_rolls_back_exact_predecessor_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ddl-failure.sqlite3"
    config = _upgrade_to_0075(path)
    _make_valid_predecessor(path)
    before = _snapshot(path)
    create_count = 0

    def fail_after_four_triggers(
        _connection, _cursor, statement: str, _parameters, _context, _executemany
    ) -> None:
        nonlocal create_count
        if statement.lstrip().upper().startswith(f"CREATE TRIGGER {TRIGGER_PREFIX.upper()}"):
            create_count += 1
            if create_count == 5:
                raise RuntimeError("Injected project graph trigger creation failure")

    event.listen(Engine, "before_cursor_execute", fail_after_four_triggers)
    try:
        with pytest.raises(RuntimeError, match="Injected project graph trigger creation failure"):
            command.upgrade(config, TARGET_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_four_triggers)

    assert create_count == 5
    assert _snapshot(path) == before
    assert _owned_triggers(path) == ()
    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _owned_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )


def test_downgrade_removes_only_owned_triggers_and_reupgrade_succeeds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "downgrade.sqlite3"
    config = _upgrade_to_0075(path)
    _make_valid_predecessor(path)
    command.upgrade(config, TARGET_REVISION)
    before = _snapshot(path)
    before_schema = before["schema"]
    assert isinstance(before_schema, tuple)

    command.downgrade(config, PREDECESSOR_REVISION)

    after = _snapshot(path)
    after_schema = after["schema"]
    assert isinstance(after_schema, tuple)
    assert (
        tuple(
            row
            for row in before_schema
            if not (row[0] == "trigger" and str(row[1]) in TRIGGER_NAMES)
        )
        == after_schema
    )
    assert _rows_without_alembic_version(after) == _rows_without_alembic_version(before)
    assert _version(path) == PREDECESSOR_REVISION
    assert _owned_triggers(path) == ()

    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert tuple(name for name, _table, _sql in _owned_triggers(path)) == tuple(
        sorted(TRIGGER_NAMES)
    )


def test_downgrade_rejects_altered_owned_trigger_before_any_drop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "altered-trigger.sqlite3"
    config = _upgrade_to_0075(path)
    _make_valid_predecessor(path)
    command.upgrade(config, TARGET_REVISION)
    altered_name = TRIGGER_NAMES[0]
    _apply_sqlite(
        path,
        (
            (f"DROP TRIGGER {altered_name}", {}),
            (
                f"CREATE TRIGGER {altered_name} BEFORE INSERT ON projects "
                "BEGIN SELECT RAISE(ABORT, 'altered project trigger'); END",
                {},
            ),
        ),
    )
    before = _snapshot(path)
    expected = "Cannot downgrade project graph consistency; an owned trigger is missing or altered."

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        command.downgrade(config, PREDECESSOR_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == TARGET_REVISION
