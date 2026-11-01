from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from pandrator.web.database import create_database_engine, sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0068_usage_ownership"
PREVIOUS_REVISION = "0067_research_cache_scope"
TABLE = "usage_events"
NOW = "2026-10-23 13:20:00"
WORKSPACE_INDEX = "ix_usage_events_workspace_id"
PROJECT_INDEX = "ix_usage_events_project_id"
OLD_COLUMNS = (
    "id",
    "session_id",
    "workflow_run_id",
    "job_id",
    "artifact_id",
    "generation_run_id",
    "agent_run_id",
    "request_key",
    "stage",
    "provider_key",
    "model_id",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "cost_usd",
    "cost_source",
    "raw_usage_json",
    "created_at",
)
LEGACY_TABLE_SQL = """CREATE TABLE usage_events_legacy (
    id VARCHAR(36) NOT NULL,
    session_id VARCHAR(36),
    workflow_run_id VARCHAR(36),
    job_id VARCHAR(36),
    artifact_id VARCHAR(36),
    generation_run_id VARCHAR(36),
    agent_run_id VARCHAR(36),
    request_key VARCHAR(200),
    stage VARCHAR(80),
    provider_key VARCHAR(80) NOT NULL,
    model_id VARCHAR(255) NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd FLOAT,
    cost_source VARCHAR(40),
    raw_usage_json JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_usage_event_agent_request UNIQUE (agent_run_id, request_key),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
    FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE SET NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifacts (id) ON DELETE SET NULL,
    FOREIGN KEY(generation_run_id) REFERENCES generation_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(agent_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
)"""
OLD_INDEX_SQL = (
    "CREATE INDEX ix_usage_events_session_id ON usage_events (session_id)",
    "CREATE INDEX ix_usage_events_job_id ON usage_events (job_id)",
    "CREATE INDEX ix_usage_events_artifact_id ON usage_events (artifact_id)",
    "CREATE INDEX ix_usage_events_generation_run_id ON usage_events (generation_run_id)",
    "CREATE INDEX ix_usage_events_agent_run_id ON usage_events (agent_run_id)",
    "CREATE INDEX ix_usage_events_created_at ON usage_events (created_at)",
    "CREATE INDEX idx_usage_events_session_stage_created "
    "ON usage_events (session_id, stage, created_at DESC, id DESC)",
)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0067(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("usage-schema-base") / "at-0067.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _sqlite_mutate(path: Path, callback: Any) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        callback(connection)
        connection.commit()
    finally:
        connection.close()


def _legacy_usage_database(schema_at_0067: Path, path: Path) -> None:
    shutil.copy2(schema_at_0067, path)

    def rebuild(connection: sqlite3.Connection) -> None:
        connection.execute(LEGACY_TABLE_SQL)
        quoted_columns = ", ".join(f'"{column}"' for column in OLD_COLUMNS)
        connection.execute(
            f"INSERT INTO usage_events_legacy ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM usage_events"
        )
        connection.execute("DROP TABLE usage_events")
        connection.execute("ALTER TABLE usage_events_legacy RENAME TO usage_events")
        for statement in OLD_INDEX_SQL:
            connection.execute(statement)

    _sqlite_mutate(path, rebuild)


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
        "(id, project_id, name, storage_key, legacy_name, legacy_path, workflow_kind, "
        "interface_mode, source_language, target_language, workflow_preset, "
        "included_stages_json, status, revision, created_at, updated_at, trashed_at, purge_after) "
        "VALUES (?, ?, ?, ?, NULL, NULL, 'audiobook', 'advanced', 'auto', NULL, 'custom', "
        "'[]', 'complete', 1, ?, ?, NULL, NULL)",
        (session_id, project_id, session_id, f"storage-{session_id}", NOW, NOW),
    )


