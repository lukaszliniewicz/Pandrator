from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import AppSetting, Job, JobEvent, SessionRecord, WorkflowRun
from pandrator.web.projects.identity import create_session_project


@pytest.fixture
def job_queue(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    upgrade_database(path)
    database = Database(path)
    queue = JobQueue(database)

    with database.session() as session:
        personal = initial_personal_workspace(session)
        first_workspace = Workspace(kind="team", name="First workspace")
        second_workspace = Workspace(kind="team", name="Second workspace")
        session.add_all([first_workspace, second_workspace])
        session.flush()

        first_record = SessionRecord(
            name="First source",
            storage_key="first-source",
            workflow_kind="subtitles",
            source_language="en",
        )
        first_project = create_session_project(
            session, first_record, workspace_id=first_workspace.id
        )
        second_record = SessionRecord(
            name="Second source",
            storage_key="second-source",
            workflow_kind="subtitles",
            source_language="en",
        )
        second_project = create_session_project(
            session, second_record, workspace_id=second_workspace.id
        )
        workflow = WorkflowRun(session_id=first_record.id, preset="admission-test")
        session.add(workflow)
        session.flush([workflow])

        ids = {
            "personal_workspace_id": personal.id,
            "first_workspace_id": first_workspace.id,
            "second_workspace_id": second_workspace.id,
            "first_session_id": first_record.id,
            "second_session_id": second_record.id,
            "first_project_id": first_project.id,
            "second_project_id": second_project.id,
            "workflow_run_id": workflow.id,
        }

    yield database, queue, ids
    database.dispose()


def _event_types(database: Database, job_id: str) -> list[str]:
    with database.session() as session:
        return list(
            session.scalars(
                select(JobEvent.event_type).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            ).all()
        )


def test_typed_session_project_workflow_and_workspace_scopes_are_persisted(job_queue):
    database, queue, ids = job_queue

    by_session = queue.enqueue("test.session", session_id=ids["first_session_id"])
    by_session_project = queue.enqueue(
        "test.session_project",
        session_id=ids["first_session_id"],
        project_id=ids["first_project_id"],
    )
    by_project = queue.enqueue("test.project", project_id=ids["second_project_id"])
    by_workflow = queue.enqueue("test.workflow", workflow_run_id=ids["workflow_run_id"])
    by_session_workflow = queue.enqueue(
        "test.session_workflow",
        session_id=ids["first_session_id"],
        workflow_run_id=ids["workflow_run_id"],
    )
    by_workspace = queue.enqueue("test.workspace", workspace_id=ids["second_workspace_id"])

    assert (by_session.workspace_id, by_session.project_id) == (
        ids["first_workspace_id"],
        ids["first_project_id"],
    )
    assert (by_session_project.workspace_id, by_session_project.project_id) == (
        ids["first_workspace_id"],
        ids["first_project_id"],
    )
    assert (by_project.workspace_id, by_project.project_id) == (
        ids["second_workspace_id"],
        ids["second_project_id"],
    )
    assert (by_workflow.workspace_id, by_workflow.project_id) == (
        ids["first_workspace_id"],
        ids["first_project_id"],
    )
    assert by_workflow.session_id is None
    assert (by_session_workflow.workspace_id, by_session_workflow.project_id) == (
        ids["first_workspace_id"],
        ids["first_project_id"],
    )
    assert by_session_workflow.session_id == ids["first_session_id"]
    assert (by_workspace.workspace_id, by_workspace.project_id) == (
        ids["second_workspace_id"],
        None,
    )
    assert _event_types(database, by_session.id) == ["job.queued"]


def test_omitted_context_uses_personal_scope_and_ignores_payload_scope_hints(job_queue):
    _database, queue, ids = job_queue

    job = queue.enqueue(
        "test.payload_hints",
        {
            "workspace_id": ids["first_workspace_id"],
            "project_id": ids["first_project_id"],
            "user_id": "untrusted-user",
        },
    )

    assert job.workspace_id == ids["personal_workspace_id"]
    assert job.project_id is None
    assert job.payload_json["workspace_id"] == ids["first_workspace_id"]
    assert job.payload_json["project_id"] == ids["first_project_id"]


@pytest.mark.parametrize("mismatch", ["project", "workspace", "workflow"])
def test_conflicting_typed_context_is_rejected_without_job_or_event(job_queue, mismatch):
    database, queue, ids = job_queue
    kwargs = {"session_id": ids["first_session_id"]}
    if mismatch == "project":
        kwargs["project_id"] = ids["second_project_id"]
    elif mismatch == "workspace":
        kwargs["workspace_id"] = ids["second_workspace_id"]
    else:
        kwargs = {
            "session_id": ids["second_session_id"],
            "workflow_run_id": ids["workflow_run_id"],
        }

    with pytest.raises(ValueError):
        queue.enqueue("test.conflict", **kwargs)

    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "test.conflict")) is None
        assert (
            session.scalar(select(JobEvent.id).where(JobEvent.event_type == "job.queued")) is None
        )


