from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError

from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    AgentRun,
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    SessionRecord,
    UsageEvent,
    WorkflowRun,
)
from pandrator.web.projects.models import Project

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
TARGET_REVISION = "0079_execution_scope_consistency"
PREDECESSOR_REVISION = "0078_library_scope_consistency"
TIMESTAMP = "2026-10-24 12:00:00"

TRIGGERS = (
    ("pandrator_execution_scope_jobs_insert", "jobs"),
    ("pandrator_execution_scope_jobs_update", "jobs"),
    ("pandrator_execution_scope_workflow_runs_update", "workflow_runs"),
    ("pandrator_execution_scope_generation_runs_insert", "generation_runs"),
    ("pandrator_execution_scope_generation_runs_update", "generation_runs"),
    ("pandrator_execution_scope_agent_runs_insert", "agent_runs"),
    ("pandrator_execution_scope_agent_runs_update", "agent_runs"),
    ("pandrator_execution_scope_usage_events_insert", "usage_events"),
    ("pandrator_execution_scope_usage_events_update", "usage_events"),
)

PARENT_TRIGGERS = (
    ("pandrator_project_graph_projects_update", "projects"),
    ("pandrator_project_graph_sessions_update", "sessions"),
    ("pandrator_media_scope_artifacts_update", "artifacts"),
    ("pandrator_library_scope_jobs_update", "jobs"),
)


@dataclass
class ExecutionCase:
    path: Path
    config: Config
    database: Database
    ids: dict[str, str]


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _make_case(path: Path) -> ExecutionCase:
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    database = Database(path)
    ids: dict[str, str] = {}

    with database.session() as session:
        workspace = Workspace(kind="team", name="Execution workspace")
        other_workspace = Workspace(kind="team", name="Other workspace")
        session.add_all([workspace, other_workspace])
        session.flush()
        ids["workspace"] = workspace.id
        ids["workspace_other"] = other_workspace.id

        project = Project(
            workspace_id=workspace.id,
            source_session_id=None,
            name="Execution project",
            created_by_user_id=None,
        )
        sibling_project = Project(
            workspace_id=workspace.id,
            source_session_id=None,
            name="Same workspace sibling project",
            created_by_user_id=None,
        )
        foreign_project = Project(
            workspace_id=other_workspace.id,
            source_session_id=None,
            name="Foreign project",
            created_by_user_id=None,
        )
        session.add_all([project, sibling_project, foreign_project])
        session.flush()
        ids.update(
            project=project.id,
            project_sibling=sibling_project.id,
            project_foreign=foreign_project.id,
        )

        session_a = SessionRecord(project_id=project.id, name="Execution session")
        session_a_sibling = SessionRecord(
            project_id=project.id, name="Same project sibling session"
        )
        session_sibling = SessionRecord(
            project_id=sibling_project.id, name="Same workspace sibling project session"
        )
        session_foreign = SessionRecord(project_id=foreign_project.id, name="Foreign session")
        session.add_all([session_a, session_a_sibling, session_sibling, session_foreign])
        session.flush()
        ids.update(
            session=session_a.id,
            session_sibling=session_a_sibling.id,
            session_other_project=session_sibling.id,
            session_foreign=session_foreign.id,
        )

        workflow = WorkflowRun(session_id=session_a.id, preset="synthetic")
        workflow_sibling = WorkflowRun(session_id=session_a_sibling.id, preset="synthetic")
        workflow_other_project = WorkflowRun(session_id=session_sibling.id, preset="synthetic")
        workflow_foreign = WorkflowRun(session_id=session_foreign.id, preset="synthetic")
        session.add_all([workflow, workflow_sibling, workflow_other_project, workflow_foreign])
        session.flush()
        ids.update(
            workflow=workflow.id,
            workflow_sibling=workflow_sibling.id,
            workflow_other_project=workflow_other_project.id,
            workflow_foreign=workflow_foreign.id,
        )

        jobs = {
            "job_workspace": Job(kind="synthetic.workspace-only", workspace_id=workspace.id),
            "job_project": Job(
                kind="synthetic.project-only",
                workspace_id=workspace.id,
                project_id=project.id,
            ),
            "job_exact": Job(
                kind="synthetic.exact",
                workspace_id=workspace.id,
                project_id=project.id,
                session_id=session_a.id,
                workflow_run_id=workflow.id,
            ),
            "job_sibling": Job(
                kind="synthetic.sibling-session",
                workspace_id=workspace.id,
                project_id=project.id,
                session_id=session_a_sibling.id,
                workflow_run_id=workflow_sibling.id,
            ),
            "job_other_project": Job(
                kind="synthetic.sibling-project",
                workspace_id=workspace.id,
                project_id=sibling_project.id,
                session_id=session_sibling.id,
                workflow_run_id=workflow_other_project.id,
            ),
            "job_foreign": Job(
                kind="synthetic.foreign",
                workspace_id=other_workspace.id,
                project_id=foreign_project.id,
                session_id=session_foreign.id,
                workflow_run_id=workflow_foreign.id,
            ),
            "job_workflow_only": Job(
                kind="synthetic.workflow-only",
                workspace_id=workspace.id,
                project_id=project.id,
                workflow_run_id=workflow.id,
            ),
        }
        session.add_all(jobs.values())
        session.flush()
        ids.update({name: job.id for name, job in jobs.items()})

        artifacts = {
            "artifact": Artifact(
                workspace_id=workspace.id,
                project_id=project.id,
                kind="text",
                role="fixture",
                relative_path="execution/alpha.txt",
                size_bytes=1,
                content_hash="alpha",
            ),
            "artifact_alt": Artifact(
                workspace_id=workspace.id,
                project_id=project.id,
                kind="text",
                role="fixture",
                relative_path="execution/alpha-alt.txt",
                size_bytes=1,
                content_hash="alpha-alt",
            ),
            "artifact_workspace": Artifact(
                workspace_id=workspace.id,
                project_id=None,
                kind="text",
                role="fixture",
                relative_path="execution/workspace.txt",
                size_bytes=1,
                content_hash="workspace",
            ),
            "artifact_other_project": Artifact(
                workspace_id=workspace.id,
                project_id=sibling_project.id,
                kind="text",
                role="fixture",
                relative_path="execution/sibling-project.txt",
                size_bytes=1,
                content_hash="sibling-project",
            ),
            "artifact_foreign": Artifact(
                workspace_id=other_workspace.id,
                project_id=foreign_project.id,
                kind="text",
                role="fixture",
                relative_path="execution/foreign.txt",
                size_bytes=1,
                content_hash="foreign",
            ),
        }
        session.add_all(artifacts.values())
        session.flush()
        ids.update({name: artifact.id for name, artifact in artifacts.items()})

        def make_generation(session_id: str, sequence_number: int = 1) -> GenerationRun:
            plan = GenerationPlan(session_id=session_id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                content_hash=f"plan-{session_id}",
            )
            session.add(revision)
            session.flush()
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=sequence_number,
            )
            session.add(run)
            session.flush()
            ids[f"revision-{session_id}"] = revision.id
            return run

        generation = make_generation(session_a.id)
        generation_sibling = make_generation(session_a_sibling.id)
        generation_foreign = make_generation(session_foreign.id)
        ids.update(
            generation=generation.id,
            generation_sibling=generation_sibling.id,
            generation_foreign=generation_foreign.id,
        )

        agent = AgentRun(
            session_id=session_a.id,
            source_artifact_id=artifacts["artifact_other_project"].id,
            result_artifact_id=artifacts["artifact"].id,
        )
        agent_sibling = AgentRun(session_id=session_a_sibling.id)
        agent_foreign = AgentRun(
            session_id=session_foreign.id,
            source_artifact_id=artifacts["artifact_foreign"].id,
            result_artifact_id=artifacts["artifact_foreign"].id,
        )
        session.add_all([agent, agent_sibling, agent_foreign])
        session.flush()
        ids.update(
            agent=agent.id,
            agent_sibling=agent_sibling.id,
            agent_foreign=agent_foreign.id,
        )

        receipt = UsageEvent(
            workspace_id=workspace.id,
            project_id=project.id,
            session_id=session_a.id,
            workflow_run_id=workflow.id,
            job_id=jobs["job_exact"].id,
            artifact_id=artifacts["artifact"].id,
            generation_run_id=generation.id,
            agent_run_id=agent.id,
            provider_key="synthetic",
            model_id="synthetic/model",
        )
        session.add(receipt)
        session.flush()
        ids["usage"] = receipt.id

    return ExecutionCase(path=path, config=config, database=database, ids=ids)