def _create_project_graph(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    project_id: str,
    root_session_id: str,
    branches: tuple[str, ...] = (),
) -> None:
    _insert_session(connection, root_session_id)
    connection.execute(
        "INSERT INTO projects "
        "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, 1, ?, ?)",
        (project_id, workspace_id, root_session_id, project_id, NOW, NOW),
    )
    connection.execute(
        "UPDATE sessions SET project_id = ? WHERE id = ?",
        (project_id, root_session_id),
    )
    for session_id in branches:
        _insert_session(connection, session_id, project_id=project_id)


def _insert_workflow(connection: sqlite3.Connection, workflow_id: str, session_id: str) -> None:
    connection.execute(
        "INSERT INTO workflow_runs "
        "(id, session_id, preset, status, settings_json, created_at, updated_at) "
        "VALUES (?, ?, 'custom', 'complete', '{}', ?, ?)",
        (workflow_id, session_id, NOW, NOW),
    )


def _insert_job(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    workspace_id: str | None,
    project_id: str | None = None,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO jobs "
        "(id, kind, workspace_id, project_id, session_id, workflow_run_id, status, "
        "payload_json, resource_keys_json, result_json, progress, progress_detail, error_code, "
        "error_message, lease_owner, lease_expires_at, lease_generation, available_at, attempts, "
        "max_attempts, created_at, started_at, finished_at, updated_at) "
        "VALUES (?, 'generic.test', ?, ?, ?, ?, 'succeeded', '{}', '[]', NULL, 0, NULL, NULL, "
        "NULL, NULL, NULL, 0, NULL, 0, 1, ?, NULL, ?, ?)",
        (job_id, workspace_id, project_id, session_id, workflow_run_id, NOW, NOW, NOW),
    )


def _insert_artifact(
    connection: sqlite3.Connection,
    artifact_id: str,
    *,
    workspace_id: str | None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO artifacts "
        "(id, workspace_id, project_id, session_id, kind, role, relative_path, mime_type, "
        "size_bytes, content_hash, state, settings_hash, metadata_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'audio', 'output', ?, 'audio/wav', 12, 'hash', 'ready', NULL, '{}', ?, ?)",
        (
            artifact_id,
            workspace_id,
            project_id,
            session_id,
            f"artifacts/{artifact_id}.wav",
            NOW,
            NOW,
        ),
    )


def _insert_source_asset(
    connection: sqlite3.Connection,
    asset_id: str,
    *,
    artifact_id: str,
    workspace_id: str | None,
) -> None:
    connection.execute(
        "INSERT INTO source_assets "
        "(id, workspace_id, artifact_id, display_name, kind, mime_type, external_path, "
        "size_bytes, content_hash, state, metadata_json, revision, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'text', 'text/plain', NULL, 12, 'hash', 'current', '{}', 1, ?, ?)",
        (asset_id, workspace_id, artifact_id, asset_id, NOW, NOW),
    )


def _insert_generation_run(
    connection: sqlite3.Connection,
    generation_run_id: str,
    *,
    session_id: str,
    job_id: str | None = None,
) -> None:
    plan_id = f"plan-{generation_run_id}"
    revision_id = f"revision-{generation_run_id}"
    connection.execute(
        "INSERT INTO generation_plans (id, session_id, active_revision_id, created_at, updated_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        (plan_id, session_id, NOW, NOW),
    )
    connection.execute(
        "INSERT INTO generation_plan_revisions "
        "(id, plan_id, source_revision_id, parent_revision_id, revision_number, settings_json, "
        "operation_json, content_hash, created_at) VALUES (?, ?, NULL, NULL, 1, '{}', '{}', 'hash', ?)",
        (revision_id, plan_id, NOW),
    )
    connection.execute(
        "INSERT INTO generation_runs "
        "(id, session_id, plan_revision_id, source_generation_run_id, output_generation_run_id, "
        "job_id, sequence_number, operation, status, pause_requested, cancel_requested, "
        "resume_source_on_completion, settings_snapshot_json, audio_version_json, settings_hash, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?, 1, 'generate', 'complete', 0, 0, 0, '{}', NULL, NULL, ?, ?)",
        (generation_run_id, session_id, revision_id, job_id, NOW, NOW),
    )