def test_missing_or_archived_explicit_scopes_are_rejected(job_queue):
    database, queue, ids = job_queue

    with pytest.raises(ValueError):
        queue.enqueue("test.missing_workspace", workspace_id="missing-workspace")
    with pytest.raises(ValueError):
        queue.enqueue("test.missing_project", project_id="missing-project")

    with database.session() as session:
        workspace = session.get(Workspace, ids["first_workspace_id"])
        workspace.state = "archived"

    with pytest.raises(ValueError):
        queue.enqueue("test.archived_workspace", workspace_id=ids["first_workspace_id"])


def test_session_writability_guard_still_rejects_trashed_sessions(job_queue):
    database, queue, ids = job_queue
    with database.session() as session:
        record = session.get(SessionRecord, ids["first_session_id"])
        assert record is not None
        record.trashed_at = datetime.now(UTC)

    with pytest.raises(ValueError, match="Restore the session"):
        queue.enqueue("test.trashed_session", session_id=ids["first_session_id"])

    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "test.trashed_session")) is None


def test_cached_workspace_is_revalidated_after_queue_gets_writer_lock(job_queue):
    database, queue, ids = job_queue
    stale_session = Session(bind=database.engine, expire_on_commit=False, future=True)
    try:
        cached_workspace = stale_session.get(Workspace, ids["first_workspace_id"])
        assert cached_workspace is not None
        assert cached_workspace.state == "active"

        with database.session() as writer:
            current_workspace = writer.get(Workspace, ids["first_workspace_id"])
            assert current_workspace is not None
            current_workspace.state = "archived"

        with pytest.raises(ValueError, match="inactive workspace"):
            queue.enqueue_in_session(
                stale_session,
                "test.late_writer",
                workspace_id=ids["first_workspace_id"],
            )
    finally:
        stale_session.close()

    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "test.late_writer")) is None


def test_rejected_enqueue_rolls_back_caller_domain_rows_job_and_event(job_queue):
    database, queue, _ids = job_queue
    with database.session() as session:
        session.add(AppSetting(key="admission-domain-row", value_json={"written": True}))
        with pytest.raises(ValueError):
            queue.enqueue_in_session(
                session,
                "test.caller_rollback",
                project_id="missing-project",
            )

    with database.session() as session:
        assert session.get(AppSetting, "admission-domain-row") is None
        assert session.scalar(select(Job.id).where(Job.kind == "test.caller_rollback")) is None
        assert (
            session.scalar(select(JobEvent.id).where(JobEvent.event_type == "job.queued")) is None
        )


def test_manager_fence_runs_before_queued_event_and_rolls_back_caller_rows(job_queue, monkeypatch):
    database, queue, _ids = job_queue
    calls = []

    def reject_app_job(path):
        calls.append(path)
        raise RuntimeError("manager maintenance is active")

    monkeypatch.setattr("pandrator.web.jobs.ensure_app_job_allowed", reject_app_job)

    with database.session() as session:
        session.add(AppSetting(key="manager-domain-row", value_json={"written": True}))
        with pytest.raises(RuntimeError, match="manager maintenance is active"):
            queue.enqueue_in_session(session, "test.manager_rejected")

    assert calls == [database.path]
    with database.session() as session:
        assert session.get(AppSetting, "manager-domain-row") is None
        assert session.scalar(select(Job.id).where(Job.kind == "test.manager_rejected")) is None
        assert (
            session.scalar(select(JobEvent.id).where(JobEvent.event_type == "job.queued")) is None
        )