@pytest.fixture
def execution_case(tmp_path: Path) -> Iterator[ExecutionCase]:
    case = _make_case(tmp_path / "execution-scope.sqlite3")
    try:
        yield case
    finally:
        case.database.dispose()


@pytest.fixture
def current_case(execution_case: ExecutionCase) -> ExecutionCase:
    command.upgrade(execution_case.config, TARGET_REVISION)
    return execution_case


def _connect(path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _execute(path: Path, sql: str, parameters: dict[str, object] | None = None) -> None:
    with _connect(path) as connection:
        connection.execute(sql, parameters or {})


def _assert_abort(
    path: Path,
    sql: str,
    parameters: dict[str, object],
    message: str,
) -> None:
    with _connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            connection.execute(sql, parameters)


def _insert_job(
    path: Path,
    row_id: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    with _connect(path) as connection:
        connection.execute(
            """INSERT INTO jobs (
                id, kind, workspace_id, project_id, session_id, workflow_run_id,
                status, payload_json, resource_keys_json, result_json, progress,
                progress_detail, error_code, error_message, lease_owner,
                lease_expires_at, lease_generation, available_at, attempts,
                max_attempts, created_at, started_at, finished_at, updated_at
            ) VALUES (
                :id, 'synthetic.direct', :workspace_id, :project_id, :session_id,
                :workflow_run_id, 'queued', '{}', '[]', NULL, 0.0, NULL, NULL,
                NULL, NULL, NULL, 0, NULL, 0, 1, :now, NULL, NULL, :now
            )""",
            {
                "id": row_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "session_id": session_id,
                "workflow_run_id": workflow_run_id,
                "now": TIMESTAMP,
            },
        )


def _insert_usage(
    path: Path,
    row_id: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
    job_id: str | None = None,
    artifact_id: str | None = None,
    generation_run_id: str | None = None,
    agent_run_id: str | None = None,
) -> None:
    with _connect(path) as connection:
        connection.execute(
            """INSERT INTO usage_events (
                id, workspace_id, project_id, session_id, workflow_run_id, job_id,
                artifact_id, generation_run_id, agent_run_id, request_key, stage,
                provider_key, model_id, input_tokens, cached_input_tokens,
                output_tokens, cost_usd, cost_source, raw_usage_json, created_at
            ) VALUES (
                :id, :workspace_id, :project_id, :session_id, :workflow_run_id,
                :job_id, :artifact_id, :generation_run_id, :agent_run_id, NULL,
                'synthetic', 'synthetic', 'synthetic/model', 0, 0, 0, NULL, NULL,
                '{}', :now
            )""",
            {
                "id": row_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "session_id": session_id,
                "workflow_run_id": workflow_run_id,
                "job_id": job_id,
                "artifact_id": artifact_id,
                "generation_run_id": generation_run_id,
                "agent_run_id": agent_run_id,
                "now": TIMESTAMP,
            },
        )


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _snapshot(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name != 'sqlite_sequence' ORDER BY type, name"
            )
        )
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT IN ('sqlite_sequence', 'alembic_version') ORDER BY name"
            )
        ]
        data: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            try:
                rows = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
            except sqlite3.OperationalError:
                rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
            data[table] = tuple(tuple(row) for row in rows)
        version_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        )
    return {"schema": schema, "data": data, "version": version_rows}