def _insert_agent_run(
    connection: sqlite3.Connection,
    agent_run_id: str,
    *,
    session_id: str,
    job_id: str | None = None,
    source_artifact_id: str | None = None,
    result_artifact_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO agent_runs "
        "(id, kind, session_id, source_artifact_id, result_artifact_id, job_id, status, "
        "source_content_hash, settings_hash, checkpoint_revision, error_message, settings_json, "
        "created_at, updated_at) "
        "VALUES (?, 'source_cleaning', ?, ?, ?, ?, 'complete', NULL, NULL, 0, NULL, '{}', ?, ?)",
        (agent_run_id, session_id, source_artifact_id, result_artifact_id, job_id, NOW, NOW),
    )


def _insert_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
    job_id: str | None = None,
    artifact_id: str | None = None,
    generation_run_id: str | None = None,
    agent_run_id: str | None = None,
    request_key: str | None = None,
    stage: str | None = "test",
    provider_key: str = "provider-test",
    model_id: str = "model-test",
    input_tokens: int = 3,
    cached_input_tokens: int = 1,
    output_tokens: int = 2,
    cost_usd: float | None = 0.0,
    cost_source: str | None = "receipt",
    raw_usage_json: str = '{ "usage": { "input": 3 } }',
) -> None:
    connection.execute(
        "INSERT INTO usage_events "
        "(id, session_id, workflow_run_id, job_id, artifact_id, generation_run_id, agent_run_id, "
        "request_key, stage, provider_key, model_id, input_tokens, cached_input_tokens, "
        "output_tokens, cost_usd, cost_source, raw_usage_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            session_id,
            workflow_run_id,
            job_id,
            artifact_id,
            generation_run_id,
            agent_run_id,
            request_key,
            stage,
            provider_key,
            model_id,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            cost_usd,
            cost_source,
            raw_usage_json,
            NOW,
        ),
    )


def _usage_rows(path: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as connection:
        columns = ", ".join(f'"{column}"' for column in OLD_COLUMNS)
        rows = connection.execute(f"SELECT {columns} FROM usage_events ORDER BY id").fetchall()
    return rows


def _snapshot(path: Path) -> tuple[list[tuple[Any, ...]], dict[str, list[tuple[Any, ...]]]]:
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        data: dict[str, list[tuple[Any, ...]]] = {}
        for table_name in table_names:
            rows = connection.execute(f'SELECT * FROM "{table_name}"').fetchall()
            data[table_name] = sorted(rows, key=repr)
    return schema, data


def _index_sql(path: Path, *, table_name: str = TABLE) -> dict[str, str | None]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
            (table_name,),
        ).fetchall()
    return {str(name): sql for name, sql in rows}


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


