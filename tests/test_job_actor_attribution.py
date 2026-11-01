from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, text

from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_storage,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User
from pandrator.web.database import Database, upgrade_database
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import (
    AppSetting,
    Job,
    JobEvent,
)
from pandrator.web.models import (
    JobActorRecord as ExportedJobActorRecord,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
HOST_ACTOR = HostActor("pandrator-cli")


@pytest.fixture
def job_runtime(tmp_path: Path) -> Iterator[tuple[Database, JobQueue, UserActor]]:
    path = tmp_path / "job-actor-attribution.sqlite3"
    upgrade_database(path)
    database = Database(path)
    queue = JobQueue(database)
    with database.session() as session:
        row = session.execute(
            select(User, UserCredential)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(User.legacy_subject == "owner")
        ).one()
        user, credential = row
        assert user.is_active is True
        user_id = user.id
        auth_revision = credential.auth_revision
    browser_store = BrowserSessionStore(database)
    issued = browser_store.issue(
        user_id,
        auth_revision,
        subject="owner",
        principal_kind="owner_session",
        scopes={"app.read"},
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    principal = browser_store.resolve(issued.raw_token)
    assert principal is not None
    actor = UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        token_id=None,
        browser_session_id=principal.session_id,
        client_id=principal.client_id,
    )
    try:
        yield database, queue, actor
    finally:
        database.dispose()


def _stored_actor(database: Database, job_id: str) -> JobActorRecord | None:
    with database.session() as session:
        row = session.get(JobActorRecord, job_id)
        if row is None:
            return None
        return JobActorRecord(
            job_id=row.job_id,
            user_id=row.user_id,
            auth_revision=row.auth_revision,
            context_json=dict(row.context_json),
        )


def test_enqueue_captures_current_user_and_ignores_payload_identity(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, actor = job_runtime
    assert JobActorRecord is ExportedJobActorRecord
    spoofed_payload: dict[str, Any] = {
        "user_id": "00000000-0000-4000-8000-000000000099",
        "auth_revision": 99,
        "actor": {"subject": "payload-claim", "scopes": ["app.admin"]},
    }

    with execution_actor_scope(actor):
        job = queue.enqueue("actor.user", spoofed_payload)

    record = _stored_actor(database, job.id)
    assert record is not None
    assert record.user_id == actor.user_id
    assert record.auth_revision == actor.auth_revision
    assert actor_from_storage(record.user_id, record.auth_revision, record.context_json) == actor
    with database.session() as session:
        stored_job = session.get(Job, job.id)
        queued_event = session.scalar(
            select(JobEvent).where(
                JobEvent.job_id == job.id,
                JobEvent.event_type == "job.queued",
            )
        )
        assert stored_job is not None
        assert stored_job.payload_json == spoofed_payload
        assert queued_event is not None
        assert not (
            {"user_id", "auth_revision", "actor", "scopes"} & queued_event.payload_json.keys()
        )


def test_enqueue_captures_host_and_none_creates_no_attribution(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, _actor = job_runtime
    with execution_actor_scope(HOST_ACTOR):
        host_job = queue.enqueue("actor.host")
    legacy_job = queue.enqueue(
        "actor.unattributed",
        {"user_id": "payload-user", "auth_revision": 1},
    )

    host_record = _stored_actor(database, host_job.id)
    assert host_record is not None
    assert host_record.user_id is None
    assert host_record.auth_revision is None
    assert host_record.context_json == {
        "version": 1,
        "source": "host",
        "label": "pandrator-cli",
    }
    assert _stored_actor(database, legacy_job.id) is None


def test_revoked_actor_rolls_back_caller_rows_job_sidecar_and_event(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, actor = job_runtime
    assert actor.browser_session_id is not None
    with database.immediate_session() as session:
        browser_session = session.get(BrowserAuthSession, actor.browser_session_id)
        assert browser_session is not None
        browser_session.revoked_at = NOW

    with execution_actor_scope(actor):
        with database.session() as session:
            session.add(AppSetting(key="actor-admission-domain", value_json={"written": True}))
            with pytest.raises(PermissionError):
                queue.enqueue_in_session(session, "actor.revoked")

    with database.session() as session:
        assert session.get(AppSetting, "actor-admission-domain") is None
        assert session.scalar(select(Job.id).where(Job.kind == "actor.revoked")) is None
        assert (
            session.scalar(select(JobEvent.id).where(JobEvent.event_type == "job.queued")) is None
        )
        assert session.scalar(select(JobActorRecord.job_id)) is None


def test_child_job_inherits_actor_and_retry_reuses_original_attribution(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, actor = job_runtime
    with execution_actor_scope(actor):
        parent_job = queue.enqueue("actor.retry", max_attempts=2)

    seen_actors: list[object] = []
    child_ids: list[str] = []

    def handler(
        payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        seen_actors.append(current_execution_actor.get())
        lease = current_job_lease.get()
        assert isinstance(lease, dict)
        assert lease["job_id"] == payload["_job_id"]
        if len(seen_actors) == 1:
            raise RuntimeError("retry this synthetic job")
        child = queue.enqueue("actor.child", {"parent_job_id": payload["_job_id"]})
        child_ids.append(child.id)
        return None

    worker = Worker(queue, "actor-worker", {"actor.retry": handler})
    outer_lease: JobLease = {
        "job_id": "outer-job",
        "owner_id": "outer-worker",
        "lease_generation": 1,
    }
    lease_token = current_job_lease.set(outer_lease)
    try:
        with execution_actor_scope(HOST_ACTOR):
            assert worker.run_once() is True
            assert current_execution_actor.get() == HOST_ACTOR
            assert current_job_lease.get() is outer_lease
            with database.session() as session:
                current_job = session.get(Job, parent_job.id)
                assert current_job is not None and current_job.status == "queued"
            assert worker.run_once() is True
            assert current_execution_actor.get() == HOST_ACTOR
            assert current_job_lease.get() is outer_lease
    finally:
        current_job_lease.reset(lease_token)

    assert seen_actors == [actor, actor]
    assert len(child_ids) == 1
    parent_record = _stored_actor(database, parent_job.id)
    child_record = _stored_actor(database, child_ids[0])
    assert parent_record is not None and child_record is not None
    assert (
        actor_from_storage(
            parent_record.user_id, parent_record.auth_revision, parent_record.context_json
        )
        == actor
    )
    assert (
        actor_from_storage(
            child_record.user_id, child_record.auth_revision, child_record.context_json
        )
        == actor
    )


def test_legacy_worker_job_clears_outer_host_and_child_stays_unattributed(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, _actor = job_runtime
    legacy_job = queue.enqueue("actor.legacy")
    seen: list[object] = []
    child_ids: list[str] = []

    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        seen.append(current_execution_actor.get())
        child_ids.append(queue.enqueue("actor.legacy-child").id)
        return None

    worker = Worker(queue, "legacy-worker", {"actor.legacy": handler})
    with execution_actor_scope(HOST_ACTOR):
        assert worker.run_once() is True
        assert current_execution_actor.get() == HOST_ACTOR

    assert seen == [None]
    assert _stored_actor(database, legacy_job.id) is None
    assert len(child_ids) == 1
    assert _stored_actor(database, child_ids[0]) is None


def test_malformed_worker_attribution_fails_before_handler(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, _actor = job_runtime
    with execution_actor_scope(HOST_ACTOR):
        job = queue.enqueue("actor.malformed")
    with database.immediate_session() as session:
        record = session.get(JobActorRecord, job.id)
        assert record is not None
        record.context_json = {"version": 999, "source": "host", "label": "pandrator-cli"}

    calls: list[str] = []

    def forbidden_handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        calls.append("called")
        return None

    worker = Worker(queue, "malformed-worker", {"actor.malformed": forbidden_handler})
    with execution_actor_scope(HOST_ACTOR):
        assert worker.run_once() is True
        assert current_execution_actor.get() == HOST_ACTOR

    assert calls == []
    with database.session() as session:
        failed_job = session.get(Job, job.id)
        assert failed_job is not None
        assert failed_job.status == "failed"
        assert failed_job.error_code


def test_present_null_worker_attribution_fails_before_handler(
    job_runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, queue, _actor = job_runtime
    with execution_actor_scope(HOST_ACTOR):
        job = queue.enqueue("actor.null-attribution")
    with database.immediate_session() as session:
        record = session.get(JobActorRecord, job.id)
        assert record is not None
        record.user_id = None
        record.auth_revision = None
        record.context_json = cast(dict[str, Any], None)

    with database.session() as session:
        stored_json = session.execute(
            text("SELECT context_json FROM job_actors WHERE job_id = :job_id"),
            {"job_id": job.id},
        ).scalar_one()
        assert stored_json == "null"

    calls: list[str] = []

    def forbidden_handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        calls.append("called")
        return None

    worker = Worker(queue, "null-attribution-worker", {"actor.null-attribution": forbidden_handler})
    with execution_actor_scope(HOST_ACTOR):
        assert worker.run_once() is True
        assert current_execution_actor.get() == HOST_ACTOR

    assert calls == []
    with database.session() as session:
        failed_job = session.get(Job, job.id)
        assert failed_job is not None
        assert failed_job.status == "failed"
        assert failed_job.error_code