def _non_owned_schema(snapshot: dict[str, object]) -> tuple[tuple[object, ...], ...]:
    schema = snapshot["schema"]
    assert isinstance(schema, tuple)
    owned_names = {name for name, _table in TRIGGERS}
    return tuple(row for row in schema if row[1] not in owned_names)


def _trigger_rows(path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE 'pandrator_execution_scope_%' "
            "ORDER BY name"
        ).fetchall()
    return tuple((str(name), str(table), str(sql)) for name, table, sql in rows)


def test_fresh_upgrade_installs_exact_triggers_and_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "fresh-execution-scope.sqlite3"
    config = _config(path)

    command.upgrade(config, TARGET_REVISION)

    assert _version(path) == TARGET_REVISION
    assert tuple((name, table) for name, table, _sql in _trigger_rows(path)) == tuple(
        sorted(TRIGGERS)
    )
    before_repeat = _snapshot(path)
    command.upgrade(config, TARGET_REVISION)
    assert _snapshot(path) == before_repeat


def test_populated_round_trip_preserves_rows_indexes_and_unrelated_triggers(
    execution_case: ExecutionCase,
) -> None:
    case = execution_case
    _execute(
        case.path,
        "CREATE TRIGGER trg_unrelated_execution_scope_test "
        "AFTER UPDATE OF status ON jobs BEGIN SELECT NEW.id; END",
    )
    before = _snapshot(case.path)

    command.upgrade(case.config, TARGET_REVISION)
    after_upgrade = _snapshot(case.path)
    assert after_upgrade["data"] == before["data"]
    assert _non_owned_schema(after_upgrade) == _non_owned_schema(before)
    assert len(_trigger_rows(case.path)) == len(TRIGGERS)

    command.downgrade(case.config, PREDECESSOR_REVISION)
    after_downgrade = _snapshot(case.path)
    assert after_downgrade["data"] == before["data"]
    assert after_downgrade["schema"] == before["schema"]
    assert _version(case.path) == PREDECESSOR_REVISION

    command.upgrade(case.config, TARGET_REVISION)
    after_reupgrade = _snapshot(case.path)
    assert after_reupgrade["data"] == before["data"]
    assert _non_owned_schema(after_reupgrade) == _non_owned_schema(before)
    assert tuple((name, table) for name, table, _sql in _trigger_rows(case.path)) == tuple(
        sorted(TRIGGERS)
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project_id": "project_foreign"}, "job project must share its workspace"),
        ({"session_id": "session"}, "job session must match its project"),
        ({"workflow_run_id": "workflow"}, "job workflow must match its project and session"),
        (
            {"workspace_id": "workspace_other", "project_id": "project"},
            "job project must share its workspace",
        ),
        (
            {"project_id": "project", "session_id": "session_foreign"},
            "job session must match its project",
        ),
        (
            {"project_id": "project", "workflow_run_id": "workflow_foreign"},
            "job workflow must match its project and session",
        ),
        (
            {
                "project_id": "project",
                "session_id": "session",
                "workflow_run_id": "workflow_sibling",
            },
            "job workflow must match its project and session",
        ),
    ],
)
def test_job_insert_rejects_inconsistent_roots(
    current_case: ExecutionCase,
    overrides: dict[str, str],
    message: str,
) -> None:
    case = current_case
    fields: dict[str, str | None] = {
        "workspace_id": case.ids["workspace"],
        "project_id": None,
        "session_id": None,
        "workflow_run_id": None,
    }
    for key, value in overrides.items():
        fields[key] = case.ids[value]
    with pytest.raises(sqlite3.IntegrityError, match=message):
        _insert_job(case.path, "job-invalid-insert", **fields)