def test_legacy_rows_adopt_all_six_typed_roots_and_preserve_null_history(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "legacy-roots.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-main",
            root_session_id="session-main",
            branches=("session-branch",),
        )
        _insert_workflow(connection, "workflow-main", "session-main")
        _insert_job(
            connection,
            "job-main",
            workspace_id=workspace_id,
            project_id="project-main",
            session_id="session-main",
            workflow_run_id="workflow-main",
        )
        _insert_artifact(
            connection,
            "artifact-main",
            workspace_id=workspace_id,
            project_id="project-main",
            session_id="session-main",
        )
        _insert_source_asset(
            connection,
            "source-asset-main",
            artifact_id="artifact-main",
            workspace_id=workspace_id,
        )
        _insert_artifact(
            connection,
            "artifact-result",
            workspace_id=workspace_id,
            project_id="project-main",
            session_id="session-main",
        )
        _insert_generation_run(
            connection,
            "generation-main",
            session_id="session-main",
            job_id="job-main",
        )
        _insert_agent_run(
            connection,
            "agent-main",
            session_id="session-main",
            job_id="job-main",
            source_artifact_id="artifact-main",
            result_artifact_id="artifact-result",
        )
        _insert_event(connection, "event-session", session_id="session-main")
        _insert_event(connection, "event-workflow", workflow_run_id="workflow-main")
        _insert_event(connection, "event-job", job_id="job-main")
        _insert_event(connection, "event-artifact", artifact_id="artifact-main")
        _insert_event(connection, "event-generation", generation_run_id="generation-main")
        _insert_event(
            connection,
            "event-agent",
            agent_run_id="agent-main",
            request_key="unit-main",
        )
        _insert_event(
            connection,
            "event-unbound",
            stage=None,
            cost_usd=None,
            cost_source=None,
            raw_usage_json="  []  ",
        )

    original_rows = _usage_rows(path)
    command.upgrade(_config(path), REVISION)

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT id, workspace_id, project_id, raw_usage_json, cost_usd FROM usage_events "
            "ORDER BY id"
        ).fetchall()
        expected_workspace = _owner_workspace(connection)[1]
        assert all(row[1] == expected_workspace for row in rows)
        assert all(row[2] == "project-main" for row in rows if row[0] != "event-unbound")
        unbound = next(row for row in rows if row[0] == "event-unbound")
        assert unbound[2] is None
        assert unbound[3] == "  []  "
        assert unbound[4] is None
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == (REVISION,)
    assert _usage_rows(path) == original_rows


def test_workspace_only_library_artifact_does_not_erase_project_evidence(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "mixed-library-root.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-main",
            root_session_id="session-main",
        )
        _insert_artifact(
            connection,
            "artifact-library",
            workspace_id=workspace_id,
        )
        _insert_source_asset(
            connection,
            "asset-library",
            artifact_id="artifact-library",
            workspace_id=workspace_id,
        )
        _insert_event(
            connection,
            "event-mixed",
            session_id="session-main",
            artifact_id="artifact-library",
        )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-mixed'"
        ).fetchone()
        assert row == (workspace_id, "project-main")


def test_agent_source_artifact_is_workspace_input_not_billing_project(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "agent-source-project.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-billing",
            root_session_id="session-billing",
        )
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-source",
            root_session_id="session-source",
        )
        _insert_artifact(
            connection,
            "artifact-from-other-project",
            workspace_id=workspace_id,
            project_id="project-source",
        )
        _insert_artifact(
            connection,
            "artifact-result-billing",
            workspace_id=workspace_id,
            project_id="project-billing",
            session_id="session-billing",
        )
        _insert_agent_run(
            connection,
            "agent-billing",
            session_id="session-billing",
            source_artifact_id="artifact-from-other-project",
            result_artifact_id="artifact-result-billing",
        )
        _insert_event(connection, "event-agent-source", agent_run_id="agent-billing")
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-agent-source'"
        ).fetchone() == (workspace_id, "project-billing")


