"""Recorded costs keep validated ownership independently of session lifetime."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.agentic_runs import AgenticRunStore
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    AgentRun,
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    Project,
    ResearchCacheEntry,
    SessionRecord,
    UsageEvent,
    WorkflowRun,
    Workspace,
    utcnow,
)
from pandrator.web.session_purge import SessionPurgeService
from pandrator.web.sessions import SessionService
from pandrator.web.usage import usage_summary
from pandrator.web.usage_identity import (
    UsageOwnership,
    bind_usage_ownership,
    usage_creation_ownership,
)
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class UsageCase:
    database: Database
    paths: DataPaths
    workspaces: dict[str, str]
    sessions: dict[str, SessionRecord]
    handlers: WorkflowHandlers

    def artifact(self, name: str, *, bound: bool = True) -> Artifact:
        path = self.paths.uploads / f"{name}-{threading.get_ident()}.txt"
        path.write_text("Synthetic usage ownership input.\n")
        return ArtifactService(self.database, self.paths).register(
            path,
            kind="text",
            workspace_id=self.workspaces["beta" if name == "beta" else "alpha"],
            session_id=self.sessions[name].id if bound else None,
        )

    def agent(self, name: str = "alpha") -> AgentRun:
        artifact = self.artifact(name)
        with self.database.immediate_session() as session:
            run = AgentRun(session_id=self.sessions[name].id, source_artifact_id=artifact.id)
            session.add(run)
            session.flush()
            session.expunge(run)
            return run

    def record(self, name: str = "alpha", **kwargs: Any) -> None:
        self.handlers._record_usage(
            self.sessions[name].id,
            "translation",
            {"model_name": "synthetic/model"},
            SimpleNamespace(
                cost=0.35,
                cost_sources=("synthetic",),
                response_count=1,
                usage={"prompt_tokens": 20, "completion_tokens": 8},
            ),
            **kwargs,
        )

    def rows(self) -> list[UsageEvent]:
        with self.database.snapshot_session() as session:
            rows = list(session.scalars(select(UsageEvent).order_by(UsageEvent.id)))
            for row in rows:
                session.expunge(row)
            return rows


def _use_predecessor(database: Database, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database.path))
    command.downgrade(config, revision)


@pytest.fixture
def usage_case(tmp_path: Path) -> Iterator[UsageCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        workspaces["personal"] = initial_personal_workspace(session).id
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
    service = SessionService(database)
    sessions = {
        name: service.create(name, workspace_id=workspaces[owner])
        for name, owner in (("alpha", "alpha"), ("alpha_second", "alpha"), ("beta", "beta"))
    }
    try:
        yield UsageCase(database, paths, workspaces, sessions, WorkflowHandlers(database, paths))
    finally:
        database.dispose()


@pytest.mark.parametrize("name", ("alpha", "alpha_second", "beta"))
def test_general_accounting_uses_actual_session_roots(usage_case: UsageCase, name: str) -> None:
    case = usage_case
    case.record(name)
    (event,) = case.rows()
    record = case.sessions[name]
    assert event.project_id == record.project_id
    assert event.workspace_id == case.workspaces["beta" if name == "beta" else "alpha"]
    assert event.session_id == record.id and event.cost_usd == 0.35
    assert usage_summary([event])["input_tokens"] == 20


def test_sessionless_preview_accounting_uses_its_job_and_artifact(
    usage_case: UsageCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = usage_case
    artifact = case.artifact("beta", bound=False)
    job = JobQueue(case.database).enqueue("tts.preview", {}, workspace_id=artifact.workspace_id)
    monkeypatch.setattr(
        "pandrator.logic.tts_handler.estimate_tts_usage",
        lambda *_args: {"provider": "synthetic", "model": "test", "cost_usd": 0.02},
    )
    case.handlers._record_tts_usage(
        "", {}, "Synthetic text", 1000, job_id=job.id, artifact_id=artifact.id
    )
    (event,) = case.rows()
    assert (event.workspace_id, event.project_id) == (case.workspaces["beta"], None)
    assert event.session_id is None and event.cost_usd == 0.02
    assert (event.job_id, event.artifact_id) == (job.id, artifact.id)


@pytest.mark.parametrize(
    "reference",
    ("session_id", "workflow_run_id", "job_id", "artifact_id", "generation_run_id", "agent_run_id"),
)
def test_each_typed_root_supplies_the_same_owner(usage_case: UsageCase, reference: str) -> None:
    case = usage_case
    record = case.sessions["alpha"]
    artifact = case.artifact("alpha")
    reusable = case.artifact("alpha_second", bound=False)
    job = JobQueue(case.database).enqueue("synthetic", {}, session_id=record.id)
    with case.database.immediate_session() as session:
        workflow = WorkflowRun(session_id=record.id, preset="synthetic")
        plan = GenerationPlan(session_id=record.id)
        session.add_all([workflow, plan])
        session.flush()
        revision = GenerationPlanRevision(plan_id=plan.id, revision_number=1, content_hash="f" * 64)
        session.add(revision)
        session.flush()
        generation = GenerationRun(
            session_id=record.id, plan_revision_id=revision.id, job_id=job.id
        )
        agent = AgentRun(session_id=record.id, job_id=job.id, source_artifact_id=artifact.id)
        session.add_all([generation, agent])
        session.flush()
        ids = dict(
            session_id=record.id,
            workflow_run_id=workflow.id,
            job_id=job.id,
            artifact_id=artifact.id,
            generation_run_id=generation.id,
            agent_run_id=agent.id,
        )
        owner = usage_creation_ownership(session, **{reference: ids[reference]})
        assert owner == UsageOwnership(case.workspaces["alpha"], record.project_id)
        assert usage_creation_ownership(session).workspace_id == case.workspaces["personal"]
        # A reusable workspace resource may support work in one of its projects.
        assert (
            usage_creation_ownership(session, session_id=record.id, artifact_id=reusable.id)
            == owner
        )


@pytest.mark.parametrize("foreign", ("alpha_second", "beta"))
def test_conflicting_inputs_do_not_create_or_overwrite_a_receipt(
    usage_case: UsageCase, foreign: str
) -> None:
    case = usage_case
    agent = case.agent()
    case.record(agent_run_id=agent.id, request_key="same-unit")
    before = case.rows()[0]
    foreign_artifact = case.artifact(foreign)
    with pytest.raises(ValueError, match="conflicting"):
        case.record(agent_run_id=agent.id, request_key="same-unit", artifact_id=foreign_artifact.id)
    (after,) = case.rows()
    assert (after.id, after.cost_usd, after.raw_usage_json, after.artifact_id) == (
        before.id,
        before.cost_usd,
        before.raw_usage_json,
        before.artifact_id,
    )
    assert (after.workspace_id, after.project_id) == (before.workspace_id, before.project_id)


def test_same_agent_request_replay_is_atomic_under_two_writers(usage_case: UsageCase) -> None:
    case = usage_case
    agent = case.agent()
    barrier = threading.Barrier(2)

    def record() -> None:
        barrier.wait(timeout=10)
        case.record(agent_run_id=agent.id, request_key="repeated-unit")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(record) for _ in range(2)]
        for result in results:
            result.result(timeout=30)
    case.record(agent_run_id=agent.id, request_key="repeated-unit")
    (event,) = case.rows()
    assert event.request_key == "repeated-unit" and event.cost_usd == 0.35
    assert event.workspace_id == case.workspaces["alpha"]


def test_recording_completed_work_is_allowed_after_archive(usage_case: UsageCase) -> None:
    case = usage_case
    with case.database.immediate_session() as session:
        session.execute(
            update(Workspace)
            .where(Workspace.id == case.workspaces["alpha"])
            .values(state="archived")
        )
    case.record()
    (event,) = case.rows()
    assert (event.workspace_id, event.project_id) == (
        case.workspaces["alpha"],
        case.sessions["alpha"].project_id,
    )


def test_revalidation_uses_current_sql_instead_of_preloaded_orm_roots(
    usage_case: UsageCase,
) -> None:
    case = usage_case
    artifact = case.artifact("alpha")
    # Stored artifact-root corruption is only representable before 0077.
    _use_predecessor(case.database, "0076_project_graph_consistency")
    with case.database.immediate_session() as session:
        cached = session.get(Artifact, artifact.id)
        assert cached is not None
        session.execute(
            update(Artifact)
            .where(Artifact.id == artifact.id)
            .values(workspace_id=case.workspaces["beta"])
            .execution_options(synchronize_session=False)
        )
        assert cached.workspace_id == case.workspaces["alpha"]
        with pytest.raises(ValueError, match="conflicting"):
            usage_creation_ownership(session, artifact_id=artifact.id)
        session.rollback()
    assert not case.rows()


def test_stored_scope_cannot_be_repaired_or_granted_by_raw_usage_json(
    usage_case: UsageCase,
) -> None:
    case = usage_case
    # Persisted NULL ownership is only representable in the real predecessor.
    # Keep the defensive no-repair check without weakening current constraints.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0074_actor_attribution")
    case.record()
    event_id = case.rows()[0].id
    with case.database.immediate_session() as session:
        event = session.get(UsageEvent, event_id)
        assert event is not None
        event.workspace_id = case.workspaces["beta"]
        with pytest.raises(ValueError, match="conflicting"):
            bind_usage_ownership(session, event)
        session.rollback()
    with case.database.immediate_session() as session:
        unowned = UsageEvent(
            provider_key="test",
            model_id="test",
            raw_usage_json={"workspace_id": case.workspaces["beta"]},
        )
        session.add(unowned)
        session.flush()
        with pytest.raises(ValueError, match="no stored ownership"):
            bind_usage_ownership(session, unowned)
        session.rollback()
    with case.database.immediate_session() as session:
        new = UsageEvent(
            provider_key="test",
            model_id="test",
            raw_usage_json={"workspace_id": case.workspaces["beta"]},
        )
        assert bind_usage_ownership(session, new) == UsageOwnership(
            case.workspaces["personal"], None
        )
        with pytest.raises(ValueError, match="missing generation"):
            usage_creation_ownership(session, generation_run_id="missing-generation")
    assert len(case.rows()) == 1


def test_agent_completion_only_attaches_same_owner_result(usage_case: UsageCase) -> None:
    case = usage_case
    agent = case.agent()
    case.record(agent_run_id=agent.id, request_key="unit")
    foreign = case.artifact("beta")
    store = AgenticRunStore(case.database)
    with pytest.raises(ValueError, match="conflicting"):
        store.finish(agent.id, artifact_id=foreign.id)
    (event,) = case.rows()
    assert event.artifact_id is None
    result = case.artifact("alpha_second", bound=False)
    # Workspace-only library results still belong to the execution project ledger.
    store.finish(agent.id, artifact_id=result.id)
    (event,) = case.rows()
    assert event.artifact_id == result.id and event.project_id == case.sessions["alpha"].project_id
    # Preserve the defensive legacy-row check after the current-schema positives.
    _use_predecessor(case.database, "0078_library_scope_consistency")
    with case.database.immediate_session() as session:
        session.execute(
            update(UsageEvent)
            .where(UsageEvent.id == event.id)
            .values(workspace_id=case.workspaces["beta"])
        )
    with pytest.raises(ValueError, match="another owner"):
        store.finish(agent.id, artifact_id=result.id)


@pytest.mark.parametrize("source_project", ("alpha_second", "beta"))
def test_reused_source_is_a_dependency_and_not_the_billing_project(
    usage_case: UsageCase, source_project: str
) -> None:
    case = usage_case
    source = case.artifact(source_project)
    if source_project == "beta":
        # Current constraints reject this malformed cross-workspace run at insertion.
        _use_predecessor(case.database, "0078_library_scope_consistency")
    with case.database.immediate_session() as session:
        run = AgentRun(session_id=case.sessions["alpha"].id, source_artifact_id=source.id)
        session.add(run)
        session.flush()
        run_id = run.id
    if source_project == "beta":
        with pytest.raises(ValueError, match="conflicting"):
            case.record(agent_run_id=run_id, request_key="source-unit")
        assert not case.rows()
    else:
        case.record(agent_run_id=run_id, request_key="source-unit")
        (receipt,) = case.rows()
        assert receipt.project_id == case.sessions["alpha"].project_id
        assert receipt.project_id != source.project_id


def test_actual_hard_purge_retains_cost_owner_while_nulling_deleted_links(
    usage_case: UsageCase,
) -> None:
    case = usage_case
    record = case.sessions["alpha"]
    folder = case.paths.sessions / record.storage_key
    folder.mkdir(exist_ok=True)
    path = folder / "costed-input.txt"
    path.write_text("Synthetic costed input")
    artifact = ArtifactService(case.database, case.paths).register(
        path, kind="text", session_id=record.id
    )
    with case.database.immediate_session() as session:
        workflow = WorkflowRun(session_id=record.id, preset="synthetic", status="completed")
        session.add(workflow)
        session.flush()
        workflow_id = workflow.id
    job = JobQueue(case.database).enqueue(
        "synthetic", {}, session_id=record.id, workflow_run_id=workflow_id
    )
    with case.database.immediate_session() as session:
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(plan_id=plan.id, revision_number=1, content_hash="a" * 64)
        session.add(revision)
        session.flush()
        generation = GenerationRun(
            session_id=record.id, plan_revision_id=revision.id, job_id=job.id, status="completed"
        )
        agent = AgentRun(
            session_id=record.id, source_artifact_id=artifact.id, job_id=job.id, status="completed"
        )
        session.add_all([generation, agent])
        session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(status="completed")
        )
        session.flush()
        generation_id, agent_id = generation.id, agent.id
    case.record(
        job_id=job.id,
        artifact_id=artifact.id,
        generation_run_id=generation_id,
        agent_run_id=agent_id,
        request_key="paid-unit",
    )
    event = case.rows()[0]
    with case.database.immediate_session() as session:
        stored = session.get_one(UsageEvent, event.id)
        stored.workflow_run_id = workflow_id
        bind_usage_ownership(session, stored)
        session.add(
            ResearchCacheEntry(
                cache_key="f" * 64,
                workspace_id=event.workspace_id,
                project_id=event.project_id,
                provider="test",
                operation="search",
                request_json={"private": "query"},
                response_json={},
                expires_at=utcnow() + timedelta(days=1),
            )
        )
    trashed = SessionService(case.database).trash(record.id, revision=record.revision)
    assert trashed is not None
    purge = SessionPurgeService(case.database, case.paths)
    preview = purge.preview(record.id)
    assert preview["can_purge"], preview
    assert purge.purge(record.id, preview["revision"], preview["impact_token"]) == {
        "state": "complete"
    }
    (retained,) = case.rows()
    assert retained.workspace_id is not None and retained.project_id is not None
    assert (
        retained.id,
        retained.cost_usd,
        retained.raw_usage_json,
        retained.workspace_id,
        retained.project_id,
    ) == (event.id, event.cost_usd, event.raw_usage_json, event.workspace_id, event.project_id)
    assert retained.session_id is None and retained.job_id is None and retained.artifact_id is None
    assert retained.workflow_run_id is None and retained.generation_run_id is None
    assert retained.agent_run_id is None and retained.request_key == "paid-unit"
    assert not path.exists()
    with case.database.snapshot_session() as session:
        assert session.get(SessionRecord, record.id) is None
        root = session.get(Project, record.project_id)
        assert root is not None and root.source_session_id is None
        assert not session.scalars(select(ResearchCacheEntry)).all()
        assert bind_usage_ownership(
            session, session.get_one(UsageEvent, retained.id)
        ) == UsageOwnership(retained.workspace_id, retained.project_id)
    with pytest.raises(IntegrityError):
        with case.database.immediate_session() as session:
            session.delete(session.get_one(Project, record.project_id))