def test_job_insert_allows_workspace_project_and_workflow_only_roots(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _insert_job(case.path, "job-workspace-only-positive", workspace_id=case.ids["workspace"])
    _insert_job(
        case.path,
        "job-project-only-positive",
        workspace_id=case.ids["workspace"],
        project_id=case.ids["project"],
    )
    _insert_job(
        case.path,
        "job-workflow-only-positive",
        workspace_id=case.ids["workspace"],
        project_id=case.ids["project"],
        workflow_run_id=case.ids["workflow"],
    )
    _insert_job(
        case.path,
        "job-exact-session-workflow-positive",
        workspace_id=case.ids["workspace"],
        project_id=case.ids["project"],
        session_id=case.ids["session"],
        workflow_run_id=case.ids["workflow"],
    )


def test_archived_and_retained_empty_project_roots_remain_valid_history(
    execution_case: ExecutionCase,
) -> None:
    case = execution_case
    with case.database.session() as session:
        retained = Project(workspace_id=case.ids["workspace"], name="Retained empty project")
        session.add(retained)
        session.flush()
        retained_id = retained.id
        workspace = session.get(Workspace, case.ids["workspace"])
        assert workspace is not None
        workspace.state = "archived"
    _insert_job(
        case.path,
        "retained-archived-history",
        workspace_id=case.ids["workspace"],
        project_id=retained_id,
    )
    before = _snapshot(case.path)
    command.upgrade(case.config, TARGET_REVISION)
    assert _snapshot(case.path)["data"] == before["data"]
    _insert_job(
        case.path,
        "retained-archived-storage",
        workspace_id=case.ids["workspace"],
        project_id=retained_id,
    )


@pytest.mark.parametrize(
    ("job_key", "column", "target_key"),
    [
        ("job_workspace", "workspace_id", "workspace_other"),
        ("job_workspace", "project_id", "project"),
        ("job_project", "project_id", "null"),
        ("job_workspace", "session_id", "session"),
        ("job_exact", "session_id", "null"),
        ("job_workspace", "workflow_run_id", "workflow"),
        ("job_exact", "workflow_run_id", "null"),
    ],
)
def test_job_execution_roots_are_immutable(
    current_case: ExecutionCase,
    job_key: str,
    column: str,
    target_key: str,
) -> None:
    case = current_case
    target = None if target_key == "null" else case.ids[target_key]
    _assert_abort(
        case.path,
        f"UPDATE jobs SET {column} = :value WHERE id = :id",
        {"value": target, "id": case.ids[job_key]},
        "immutable",
    )


@pytest.mark.parametrize(
    ("table", "row_key", "model"),
    [
        ("workflow_runs", "workflow", WorkflowRun),
        ("generation_runs", "generation", GenerationRun),
        ("agent_runs", "agent", AgentRun),
    ],
)
def test_execution_session_roots_reject_direct_and_orm_reparenting(
    current_case: ExecutionCase,
    table: str,
    row_key: str,
    model: type[WorkflowRun] | type[GenerationRun] | type[AgentRun],
) -> None:
    case = current_case
    _assert_abort(
        case.path,
        f"UPDATE {table} SET session_id = :session_id WHERE id = :id",
        {"session_id": case.ids["session_sibling"], "id": case.ids[row_key]},
        "session is immutable",
    )
    with pytest.raises(IntegrityError, match="session is immutable"):
        with case.database.session() as session:
            row = session.get(model, case.ids[row_key])
            assert row is not None
            row.session_id = case.ids["session_sibling"]
            session.flush()


def test_generation_job_links_can_attach_replace_clear_and_reject_foreign_roots(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _execute(
        case.path,
        "UPDATE generation_runs SET job_id = :job WHERE id = :id",
        {"job": case.ids["job_workspace"], "id": case.ids["generation"]},
    )
    _execute(
        case.path,
        "UPDATE generation_runs SET job_id = :job WHERE id = :id",
        {"job": case.ids["job_sibling"], "id": case.ids["generation"]},
    )
    _execute(
        case.path,
        "UPDATE generation_runs SET job_id = NULL WHERE id = :id",
        {"id": case.ids["generation"]},
    )
    for key in ("job_other_project", "job_foreign"):
        _assert_abort(
            case.path,
            "UPDATE generation_runs SET job_id = :job WHERE id = :id",
            {"job": case.ids[key], "id": case.ids["generation"]},
            "generation job must share its session scope",
        )


def test_generation_insert_checks_job_scope_with_orm(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    with case.database.session() as session:
        accepted = GenerationRun(
            session_id=case.ids["session"],
            plan_revision_id=case.ids[f"revision-{case.ids['session']}"],
            sequence_number=2,
            job_id=case.ids["job_workspace"],
        )
        session.add(accepted)
        session.flush()

    with pytest.raises(IntegrityError, match="generation job must share its session scope"):
        with case.database.session() as session:
            rejected = GenerationRun(
                session_id=case.ids["session"],
                plan_revision_id=case.ids[f"revision-{case.ids['session']}"],
                sequence_number=3,
                job_id=case.ids["job_other_project"],
            )
            session.add(rejected)
            session.flush()


def test_agent_links_preserve_workspace_only_and_cross_project_source_rules(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _execute(
        case.path,
        "UPDATE agent_runs SET source_artifact_id = :artifact WHERE id = :id",
        {"artifact": case.ids["artifact_workspace"], "id": case.ids["agent"]},
    )
    _execute(
        case.path,
        "UPDATE agent_runs SET source_artifact_id = :artifact WHERE id = :id",
        {"artifact": case.ids["artifact_other_project"], "id": case.ids["agent"]},
    )
    _assert_abort(
        case.path,
        "UPDATE agent_runs SET source_artifact_id = :artifact WHERE id = :id",
        {"artifact": case.ids["artifact_foreign"], "id": case.ids["agent"]},
        "agent source artifact must share its session workspace",
    )
    _execute(
        case.path,
        "UPDATE agent_runs SET source_artifact_id = NULL WHERE id = :id",
        {"id": case.ids["agent"]},
    )

    _execute(
        case.path,
        "UPDATE agent_runs SET result_artifact_id = :artifact WHERE id = :id",
        {"artifact": case.ids["artifact_alt"], "id": case.ids["agent"]},
    )
    _execute(
        case.path,
        "UPDATE agent_runs SET result_artifact_id = :artifact WHERE id = :id",
        {"artifact": case.ids["artifact_workspace"], "id": case.ids["agent"]},
    )
    for key in ("artifact_other_project", "artifact_foreign"):
        _assert_abort(
            case.path,
            "UPDATE agent_runs SET result_artifact_id = :artifact WHERE id = :id",
            {"artifact": case.ids[key], "id": case.ids["agent"]},
            "agent result artifact must share its session scope",
        )
    _execute(
        case.path,
        "UPDATE agent_runs SET result_artifact_id = NULL WHERE id = :id",
        {"id": case.ids["agent"]},
    )

    _execute(
        case.path,
        "UPDATE agent_runs SET job_id = :job WHERE id = :id",
        {"job": case.ids["job_sibling"], "id": case.ids["agent"]},
    )
    for key in ("job_other_project", "job_foreign"):
        _assert_abort(
            case.path,
            "UPDATE agent_runs SET job_id = :job WHERE id = :id",
            {"job": case.ids[key], "id": case.ids["agent"]},
            "agent job must share its session scope",
        )
    _execute(
        case.path,
        "UPDATE agent_runs SET job_id = NULL WHERE id = :id",
        {"id": case.ids["agent"]},
    )


def test_agent_insert_checks_job_and_artifact_roots_with_orm(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    with case.database.session() as session:
        accepted = AgentRun(
            session_id=case.ids["session"],
            job_id=case.ids["job_sibling"],
            source_artifact_id=case.ids["artifact_other_project"],
            result_artifact_id=case.ids["artifact_workspace"],
        )
        session.add(accepted)
        session.flush()

    for values, message in (
        (
            {"source_artifact_id": case.ids["artifact_foreign"]},
            "agent source artifact must share its session workspace",
        ),
        (
            {"result_artifact_id": case.ids["artifact_other_project"]},
            "agent result artifact must share its session scope",
        ),
        (
            {"job_id": case.ids["job_foreign"]},
            "agent job must share its session scope",
        ),
    ):
        with pytest.raises(IntegrityError, match=message):
            with case.database.session() as session:
                rejected = AgentRun(session_id=case.ids["session"], **values)
                session.add(rejected)
                session.flush()


@pytest.mark.parametrize(
    ("field", "target_key", "message"),
    [
        ("session_id", "session_foreign", "usage session must match its stored scope"),
        ("project_id", "project_foreign", "usage project must share its workspace"),
        ("workflow_run_id", "workflow_foreign", "usage workflow must match its stored scope"),
        ("generation_run_id", "generation_foreign", "usage generation must match its stored scope"),
        ("agent_run_id", "agent_foreign", "usage agent must match its stored scope"),
        ("job_id", "job_foreign", "usage job must match its stored scope"),
        ("job_id", "job_other_project", "usage job must match its stored scope"),
        ("artifact_id", "artifact_foreign", "usage artifact must match its stored scope"),
        (
            "artifact_id",
            "artifact_other_project",
            "usage artifact must match its stored scope",
        ),
    ],
)
def test_usage_insert_rejects_foreign_or_sibling_project_references(
    current_case: ExecutionCase,
    field: str,
    target_key: str,
    message: str,
) -> None:
    case = current_case
    values: dict[str, str | None] = {
        "workspace_id": case.ids["workspace"],
        "project_id": case.ids["project"],
    }
    values[field] = case.ids[target_key]
    with pytest.raises(sqlite3.IntegrityError, match=message):
        _insert_usage(
            case.path,
            "usage-invalid-insert",
            **values,
        )


def test_usage_insert_accepts_project_collaboration_and_workspace_only_parents(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _insert_usage(
        case.path,
        "usage-project-collaboration",
        workspace_id=case.ids["workspace"],
        project_id=case.ids["project"],
        session_id=case.ids["session_sibling"],
        workflow_run_id=case.ids["workflow"],
        generation_run_id=case.ids["generation"],
        agent_run_id=case.ids["agent"],
        job_id=case.ids["job_workspace"],
        artifact_id=case.ids["artifact_workspace"],
    )
    _insert_usage(
        case.path,
        "usage-workspace-only",
        workspace_id=case.ids["workspace"],
        job_id=case.ids["job_workspace"],
        artifact_id=case.ids["artifact_workspace"],
    )


@pytest.mark.parametrize(
    ("field", "target_key"),
    [
        ("session_id", "session_foreign"),
        ("workflow_run_id", "workflow_foreign"),
        ("generation_run_id", "generation_foreign"),
        ("agent_run_id", "agent_foreign"),
        ("job_id", "job_other_project"),
        ("artifact_id", "artifact_other_project"),
    ],
)
def test_usage_reference_replacements_cannot_cross_the_retained_scope(
    current_case: ExecutionCase,
    field: str,
    target_key: str,
) -> None:
    case = current_case
    _assert_abort(
        case.path,
        f"UPDATE usage_events SET {field} = :value WHERE id = :id",
        {"value": case.ids[target_key], "id": case.ids["usage"]},
        "must match its stored scope",
    )


def test_usage_links_are_replaceable_and_roots_are_immutable(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _insert_usage(
        case.path,
        "usage-nullable-links",
        workspace_id=case.ids["workspace"],
        project_id=case.ids["project"],
    )
    for column, key in (
        ("session_id", "session_sibling"),
        ("workflow_run_id", "workflow"),
        ("generation_run_id", "generation"),
        ("agent_run_id", "agent"),
        ("job_id", "job_workspace"),
        ("artifact_id", "artifact_workspace"),
    ):
        _execute(
            case.path,
            f"UPDATE usage_events SET {column} = :value WHERE id = :id",
            {"value": case.ids[key], "id": "usage-nullable-links"},
        )
    _execute(
        case.path,
        "UPDATE usage_events SET session_id = NULL, workflow_run_id = NULL, "
        "generation_run_id = NULL, agent_run_id = NULL, job_id = NULL, artifact_id = NULL "
        "WHERE id = :id",
        {"id": "usage-nullable-links"},
    )

    _execute(
        case.path,
        "UPDATE usage_events SET session_id = :value WHERE id = :id",
        {"value": case.ids["session_sibling"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET workflow_run_id = :value WHERE id = :id",
        {"value": case.ids["workflow_sibling"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET generation_run_id = :value WHERE id = :id",
        {"value": case.ids["generation_sibling"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET agent_run_id = :value WHERE id = :id",
        {"value": case.ids["agent_sibling"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET job_id = :value WHERE id = :id",
        {"value": case.ids["job_workspace"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET job_id = :value WHERE id = :id",
        {"value": case.ids["job_exact"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET artifact_id = :value WHERE id = :id",
        {"value": case.ids["artifact_workspace"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET artifact_id = :value WHERE id = :id",
        {"value": case.ids["artifact_alt"], "id": case.ids["usage"]},
    )
    _execute(
        case.path,
        "UPDATE usage_events SET session_id = NULL, workflow_run_id = NULL, "
        "generation_run_id = NULL, agent_run_id = NULL, job_id = NULL, artifact_id = NULL "
        "WHERE id = :id",
        {"id": case.ids["usage"]},
    )

    for column, value in (
        ("workspace_id", case.ids["workspace_other"]),
        ("project_id", case.ids["project_sibling"]),
        ("project_id", None),
    ):
        _assert_abort(
            case.path,
            f"UPDATE usage_events SET {column} = :value WHERE id = :id",
            {"value": value, "id": case.ids["usage"]},
            "usage scope is immutable",
        )

    _insert_usage(
        case.path,
        "usage-null-project",
        workspace_id=case.ids["workspace"],
    )
    _assert_abort(
        case.path,
        "UPDATE usage_events SET project_id = :project WHERE id = :id",
        {"project": case.ids["project"], "id": "usage-null-project"},
        "usage scope is immutable",
    )


def test_parent_deletion_nulls_multiple_agent_and_usage_artifact_links(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _execute(
        case.path,
        "UPDATE agent_runs SET source_artifact_id = :artifact WHERE id = :id",
        {"artifact": case.ids["artifact"], "id": case.ids["agent"]},
    )
    _execute(case.path, "DELETE FROM artifacts WHERE id = :id", {"id": case.ids["artifact"]})

    with sqlite3.connect(case.path) as connection:
        assert connection.execute(
            "SELECT source_artifact_id, result_artifact_id FROM agent_runs WHERE id = ?",
            (case.ids["agent"],),
        ).fetchone() == (None, None)
        assert connection.execute(
            "SELECT artifact_id FROM usage_events WHERE id = ?", (case.ids["usage"],)
        ).fetchone() == (None,)


def test_parent_deletion_nulls_shared_generation_agent_and_usage_job_links(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _execute(
        case.path,
        "UPDATE generation_runs SET job_id = :job WHERE id = :id",
        {"job": case.ids["job_exact"], "id": case.ids["generation"]},
    )
    _execute(
        case.path,
        "UPDATE agent_runs SET job_id = :job WHERE id = :id",
        {"job": case.ids["job_exact"], "id": case.ids["agent"]},
    )
    _execute(case.path, "DELETE FROM jobs WHERE id = :id", {"id": case.ids["job_exact"]})

    with sqlite3.connect(case.path) as connection:
        assert connection.execute(
            "SELECT job_id FROM generation_runs WHERE id = ?", (case.ids["generation"],)
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT job_id FROM agent_runs WHERE id = ?", (case.ids["agent"],)
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT job_id FROM usage_events WHERE id = ?", (case.ids["usage"],)
        ).fetchone() == (None,)


def test_session_cascade_retains_receipt_scope_and_clears_links(
    current_case: ExecutionCase,
) -> None:
    case = current_case
    _execute(case.path, "DELETE FROM sessions WHERE id = :id", {"id": case.ids["session"]})

    with sqlite3.connect(case.path) as connection:
        row = connection.execute(
            "SELECT workspace_id, project_id, session_id, workflow_run_id, job_id, "
            "artifact_id, generation_run_id, agent_run_id FROM usage_events WHERE id = ?",
            (case.ids["usage"],),
        ).fetchone()
    assert row == (
        case.ids["workspace"],
        case.ids["project"],
        None,
        None,
        None,
        case.ids["artifact"],
        None,
        None,
    )


@pytest.mark.parametrize(
    ("corruption", "table_name"),
    [
        ("job_roots", "jobs"),
        ("project_missing_workspace", "projects"),
        ("workflow_missing_session", "workflow_runs"),
        ("generation_foreign_job", "generation_runs"),
        ("agent_foreign_source", "agent_runs"),
        ("agent_sibling_result", "agent_runs"),
        ("usage_foreign_job", "usage_events"),
        ("usage_missing_session", "usage_events"),
    ],
)
def test_preflight_rejects_corrupt_genuine_predecessor_rows_atomically(
    execution_case: ExecutionCase,
    corruption: str,
    table_name: str,
) -> None:
    case = execution_case
    if corruption == "job_roots":
        _insert_job(
            case.path,
            "job-corrupt-predecessor",
            workspace_id=case.ids["workspace"],
            project_id=case.ids["project"],
            session_id=case.ids["session_foreign"],
        )
    elif corruption == "project_missing_workspace":
        with _connect(case.path, foreign_keys=False) as connection:
            connection.execute(
                "INSERT INTO projects "
                "(id, workspace_id, source_session_id, name, created_by_user_id, "
                "revision, created_at, updated_at) "
                "VALUES ('project-missing-workspace', 'missing-workspace', NULL, "
                "'Malformed predecessor root', NULL, 1, :now, :now)",
                {"now": TIMESTAMP},
            )
    elif corruption == "workflow_missing_session":
        with _connect(case.path, foreign_keys=False) as connection:
            connection.execute(
                "INSERT INTO workflow_runs "
                "(id, session_id, preset, status, settings_json, created_at, updated_at) "
                "VALUES ('workflow-missing-session', 'missing-session', 'synthetic', "
                "'ready', '{}', :now, :now)",
                {"now": TIMESTAMP},
            )
    elif corruption == "generation_foreign_job":
        _execute(
            case.path,
            "UPDATE generation_runs SET job_id = :job WHERE id = :id",
            {"job": case.ids["job_foreign"], "id": case.ids["generation"]},
        )
    elif corruption == "agent_foreign_source":
        _execute(
            case.path,
            "UPDATE agent_runs SET source_artifact_id = :artifact WHERE id = :id",
            {"artifact": case.ids["artifact_foreign"], "id": case.ids["agent"]},
        )
    elif corruption == "agent_sibling_result":
        _execute(
            case.path,
            "UPDATE agent_runs SET result_artifact_id = :artifact WHERE id = :id",
            {"artifact": case.ids["artifact_other_project"], "id": case.ids["agent"]},
        )
    elif corruption == "usage_foreign_job":
        _execute(
            case.path,
            "UPDATE usage_events SET job_id = :job WHERE id = :id",
            {"job": case.ids["job_foreign"], "id": case.ids["usage"]},
        )
    else:
        with _connect(case.path, foreign_keys=False) as connection:
            connection.execute(
                "UPDATE usage_events SET session_id = 'missing-session' WHERE id = ?",
                (case.ids["usage"],),
            )

    before = _snapshot(case.path)
    with pytest.raises(RuntimeError, match=f"{table_name} row"):
        command.upgrade(case.config, TARGET_REVISION)
    assert _snapshot(case.path) == before
    assert _version(case.path) == PREDECESSOR_REVISION


def _replace_usage_table_with_malformed_shape(path: Path, kind: str) -> None:
    columns = [
        "id VARCHAR(36) PRIMARY KEY NOT NULL",
        "workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT",
        "project_id VARCHAR(36) REFERENCES projects(id) ON DELETE RESTRICT",
        "session_id VARCHAR(36) REFERENCES sessions(id) ON DELETE SET NULL",
        "workflow_run_id VARCHAR(36) REFERENCES workflow_runs(id) ON DELETE SET NULL",
        "job_id VARCHAR(36) REFERENCES jobs(id) ON DELETE SET NULL",
        "artifact_id VARCHAR(36) REFERENCES artifacts(id) ON DELETE SET NULL",
        "generation_run_id VARCHAR(36) REFERENCES generation_runs(id) ON DELETE SET NULL",
        "agent_run_id VARCHAR(36) REFERENCES agent_runs(id) ON DELETE SET NULL",
        "provider_key VARCHAR(80) NOT NULL",
        "model_id VARCHAR(255) NOT NULL",
    ]
    if kind == "missing-column":
        columns = [column for column in columns if not column.startswith("agent_run_id ")]
    elif kind == "wrong-delete-action":
        columns = [
            column.replace(
                "session_id VARCHAR(36) REFERENCES sessions(id) ON DELETE SET NULL",
                "session_id VARCHAR(36) REFERENCES sessions(id) ON DELETE CASCADE",
            )
            for column in columns
        ]
    elif kind == "unexpected-default":
        columns = [
            column.replace(
                "workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT",
                "workspace_id VARCHAR(36) NOT NULL DEFAULT 'unexpected' "
                "REFERENCES workspaces(id) ON DELETE RESTRICT",
            )
            for column in columns
        ]
    with _connect(path, foreign_keys=False) as connection:
        connection.execute("DROP TABLE usage_events")
        connection.execute(f"CREATE TABLE usage_events ({', '.join(columns)})")


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing-column", "a required column is missing"),
        ("wrong-delete-action", "ownership foreign key has an unexpected shape"),
        ("unexpected-default", "ownership foreign key has an unexpected shape"),
    ],
)
def test_preflight_rejects_malformed_target_schema_before_ddl(
    execution_case: ExecutionCase,
    kind: str,
    message: str,
) -> None:
    case = execution_case
    _replace_usage_table_with_malformed_shape(case.path, kind)
    before = _snapshot(case.path)

    with pytest.raises(RuntimeError, match=message):
        command.upgrade(case.config, TARGET_REVISION)

    assert _snapshot(case.path) == before
    assert _version(case.path) == PREDECESSOR_REVISION


@pytest.mark.parametrize("altered", [False, True])
@pytest.mark.parametrize("name,table", PARENT_TRIGGERS)
def test_missing_or_altered_parent_trigger_fails_before_ddl(
    execution_case: ExecutionCase,
    altered: bool,
    name: str,
    table: str,
) -> None:
    case = execution_case
    with _connect(case.path) as connection:
        connection.execute(f'DROP TRIGGER "{name}"')
        if altered:
            connection.execute(
                f'CREATE TRIGGER "{name}" BEFORE UPDATE ON "{table}" '
                "BEGIN SELECT RAISE(ABORT, 'altered trigger'); END"
            )
    before = _snapshot(case.path)

    with pytest.raises(RuntimeError, match="required parent trigger is missing or altered"):
        command.upgrade(case.config, TARGET_REVISION)

    assert _snapshot(case.path) == before
    assert _version(case.path) == PREDECESSOR_REVISION


def test_case_insensitive_owned_trigger_name_collision_fails_before_ddl(
    execution_case: ExecutionCase,
) -> None:
    case = execution_case
    _execute(
        case.path,
        "CREATE TRIGGER PANDRATOR_EXECUTION_SCOPE_JOBS_INSERT "
        "AFTER UPDATE ON jobs BEGIN SELECT NEW.id; END",
    )
    before = _snapshot(case.path)

    with pytest.raises(RuntimeError, match="an owned trigger name is already in use"):
        command.upgrade(case.config, TARGET_REVISION)

    assert _snapshot(case.path) == before
    assert _version(case.path) == PREDECESSOR_REVISION


def test_mid_create_failure_rolls_back_every_trigger_and_revision(
    execution_case: ExecutionCase,
) -> None:
    case = execution_case
    before = _snapshot(case.path)
    create_count = 0

    def fail_during_trigger_create(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        nonlocal create_count
        if statement.startswith("CREATE TRIGGER pandrator_execution_scope_"):
            create_count += 1
            if create_count == 5:
                raise RuntimeError("Injected execution trigger DDL failure")

    sqlalchemy_event.listen(Engine, "before_cursor_execute", fail_during_trigger_create)
    try:
        with pytest.raises(RuntimeError, match="Injected execution trigger DDL failure"):
            command.upgrade(case.config, TARGET_REVISION)
    finally:
        sqlalchemy_event.remove(Engine, "before_cursor_execute", fail_during_trigger_create)

    assert create_count == 5
    assert _snapshot(case.path) == before
    assert _version(case.path) == PREDECESSOR_REVISION


@pytest.mark.parametrize("altered", [False, True])
def test_downgrade_validates_all_owned_triggers_before_dropping_any(
    current_case: ExecutionCase,
    altered: bool,
) -> None:
    case = current_case
    owned_name, owned_table = TRIGGERS[5]
    with _connect(case.path) as connection:
        connection.execute(f'DROP TRIGGER "{owned_name}"')
        if altered:
            connection.execute(
                f'CREATE TRIGGER "{owned_name}" BEFORE UPDATE ON "{owned_table}" '
                "BEGIN SELECT RAISE(ABORT, 'altered owned trigger'); END"
            )
    before = _snapshot(case.path)

    with pytest.raises(RuntimeError, match="owned trigger is missing or altered"):
        command.downgrade(case.config, PREDECESSOR_REVISION)

    assert _snapshot(case.path) == before
    assert _version(case.path) == TARGET_REVISION