def test_agent_source_artifact_from_another_workspace_fails_before_ddl(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "agent-source-workspace.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-source")
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-billing",
            root_session_id="session-billing",
        )
        _create_project_graph(
            connection,
            workspace_id="workspace-source",
            project_id="project-source",
            root_session_id="session-source",
        )
        _insert_artifact(
            connection,
            "artifact-other-workspace",
            workspace_id="workspace-source",
            project_id="project-source",
        )
        _insert_agent_run(
            connection,
            "agent-source-workspace",
            session_id="session-billing",
            source_artifact_id="artifact-other-workspace",
        )
        _insert_event(connection, "event-agent-workspace", agent_run_id="agent-source-workspace")
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize("conflict", ["workspace", "project"])
def test_conflicting_direct_roots_fail_before_ddl(
    schema_at_0067: Path, tmp_path: Path, conflict: str
) -> None:
    path = tmp_path / f"conflict-{conflict}.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        other_workspace_id = workspace_id if conflict == "project" else "workspace-other"
        if conflict == "workspace":
            _create_workspace(connection, other_workspace_id)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-one",
            root_session_id="session-one",
        )
        _create_project_graph(
            connection,
            workspace_id=other_workspace_id,
            project_id="project-two",
            root_session_id="session-two",
        )
        _insert_artifact(
            connection,
            "artifact-two",
            workspace_id=other_workspace_id,
            project_id="project-two",
        )
        _insert_event(
            connection,
            "event-conflict",
            session_id="session-one",
            artifact_id="artifact-two",
        )
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize(
    "case",
    ["missing_session", "blank_session", "unowned_job", "unowned_artifact"],
)
def test_missing_malformed_or_unowned_roots_fail_before_ddl(
    schema_at_0067: Path, tmp_path: Path, case: str
) -> None:
    path = tmp_path / f"bad-root-{case}.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        if case in {"missing_session", "blank_session"}:
            _insert_event(
                connection,
                "event-bad",
                session_id="missing-session" if case == "missing_session" else "   ",
            )
        elif case == "unowned_job":
            _insert_job(connection, "job-unowned", workspace_id=None)
            _insert_event(connection, "event-bad", job_id="job-unowned")
        else:
            _insert_artifact(
                connection,
                "artifact-unowned",
                workspace_id=None,
            )
            _insert_event(connection, "event-bad", artifact_id="artifact-unowned")
    _assert_upgrade_fails_unchanged(path)


def test_source_asset_backreference_must_match_artifact_workspace_before_ddl(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "source-asset-workspace-conflict.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-other")
        _insert_artifact(
            connection,
            "artifact-source",
            workspace_id=workspace_id,
        )
        _insert_source_asset(
            connection,
            "asset-conflict",
            artifact_id="artifact-source",
            workspace_id="workspace-other",
        )
        _insert_event(connection, "event-source", artifact_id="artifact-source")
    _assert_upgrade_fails_unchanged(path)


def test_null_project_source_with_remaining_session_is_rejected_before_ddl(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "null-source-project.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "INSERT INTO projects "
            "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
            "created_at, updated_at) VALUES ('project-retained', ?, NULL, 'Retained', NULL, 1, ?, ?)",
            (workspace_id, NOW, NOW),
        )
        _insert_session(connection, "session-dangling-project", project_id="project-retained")
        _insert_event(connection, "event-retained", session_id="session-dangling-project")
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize("root_kind", ["session", "workflow"])
def test_job_execution_root_requires_matching_stored_project_before_ddl(
    schema_at_0067: Path, tmp_path: Path, root_kind: str
) -> None:
    path = tmp_path / f"job-partial-project-{root_kind}.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-job",
            root_session_id="session-job",
        )
        workflow_id = None
        session_id = "session-job" if root_kind == "session" else None
        if root_kind == "workflow":
            workflow_id = "workflow-job"
            _insert_workflow(connection, workflow_id, "session-job")
        _insert_job(
            connection,
            "job-partial-project",
            workspace_id=workspace_id,
            project_id=None,
            session_id=session_id,
            workflow_run_id=workflow_id,
        )
        _insert_event(connection, "event-job-partial", job_id="job-partial-project")
    _assert_upgrade_fails_unchanged(path)


def test_retained_sessionless_project_is_a_valid_receipt_root(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "retained-project-root.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "INSERT INTO projects "
            "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
            "created_at, updated_at) VALUES ('project-retained', ?, NULL, 'Retained', NULL, 1, ?, ?)",
            (workspace_id, NOW, NOW),
        )
        _insert_artifact(
            connection,
            "artifact-retained-project",
            workspace_id=workspace_id,
            project_id="project-retained",
        )
        _insert_event(
            connection,
            "event-retained-project",
            artifact_id="artifact-retained-project",
        )
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-retained-project'"
        ).fetchone() == (workspace_id, "project-retained")


