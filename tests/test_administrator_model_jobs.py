from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from pandrator.web.accounts.administration import AdministratorRequired
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.execution_actor import (
    ExecutionActor,
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import AppSetting, Job, JobActorRecord, JobEvent
from pandrator.web.workflow_voice import upload_rvc_model

HOST_ACTOR = HostActor("pandrator-cli")
RVC_UPLOAD = "rvc.model.upload"


@pytest.fixture
def administrator_runtime(
    tmp_path: Path,
) -> Iterator[tuple[Database, JobQueue, str, int]]:
    path = tmp_path / "administrator-model-jobs.sqlite3"
    upgrade_database(path)
    database = Database(path)
    queue = JobQueue(database)
    with database.immediate_session() as session:
        user = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert user is not None
        user.is_instance_admin = True
        credential = session.get(UserCredential, user.id)
        assert credential is not None
        user_id = user.id
        auth_revision = credential.auth_revision
    try:
        yield database, queue, user_id, auth_revision
    finally:
        database.dispose()


def _browser_actor(
    database: Database,
    user_id: str,
    auth_revision: int,
    scopes: frozenset[str],
    *,
    principal_kind: str = "owner_session",
) -> UserActor:
    issued = BrowserSessionStore(database).issue(
        user_id,
        auth_revision,
        subject="owner",
        principal_kind=principal_kind,
        scopes=scopes,
        client_id=("pandrator-manager-browser" if principal_kind == "manager_bootstrap" else None),
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    principal = BrowserSessionStore(database).resolve(issued.raw_token)
    assert principal is not None
    return UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        token_id=None,
        browser_session_id=principal.session_id,
        client_id=principal.client_id,
    )


def _assert_no_job_for_kind(database: Database, kind: str) -> None:
    with database.session() as session:
        job_id = session.scalar(select(Job.id).where(Job.kind == kind))
        assert job_id is None
        assert (
            session.scalar(select(JobEvent.id).where(JobEvent.event_type == "job.queued")) is None
        )
        assert session.scalar(select(JobActorRecord.job_id)) is None


def test_rvc_upload_queue_requires_admin_role_and_both_literal_scopes(
    administrator_runtime: tuple[Database, JobQueue, str, int],
) -> None:
    database, queue, user_id, revision = administrator_runtime
    administrator = _browser_actor(
        database,
        user_id,
        revision,
        frozenset({"app.admin", "app.write"}),
    )
    with execution_actor_scope(administrator):
        admitted = queue.enqueue(RVC_UPLOAD, {"pth_artifact_id": "payload-only"})
    handler_calls: list[str] = []

    def allowed_handler(
        payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        assert current_execution_actor.get() == administrator
        handler_calls.append(payload["_job_id"])
        return {"accepted": True}

    worker = Worker(queue, "authorized-rvc-worker", {RVC_UPLOAD: allowed_handler})
    assert worker.run_once() is True
    assert handler_calls == [admitted.id]
    with database.session() as session:
        record = session.get(JobActorRecord, admitted.id)
        assert record is not None

    missing_write = _browser_actor(database, user_id, revision, frozenset({"app.admin"}))
    with execution_actor_scope(missing_write):
        with pytest.raises(AdministratorRequired):
            queue.enqueue("rvc.model.upload")

    missing_admin_scope = _browser_actor(database, user_id, revision, frozenset({"app.write"}))
    with execution_actor_scope(missing_admin_scope):
        with pytest.raises(AdministratorRequired):
            queue.enqueue("rvc.model.upload")
    with database.session() as session:
        assert list(session.scalars(select(Job.id).where(Job.kind == RVC_UPLOAD))) == [admitted.id]
        assert list(
            session.scalars(
                select(JobEvent.job_id)
                .join(Job, Job.id == JobEvent.job_id)
                .where(Job.kind == RVC_UPLOAD, JobEvent.event_type == "job.queued")
            )
        ) == [admitted.id]
        assert list(
            session.scalars(
                select(JobActorRecord.job_id).where(JobActorRecord.job_id == admitted.id)
            )
        ) == [admitted.id]


@pytest.mark.parametrize("scopes", [frozenset({"app.admin"}), frozenset({"app.run"})])
def test_training_admission_requires_both_administrator_and_run_scopes(
    administrator_runtime: tuple[Database, JobQueue, str, int],
    scopes: frozenset[str],
) -> None:
    database, queue, user_id, revision = administrator_runtime
    actor = _browser_actor(database, user_id, revision, scopes)
    with execution_actor_scope(actor), pytest.raises(AdministratorRequired):
        queue.enqueue("training.xtts")
    _assert_no_job_for_kind(database, "training.xtts")


def test_training_admission_and_worker_recheck_actual_current_authority(
    administrator_runtime: tuple[Database, JobQueue, str, int],
) -> None:
    database, queue, user_id, revision = administrator_runtime
    with execution_actor_scope(None), pytest.raises(AdministratorRequired):
        queue.enqueue("training.xtts")
    _assert_no_job_for_kind(database, "training.xtts")
    actor = _browser_actor(database, user_id, revision, frozenset({"app.admin", "app.run"}))
    with execution_actor_scope(actor):
        admitted = queue.enqueue("training.xtts")
    with database.immediate_session() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.is_instance_admin = False
    calls: list[str] = []

    def forbidden_handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        calls.append("called")
        return None

    worker = Worker(queue, "training-authority-worker", {"training.xtts": forbidden_handler})
    assert worker.run_once() is True
    assert calls == []
    assert queue.get(admitted.id).status == "failed"


def test_unknown_and_manager_bootstrap_actors_cannot_enqueue_privileged_job(
    administrator_runtime: tuple[Database, JobQueue, str, int],
) -> None:
    database, queue, user_id, revision = administrator_runtime
    with execution_actor_scope(None):
        with database.session() as session:
            session.add(AppSetting(key="privileged-queue-domain", value_json={"written": True}))
            with pytest.raises(AdministratorRequired):
                queue.enqueue_in_session(session, RVC_UPLOAD)
    with database.session() as session:
        assert session.get(AppSetting, "privileged-queue-domain") is None

    manager_bootstrap = _browser_actor(
        database,
        user_id,
        revision,
        frozenset({"app.write"}),
        principal_kind="manager_bootstrap",
    )
    with execution_actor_scope(manager_bootstrap):
        with pytest.raises(AdministratorRequired):
            queue.enqueue("rvc.model.upload")
    _assert_no_job_for_kind(database, "rvc.model.upload")


def test_explicit_host_actor_can_enqueue_privileged_rvc_job(
    administrator_runtime: tuple[Database, JobQueue, str, int],
) -> None:
    database, queue, _user_id, _revision = administrator_runtime
    with execution_actor_scope(HOST_ACTOR):
        job = queue.enqueue(RVC_UPLOAD)
    assert job.id
    with database.session() as session:
        record = session.get(JobActorRecord, job.id)
        assert record is not None
        assert record.context_json == {
            "version": 1,
            "source": "host",
            "label": "pandrator-cli",
        }


def test_demoted_administrator_is_rechecked_before_worker_handler(
    administrator_runtime: tuple[Database, JobQueue, str, int],
) -> None:
    database, queue, user_id, revision = administrator_runtime
    administrator = _browser_actor(
        database,
        user_id,
        revision,
        frozenset({"app.admin", "app.write"}),
    )
    with execution_actor_scope(administrator):
        job = queue.enqueue(RVC_UPLOAD)

    with database.immediate_session() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.is_instance_admin = False

    calls: list[str] = []

    def forbidden_handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        calls.append("called")
        return None

    worker = Worker(queue, "administrator-boundary-worker", {RVC_UPLOAD: forbidden_handler})
    with execution_actor_scope(HOST_ACTOR):
        assert worker.run_once() is True
        assert current_execution_actor.get() == HOST_ACTOR

    assert calls == []
    with database.session() as session:
        failed_job = session.get(Job, job.id)
        assert failed_job is not None
        assert failed_job.status == "failed"
        assert failed_job.error_code


def test_rvc_upload_command_denies_before_payload_or_input_resolution(
    administrator_runtime: tuple[Database, JobQueue, str, int],
) -> None:
    database, _queue, _user_id, _revision = administrator_runtime
    resolved: list[str] = []

    def resolve_input(artifact_id: str) -> tuple[Any, Path]:
        resolved.append(artifact_id)
        raise AssertionError("input resolution must follow administrator authorization")

    class UnreadablePayload(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            raise AssertionError(f"payload was read before authorization: {key}")

    context = cast(Any, SimpleNamespace(database=database, _resolve_input=resolve_input))
    with execution_actor_scope(None):
        with pytest.raises(AdministratorRequired):
            upload_rvc_model(
                context,
                UnreadablePayload(),
                lambda _value, _detail=None: None,
                threading.Event(),
            )
    assert resolved == []


def test_rvc_upload_command_explicitly_allows_host_provenance(
    administrator_runtime: tuple[Database, JobQueue, str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _queue, _user_id, _revision = administrator_runtime
    import pandrator.web.accounts.administration as administration

    calls: list[tuple[object, object, bool, ExecutionActor | None]] = []

    class StopAfterAuthorization(Exception):
        pass

    def authorize(
        received_database: Database,
        *,
        required_scopes: object = (),
        allow_host: bool = False,
    ) -> None:
        calls.append(
            (received_database, required_scopes, allow_host, current_execution_actor.get())
        )
        raise StopAfterAuthorization

    monkeypatch.setattr(administration, "require_execution_administrator_actor", authorize)
    context = cast(Any, SimpleNamespace(database=database))
    with execution_actor_scope(HOST_ACTOR):
        with pytest.raises(StopAfterAuthorization):
            upload_rvc_model(
                context,
                {},
                lambda _value, _detail=None: None,
                threading.Event(),
            )

    assert calls == [(database, {"app.write"}, True, HOST_ACTOR)]
