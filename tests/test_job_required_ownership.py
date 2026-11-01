"""Admission must serialize ownership reads without inserting an unscoped row."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.engine.default import DefaultExecutionContext

from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, JobActorRecord, JobEvent, SessionRecord
from pandrator.web.projects.models import Project
from pandrator.web.sessions import SessionService


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "required-job-ownership.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        yield database
    finally:
        database.dispose()


def test_first_job_insert_already_contains_its_owner(database: Database) -> None:
    source = SessionService(database).create("Scoped queue source")
    inserts: list[dict[str, Any]] = []

    def capture_insert(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: DefaultExecutionContext,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO JOBS "):
            inserts.extend(dict(parameters) for parameters in context.compiled_parameters)

    event.listen(database.engine, "before_cursor_execute", capture_insert)
    try:
        with execution_actor_scope(HostActor("pandrator-cli")):
            job = JobQueue(database).enqueue("synthetic.owned", session_id=source.id)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_insert)

    assert len(inserts) == 1
    assert inserts[0]["workspace_id"] == job.workspace_id
    assert inserts[0]["workspace_id"]
    assert inserts[0]["project_id"] == source.project_id
    with database.session() as session:
        actor = session.get(JobActorRecord, job.id)
        assert actor is not None and actor.context_json["source"] == "host"
        assert list(session.scalars(select(JobEvent.event_type))) == ["job.queued"]


def test_admission_resolves_owner_after_waiting_for_another_writer(database: Database) -> None:
    source = SessionService(database).create("Changing queue source")
    with database.session() as session:
        project = session.get(Project, source.project_id)
        assert project is not None
        original_workspace_id = project.workspace_id

    first_queue_write = Event()

    def observe_queue_write(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: DefaultExecutionContext,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("UPDATE JOBS ", "INSERT INTO JOBS ")):
            first_queue_write.set()

    def admit() -> Job:
        with execution_actor_scope(HostActor("pandrator-cli")):
            with database.session() as session:
                cached_project = session.get(Project, source.project_id)
                assert cached_project is not None
                assert cached_project.workspace_id == original_workspace_id
                cached_workspace = session.get(Workspace, original_workspace_id)
                assert cached_workspace is not None and cached_workspace.state == "active"
                job = JobQueue(database).enqueue_in_session(
                    session, "synthetic.waited", session_id=source.id
                )
                session.expunge(job)
                return job

    event.listen(database.engine, "before_cursor_execute", observe_queue_write)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with database.immediate_session() as blocker:
                result = executor.submit(admit)
                assert first_queue_write.wait(5), "Queue did not reach its writer fence."
                blocker.execute(
                    update(Workspace)
                    .where(Workspace.id == original_workspace_id)
                    .values(state="archived")
                )
            with pytest.raises(ValueError, match="inactive workspace"):
                result.result(timeout=5)
    finally:
        event.remove(database.engine, "before_cursor_execute", observe_queue_write)

    with database.session() as session:
        assert session.scalar(select(Job.id)) is None
        assert session.scalar(select(JobActorRecord.job_id)) is None
        assert session.scalar(select(JobEvent.id)) is None


def test_rejected_ownership_still_rolls_back_pending_caller_changes(database: Database) -> None:
    source = SessionService(database).create("Retained source name")
    with database.session() as session:
        record = session.get(SessionRecord, source.id)
        assert record is not None
        record.name = "Uncommitted caller change"
        with execution_actor_scope(HostActor("pandrator-cli")):
            with pytest.raises(ValueError, match="missing workspace"):
                JobQueue(database).enqueue_in_session(
                    session, "synthetic.rejected", workspace_id="missing-workspace"
                )

    with database.session() as session:
        record = session.get(SessionRecord, source.id)
        assert record is not None and record.name == "Retained source name"
        assert session.scalar(select(Job.id)) is None
        assert session.scalar(select(JobActorRecord.job_id)) is None
        assert session.scalar(select(JobEvent.id)) is None