def test_archived_workspace_root_remains_valid_for_history(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "archived-workspace-root.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, _initial_workspace_id = _owner_workspace(connection)
        _create_workspace(connection, "workspace-archived", state="archived")
        _create_project_graph(
            connection,
            workspace_id="workspace-archived",
            project_id="project-archived",
            root_session_id="session-archived",
        )
        _insert_event(connection, "event-archived", session_id="session-archived")
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-archived'"
        ).fetchone() == ("workspace-archived", "project-archived")


def test_seeded_owner_is_validated_even_when_events_have_typed_roots(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "invalid-seed.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-main",
            root_session_id="session-main",
        )
        _insert_event(connection, "event-rooted", session_id="session-main")
        connection.execute(
            "UPDATE workspace_memberships SET role = 'member' "
            "WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, owner_id),
        )
    _assert_upgrade_fails_unchanged(path)


@pytest.mark.parametrize("shape", ["partial", "malformed", "populated"])
def test_partial_malformed_and_populated_target_schemas_fail_atomically(
    schema_at_0067: Path, tmp_path: Path, shape: str
) -> None:
    path = tmp_path / f"target-{shape}.sqlite"
    if shape == "populated":
        shutil.copy2(schema_at_0067, path)
        with sqlite3.connect(path) as connection:
            _owner_id, workspace_id = _owner_workspace(connection)
            _insert_event(connection, "event-prepopulated")
            connection.execute(
                "UPDATE usage_events SET workspace_id = ? WHERE id = 'event-prepopulated'",
                (workspace_id,),
            )
    else:
        _legacy_usage_database(schema_at_0067, path)

        def add_scope(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE usage_events ADD COLUMN workspace_id VARCHAR(36)")
            if shape == "malformed":
                connection.execute("ALTER TABLE usage_events ADD COLUMN project_id VARCHAR(36)")
                connection.execute(
                    "CREATE INDEX ix_usage_events_workspace_id ON usage_events (workspace_id)"
                )
                connection.execute(
                    "CREATE INDEX ix_usage_events_project_id ON usage_events (project_id)"
                )

        _sqlite_mutate(path, add_scope)
    _assert_upgrade_fails_unchanged(path)


def test_correct_early_target_with_both_null_columns_is_adopted(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-target.sqlite"
    shutil.copy2(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _insert_event(connection, "event-early")
        assert connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-early'"
        ).fetchone() == (None, None)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-early'"
        ).fetchone() == (workspace_id, None)


def test_scope_restricts_project_delete_and_old_session_fk_still_sets_null(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "retained-receipt.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id="project-retained",
            root_session_id="session-retained",
        )
        _insert_event(
            connection,
            "event-retained",
            session_id="session-retained",
        )
    command.upgrade(_config(path), REVISION)

    engine = create_database_engine(path)
    with engine.begin() as connection:
        foreign_keys = sa.inspect(connection).get_foreign_keys(TABLE)
        named = {
            item["name"]: (
                item["referred_table"],
                (item.get("options") or {}).get("ondelete"),
            )
            for item in foreign_keys
        }
        assert named["fk_usage_events_workspace_id_workspaces"] == ("workspaces", "RESTRICT")
        assert named["fk_usage_events_project_id_projects"] == ("projects", "RESTRICT")
        with pytest.raises(IntegrityError):
            connection.execute(sa.text("DELETE FROM projects WHERE id = 'project-retained'"))

    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM sessions WHERE id = 'session-retained'"))
        row = connection.execute(
            sa.text(
                "SELECT session_id, workspace_id, project_id FROM usage_events "
                "WHERE id = 'event-retained'"
            )
        ).one()
        assert tuple(row) == (None, workspace_id, "project-retained")
    engine.dispose()


def test_late_failure_rolls_back_schema_data_and_version_then_retry_downgrade_repeat(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "late-failure.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        _insert_event(
            connection,
            "event-history",
            raw_usage_json=' { "workspace_id": "decoy", "project_id": "decoy" } ',
            cost_usd=0.0,
        )
        connection.execute(
            "CREATE INDEX custom_usage_desc "
            "ON usage_events (cost_usd DESC, created_at DESC, id ASC) "
            "WHERE stage IS NOT NULL"
        )
        connection.execute(
            "CREATE TRIGGER fail_usage_version BEFORE UPDATE OF version_num ON alembic_version "
            "WHEN NEW.version_num = '0068_usage_ownership' BEGIN "
            "SELECT RAISE(ABORT, 'late migration failure'); END"
        )
    original_rows = _usage_rows(path)
    before = _snapshot(path)
    with pytest.raises(SQLAlchemyError, match="late migration failure"):
        command.upgrade(_config(path), REVISION)
    assert _snapshot(path) == before
    assert _usage_rows(path) == original_rows
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).fetchall()
            == []
        )

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_usage_version")
    command.upgrade(_config(path), REVISION)
    upgraded_snapshot = _snapshot(path)
    assert _usage_rows(path) == original_rows
    assert _index_sql(path)["custom_usage_desc"] == (
        "CREATE INDEX custom_usage_desc ON usage_events "
        "(cost_usd DESC, created_at DESC, id ASC) WHERE stage IS NOT NULL"
    )
    command.upgrade(_config(path), REVISION)
    assert _snapshot(path) == upgraded_snapshot

    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _usage_rows(path) == original_rows
    legacy_indexes = _index_sql(path)
    assert legacy_indexes["custom_usage_desc"] == (
        "CREATE INDEX custom_usage_desc ON usage_events "
        "(cost_usd DESC, created_at DESC, id ASC) WHERE stage IS NOT NULL"
    )
    assert WORKSPACE_INDEX not in legacy_indexes
    assert PROJECT_INDEX not in legacy_indexes
    command.upgrade(_config(path), REVISION)
    assert _usage_rows(path) == original_rows


def test_existing_ascending_composite_index_survives_upgrade_and_downgrade_exactly(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "ascending-composite-index.sqlite"
    shutil.copy2(schema_at_0067, path)
    asc_index_sql = (
        "CREATE INDEX idx_usage_events_session_stage_created ON usage_events "
        "(session_id, stage, created_at, id)"
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_usage_events_session_stage_created")
        connection.execute(asc_index_sql)
        _insert_event(
            connection,
            "event-ascending-index",
            cost_usd=None,
            cost_source=None,
            raw_usage_json=' { "retained": "ascending-index" } ',
        )

    original_rows = _usage_rows(path)
    command.upgrade(_config(path), REVISION)
    assert _usage_rows(path) == original_rows
    assert _index_sql(path)["idx_usage_events_session_stage_created"] == asc_index_sql

    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _usage_rows(path) == original_rows
    assert _index_sql(path)["idx_usage_events_session_stage_created"] == asc_index_sql


def test_opaque_whitespace_root_ids_are_looked_up_without_normalization(
    schema_at_0067: Path, tmp_path: Path
) -> None:
    path = tmp_path / "opaque-ids.sqlite"
    _legacy_usage_database(schema_at_0067, path)
    with sqlite3.connect(path) as connection:
        owner_id, default_workspace_id = _owner_workspace(connection)
        workspace_id = " workspace-opaque "
        _create_workspace(connection, workspace_id)
        project_id = " project-opaque "
        session_id = " session-opaque "
        _create_project_graph(
            connection,
            workspace_id=workspace_id,
            project_id=project_id,
            root_session_id=session_id,
        )
        _insert_event(connection, "event-opaque", session_id=session_id)
        assert default_workspace_id != workspace_id
        assert owner_id
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id, project_id FROM usage_events WHERE id = 'event-opaque'"
        ).fetchone() == (" workspace-opaque ", " project-opaque ")
