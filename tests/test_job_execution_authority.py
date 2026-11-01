"""Focused tests for authority delegated only by an active durable-job lease."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    require_model_acquisition,
)
from pandrator.web.accounts.actor_authorization import (
    ActorAuthorizationDenied,
    require_actor_in_session,
)
from pandrator.web.accounts.administration import (
    AdministratorRequired,
    require_administrator_actor,
)
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.execution_actor import (
    ExecutionActor,
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.job_authorization import require_execution_actor_in_session
from pandrator.web.accounts.models import User
from pandrator.web.auth import AuthService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.model_base import utcnow
from pandrator.web.models import (
    ApiToken,
    AppSetting,
    AutomationClient,
    Job,
    JobActorRecord,
)

ADMIN_SCOPES = frozenset({"app.admin", "app.read", "app.run", "app.write"})
RVC_UPLOAD = "rvc.model.upload"


@dataclass(frozen=True)
class Runtime:
    database: Database
    queue: JobQueue
    user_id: str
    auth_revision: int


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    path = tmp_path / "job-execution-authority.sqlite3"
    upgrade_database(path)
    database = Database(path)
    with database.immediate_session() as session:
        user = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert user is not None
        credential = session.get(UserCredential, user.id)
        assert credential is not None
        user.is_instance_admin = True
        user_id = user.id
        auth_revision = credential.auth_revision
    result = Runtime(database, JobQueue(database), user_id, auth_revision)
    try:
        yield result
    finally:
        database.dispose()


def _browser_actor(runtime: Runtime, *, scopes: frozenset[str] = ADMIN_SCOPES) -> UserActor:
    store = BrowserSessionStore(runtime.database)
    issued = store.issue(
        runtime.user_id,
        runtime.auth_revision,
        subject="owner",
        principal_kind="owner_session",
        scopes=scopes,
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    principal = store.resolve(issued.raw_token)
    assert principal is not None
    return UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        browser_session_id=principal.session_id,
        client_id=principal.client_id,
    )


def _api_actor(
    runtime: Runtime,
    *,
    client_id: str | None = None,
    scopes: frozenset[str] = ADMIN_SCOPES,
) -> UserActor:
    token, _raw = AuthService(runtime.database).create_api_token(
        "synthetic execution token",
        scopes=scopes,
        user_id=runtime.user_id,
        auth_revision=runtime.auth_revision,
        client_id=client_id,
    )
    assert token.user_id is not None and token.user_auth_revision is not None
    assert token.subject is not None
    return UserActor(
        user_id=token.user_id,
        auth_revision=token.user_auth_revision,
        subject=token.subject,
        principal_kind=cast(Any, token.principal_kind),
        scopes=frozenset(token.scopes_json),
        token_id=token.id,
        client_id=token.client_id,
    )


def _create_client(runtime: Runtime, scopes: frozenset[str] = ADMIN_SCOPES) -> str:
    client_id = str(uuid4())
    with runtime.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic execution client",
                subject=f"synthetic-client:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=sorted(scopes),
                target_instance_id=str(uuid4()),
                canonical_origin="https://execution-authority.invalid",
                created_by="synthetic-test",
                user_id=runtime.user_id,
                user_auth_revision=runtime.auth_revision,
            )
        )
    return client_id


def _enqueue(
    runtime: Runtime,
    actor: ExecutionActor,
    kind: str = "noop",
    payload: dict[str, Any] | None = None,
) -> Job:
    with execution_actor_scope(actor):
        return runtime.queue.enqueue(kind, payload)


def _set_browser_state(
    runtime: Runtime,
    actor: UserActor,
    *,
    revoked: bool = False,
    expired: bool = False,
) -> None:
    assert actor.browser_session_id is not None
    with runtime.database.immediate_session() as session:
        browser = session.get(BrowserAuthSession, actor.browser_session_id)
        assert browser is not None
        if revoked:
            browser.revoked_at = utcnow()
        if expired:
            past = utcnow() - timedelta(seconds=1)
            browser.expires_at = past
            browser.idle_expires_at = past


def _claim(runtime: Runtime, job: Job, worker_id: str = "authority-proof-worker") -> Job:
    claimed = runtime.queue.claim(worker_id)
    assert claimed is not None and claimed.id == job.id
    return claimed


def _lease_for(job: Job, worker_id: str) -> JobLease:
    return {
        "job_id": job.id,
        "owner_id": worker_id,
        "lease_generation": job.lease_generation,
    }


@contextmanager
def _lease_scope(value: object) -> Iterator[None]:
    token = current_job_lease.set(cast(Any, value))
    try:
        yield
    finally:
        current_job_lease.reset(token)


def _authorize_under_lease(
    runtime: Runtime,
    actor: ExecutionActor | None,
    lease: object,
    *,
    required_scopes: object = (),
    require_admin: bool = False,
    allow_host: bool = False,
) -> None:
    with execution_actor_scope(actor), _lease_scope(lease):
        with runtime.database.immediate_session() as session:
            require_execution_actor_in_session(
                session,
                actor,
                required_scopes=required_scopes,
                require_admin=require_admin,
                allow_host=allow_host,
            )


def _forbidden_handler(
    calls: list[str],
) -> Callable[
    [dict[str, Any], Callable[[float, str | None], None], threading.Event], dict[str, Any] | None
]:
    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        calls.append("called")
        return None

    return handler


def test_no_lease_path_matches_strict_current_transport_validation(runtime: Runtime) -> None:
    actor = _browser_actor(runtime)
    with execution_actor_scope(actor), runtime.database.immediate_session() as session:
        strict = require_actor_in_session(session, actor, require_admin=True)
        delegated = require_execution_actor_in_session(session, actor, require_admin=True)
        assert delegated == strict

    _set_browser_state(runtime, actor, revoked=True)
    with execution_actor_scope(actor), runtime.database.immediate_session() as session:
        with pytest.raises(ActorAuthorizationDenied):
            require_actor_in_session(session, actor, require_admin=True)
    with execution_actor_scope(actor), runtime.database.immediate_session() as session:
        with pytest.raises(ActorAuthorizationDenied):
            require_execution_actor_in_session(session, actor, require_admin=True)


@pytest.mark.parametrize("browser_change", ["logout", "expiry"])
def test_active_browser_job_keeps_captured_authority_for_handler_model_guard_and_child(
    runtime: Runtime,
    browser_change: str,
) -> None:
    actor = _browser_actor(runtime)
    parent = _enqueue(runtime, actor, RVC_UPLOAD)
    _set_browser_state(
        runtime,
        actor,
        revoked=browser_change == "logout",
        expired=browser_change == "expiry",
    )
    called: list[str] = []
    children: list[str] = []

    def handler(
        payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any]:
        assert current_execution_actor.get() == actor
        lease = current_job_lease.get()
        assert lease is not None and lease["job_id"] == parent.id
        require_model_acquisition()
        child = runtime.queue.enqueue("child.noop", {"parent": parent.id})
        children.append(child.id)
        called.append(payload["_job_id"])
        return {"child_id": child.id}

    worker = Worker(runtime.queue, "browser-execution-worker", {RVC_UPLOAD: handler})
    assert worker.run_once() is True
    assert called == [parent.id]
    assert len(children) == 1
    assert runtime.queue.get(parent.id).status == "succeeded"
    assert runtime.queue.get(children[0]).status == "queued"
    assert current_execution_actor.get() is None
    assert current_job_lease.get() is None

    with execution_actor_scope(actor):
        with pytest.raises(AdministratorRequired):
            require_administrator_actor(runtime.database, required_scopes={"app.write"})
        with pytest.raises(AdministratorRequired):
            runtime.queue.enqueue(RVC_UPLOAD)


def test_natural_api_expiry_allows_admitted_job_but_denies_new_access(runtime: Runtime) -> None:
    actor = _api_actor(runtime)
    parent = _enqueue(runtime, actor, RVC_UPLOAD)
    assert actor.token_id is not None
    with runtime.database.immediate_session() as session:
        token = session.get(ApiToken, actor.token_id)
        assert token is not None
        token.expires_at = utcnow() - timedelta(seconds=1)

    calls: list[str] = []

    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        require_model_acquisition()
        calls.append(parent.id)
        return None

    worker = Worker(runtime.queue, "expired-token-worker", {RVC_UPLOAD: handler})
    assert worker.run_once() is True
    assert calls == [parent.id]
    assert runtime.queue.get(parent.id).status == "succeeded"
    with execution_actor_scope(actor):
        with pytest.raises(AdministratorRequired):
            require_administrator_actor(runtime.database, required_scopes={"app.write"})
        with pytest.raises(AdministratorRequired):
            runtime.queue.enqueue(RVC_UPLOAD)


@pytest.mark.parametrize(
    "change",
    ["revoked", "scope_narrowed", "account_disabled", "epoch_changed", "admin_demoted"],
)
def test_worker_rechecks_api_revocation_scopes_account_epoch_and_role(
    runtime: Runtime,
    change: str,
) -> None:
    actor = _api_actor(runtime)
    parent = _enqueue(runtime, actor, RVC_UPLOAD)
    assert actor.token_id is not None
    with runtime.database.immediate_session() as session:
        token = session.get(ApiToken, actor.token_id)
        user = session.get(User, runtime.user_id)
        credential = session.get(UserCredential, runtime.user_id)
        assert token is not None and user is not None and credential is not None
        if change == "revoked":
            token.revoked_at = utcnow()
        elif change == "scope_narrowed":
            token.scopes_json = ["app.admin", "app.read", "app.run"]
        elif change == "account_disabled":
            user.is_active = False
        elif change == "epoch_changed":
            credential.auth_revision += 1
        else:
            user.is_instance_admin = False

    calls: list[str] = []
    worker = Worker(
        runtime.queue,
        "revocation-check-worker",
        {RVC_UPLOAD: _forbidden_handler(calls)},
    )
    assert worker.run_once() is True
    assert calls == []
    assert runtime.queue.get(parent.id).status == "failed"
    assert current_execution_actor.get() is None
    assert current_job_lease.get() is None


@pytest.mark.parametrize("change", ["revoked", "scope_narrowed"])
def test_api_client_grant_is_rechecked_for_captured_scopes(
    runtime: Runtime,
    change: str,
) -> None:
    client_id = _create_client(runtime)
    actor = _api_actor(runtime, client_id=client_id)
    parent = _enqueue(runtime, actor, RVC_UPLOAD)
    with runtime.database.immediate_session() as session:
        client = session.get(AutomationClient, client_id)
        assert client is not None
        if change == "revoked":
            client.revoked_at = utcnow()
        else:
            client.allowed_scopes_json = ["app.admin", "app.read", "app.run"]

    calls: list[str] = []
    worker = Worker(
        runtime.queue,
        "client-grant-worker",
        {RVC_UPLOAD: _forbidden_handler(calls)},
    )
    assert worker.run_once() is True
    assert calls == []
    assert runtime.queue.get(parent.id).status == "failed"


@pytest.mark.parametrize(
    "change",
    [
        "wrong_owner",
        "wrong_generation",
        "expired",
        "cancelled",
        "terminal",
        "missing_sidecar",
        "malformed_sidecar",
        "different_actor",
        "host_replacement",
        "unknown_replacement",
    ],
)
def test_active_lease_proof_rejects_forged_or_stale_identity(
    runtime: Runtime,
    change: str,
) -> None:
    actor = _browser_actor(runtime)
    admitted = _enqueue(runtime, actor)
    worker_id = "authority-proof-worker"
    claimed = _claim(runtime, admitted, worker_id)
    lease = _lease_for(claimed, worker_id)
    supplied_actor: ExecutionActor | None = actor
    with runtime.database.immediate_session() as session:
        stored = session.get(Job, admitted.id)
        assert stored is not None
        if change == "expired":
            stored.lease_expires_at = utcnow() - timedelta(seconds=1)
        elif change == "cancelled":
            stored.status = "cancel_requested"
        elif change == "terminal":
            stored.status = "succeeded"
        elif change == "missing_sidecar":
            sidecar = session.get(JobActorRecord, admitted.id)
            assert sidecar is not None
            session.delete(sidecar)
        elif change == "malformed_sidecar":
            sidecar = session.get(JobActorRecord, admitted.id)
            assert sidecar is not None
            sidecar.context_json = {"version": 77}
        elif change == "different_actor":
            supplied_actor = UserActor(
                user_id=actor.user_id,
                auth_revision=actor.auth_revision,
                subject=actor.subject,
                principal_kind=actor.principal_kind,
                scopes=frozenset({"app.read"}),
                browser_session_id=actor.browser_session_id,
                client_id=actor.client_id,
            )
        elif change == "host_replacement":
            supplied_actor = HostActor("pandrator-cli")
        elif change == "unknown_replacement":
            supplied_actor = None

    if change == "wrong_owner":
        lease["owner_id"] = "different-worker"
    elif change == "wrong_generation":
        lease["lease_generation"] += 1

    with pytest.raises(ActorAuthorizationDenied) as error:
        _authorize_under_lease(runtime, supplied_actor, lease)
    assert str(error.value) == "The execution actor is not currently authorized."


@pytest.mark.parametrize(
    "lease",
    [
        {},
        {"job_id": "job", "owner_id": "worker", "lease_generation": True},
        {"job_id": "job", "owner_id": " ", "lease_generation": 1},
        {"job_id": "job", "owner_id": "worker", "lease_generation": 1, "extra": 1},
    ],
)
def test_malformed_private_lease_context_is_denied_without_fallback(
    runtime: Runtime,
    lease: dict[str, Any],
) -> None:
    actor = _browser_actor(runtime)
    with pytest.raises(ActorAuthorizationDenied):
        _authorize_under_lease(runtime, actor, lease)


def test_helper_preserves_pending_transaction_after_browser_logout(
    runtime: Runtime,
) -> None:
    actor = _browser_actor(runtime)
    admitted = _enqueue(runtime, actor)
    worker_id = "fresh-column-worker"
    claimed = _claim(runtime, admitted, worker_id)
    stale_session = Session(bind=runtime.database.engine, expire_on_commit=False)
    pending = AppSetting(key="execution-authority-pending", value_json={"saved": False})
    try:
        stale_browser = stale_session.get(BrowserAuthSession, actor.browser_session_id)
        assert stale_browser is not None and stale_browser.revoked_at is None
        stale_session.commit()
        _set_browser_state(runtime, actor, revoked=True)
        stale_session.add(pending)
        with execution_actor_scope(actor), _lease_scope(_lease_for(claimed, worker_id)):
            result = require_execution_actor_in_session(
                stale_session,
                actor,
                require_admin=True,
            )
        assert result is not None and result.user_id == runtime.user_id
        assert pending in stale_session.new
        with runtime.database.session() as session:
            assert session.get(AppSetting, pending.key) is None
    finally:
        stale_session.rollback()
        stale_session.close()


@pytest.mark.parametrize("change", ["role", "epoch", "lease", "token"])
def test_helper_rejects_fresh_revocation_despite_cached_rows_without_finalizing_caller(
    runtime: Runtime,
    change: str,
) -> None:
    actor = _api_actor(runtime)
    admitted = _enqueue(runtime, actor)
    worker_id = "fresh-revocation-worker"
    claimed = _claim(runtime, admitted, worker_id)
    stale_session = Session(bind=runtime.database.engine, expire_on_commit=False)
    pending = AppSetting(key="revoked-authority-pending", value_json=True)
    try:
        stale_user = stale_session.get(User, runtime.user_id)
        stale_credential = stale_session.get(UserCredential, runtime.user_id)
        stale_job = stale_session.get(Job, admitted.id)
        stale_token = stale_session.get(ApiToken, actor.token_id)
        assert stale_user is not None and stale_user.is_instance_admin
        assert (
            stale_credential is not None and stale_credential.auth_revision == actor.auth_revision
        )
        assert stale_job is not None and stale_job.status == "running"
        assert stale_token is not None and stale_token.revoked_at is None
        stale_session.commit()
        with runtime.database.immediate_session() as session:
            if change == "role":
                user = session.get(User, runtime.user_id)
                assert user is not None
                user.is_instance_admin = False
            elif change == "epoch":
                credential = session.get(UserCredential, runtime.user_id)
                assert credential is not None
                credential.auth_revision += 1
            elif change == "lease":
                job = session.get(Job, admitted.id)
                assert job is not None
                job.lease_expires_at = utcnow() - timedelta(seconds=1)
            else:
                token = session.get(ApiToken, actor.token_id)
                assert token is not None
                token.revoked_at = utcnow()

        assert stale_user.is_instance_admin
        assert stale_credential.auth_revision == actor.auth_revision
        assert stale_job.status == "running"
        assert stale_token.revoked_at is None
        stale_session.add(pending)
        transaction = stale_session.get_transaction()
        with execution_actor_scope(actor), _lease_scope(_lease_for(claimed, worker_id)):
            with pytest.raises(ActorAuthorizationDenied):
                require_execution_actor_in_session(stale_session, actor, require_admin=True)
        assert stale_session.get_transaction() is transaction
        assert pending in stale_session.new
        with runtime.database.session() as session:
            assert session.get(AppSetting, pending.key) is None
    finally:
        stale_session.rollback()
        stale_session.close()


def test_failed_child_enqueue_rolls_back_caller_work_and_creates_no_child(
    runtime: Runtime,
) -> None:
    actor = _browser_actor(runtime)
    parent = _enqueue(runtime, actor, "parent.rollback")
    calls: list[str] = []

    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        lease = current_job_lease.get()
        assert lease is not None and lease["job_id"] == parent.id
        wrong_lease = dict(lease)
        wrong_lease["lease_generation"] = lease["lease_generation"] + 1
        with _lease_scope(wrong_lease), runtime.database.session() as session:
            session.add(AppSetting(key="rejected-child-side-effect", value_json=True))
            with pytest.raises(ActorAuthorizationDenied):
                runtime.queue.enqueue_in_session(session, "child.noop")
        calls.append(parent.id)
        return None

    worker = Worker(runtime.queue, "child-rollback-worker", {"parent.rollback": handler})
    assert worker.run_once() is True
    assert calls == [parent.id]
    with runtime.database.session() as session:
        assert session.get(AppSetting, "rejected-child-side-effect") is None
        assert session.scalar(select(Job.id).where(Job.kind == "child.noop")) is None
        assert (
            session.scalar(select(JobActorRecord.job_id).where(JobActorRecord.job_id != parent.id))
            is None
        )


def test_unknown_legacy_actor_keeps_existing_enqueue_but_cannot_acquire_model(
    runtime: Runtime,
) -> None:
    with execution_actor_scope(None):
        job = runtime.queue.enqueue("legacy.noop")
    calls: list[str] = []

    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        require_model_acquisition()
        calls.append("model-authorized")
        return None

    worker = Worker(runtime.queue, "legacy-actor-worker", {"legacy.noop": handler})
    assert worker.run_once() is True
    assert calls == []
    assert runtime.queue.get(job.id).status == "failed"


def test_manager_bootstrap_and_missing_literal_scope_cannot_use_execution_admin(
    runtime: Runtime,
) -> None:
    store = BrowserSessionStore(runtime.database)
    issued = store.issue(
        runtime.user_id,
        runtime.auth_revision,
        subject="manager-bootstrap",
        principal_kind="manager_bootstrap",
        scopes={"app.read", "app.run"},
        client_id="pandrator-manager-browser",
    )
    principal = store.resolve(issued.raw_token)
    assert principal is not None
    bootstrap = UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        browser_session_id=principal.session_id,
        client_id=principal.client_id,
    )
    bootstrap_job = _enqueue(runtime, bootstrap)
    claimed = _claim(runtime, bootstrap_job, "bootstrap-worker")
    with pytest.raises(ActorAuthorizationDenied):
        _authorize_under_lease(
            runtime,
            bootstrap,
            _lease_for(claimed, "bootstrap-worker"),
            required_scopes={"app.admin"},
            require_admin=True,
            allow_host=True,
        )

    actor = _browser_actor(runtime, scopes=frozenset({"app.admin", "app.read"}))
    scoped_job = _enqueue(runtime, actor)
    scoped_claim = _claim(runtime, scoped_job, "scope-worker")
    with pytest.raises(ActorAuthorizationDenied):
        _authorize_under_lease(
            runtime,
            actor,
            _lease_for(scoped_claim, "scope-worker"),
            required_scopes={"app.write"},
            require_admin=True,
            allow_host=True,
        )


def test_explicit_cli_host_can_run_privileged_job_and_model_guard(runtime: Runtime) -> None:
    host = HostActor("pandrator-cli")
    parent = _enqueue(runtime, host, RVC_UPLOAD)
    called: list[str] = []

    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        assert current_execution_actor.get() == host
        require_model_acquisition()
        called.append(parent.id)
        return None

    worker = Worker(runtime.queue, "cli-host-worker", {RVC_UPLOAD: handler})
    assert worker.run_once() is True
    assert called == [parent.id]
    assert runtime.queue.get(parent.id).status == "succeeded"


def test_worker_overwrites_payload_authority_claims_and_restores_nested_contexts(
    runtime: Runtime,
) -> None:
    actor = _browser_actor(runtime)
    child_actor = _browser_actor(runtime, scopes=frozenset({"app.read"}))
    parent = _enqueue(
        runtime,
        actor,
        "parent.noop",
        {
            "_job_id": "forged-job",
            "_lease_generation": 0,
            "actor_user_id": "forged-user",
            "actor_context_json": {"source": "host"},
        },
    )
    child = _enqueue(runtime, child_actor, "child.noop")
    with runtime.database.immediate_session() as session:
        child_row = session.get(Job, child.id)
        assert child_row is not None
        child_row.available_at = utcnow() + timedelta(hours=1)
    nested_calls: list[str] = []

    def child_handler(
        payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        assert current_execution_actor.get() == child_actor
        lease = current_job_lease.get()
        assert lease is not None and lease["job_id"] == child.id
        assert payload["_job_id"] == child.id
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()
        nested_calls.append(payload["_job_id"])
        return None

    def parent_handler(
        payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> dict[str, Any]:
        assert payload["_job_id"] == parent.id
        assert payload["_lease_generation"] > 0
        assert current_execution_actor.get() == actor
        parent_lease = current_job_lease.get()
        assert parent_lease is not None and parent_lease["job_id"] == parent.id
        with runtime.database.immediate_session() as session:
            child_row = session.get(Job, child.id)
            assert child_row is not None
            child_row.available_at = utcnow() - timedelta(seconds=1)
        nested = Worker(runtime.queue, "nested-context-worker", {"child.noop": child_handler})
        assert nested.run_once() is True
        assert current_execution_actor.get() == actor
        assert current_job_lease.get() == parent_lease
        return {"child_id": child.id}

    worker = Worker(runtime.queue, "parent-context-worker", {"parent.noop": parent_handler})
    outer_actor = HostActor("pandrator-cli")
    outer_lease: JobLease = {
        "job_id": "outer-job",
        "owner_id": "outer-worker",
        "lease_generation": 1,
    }
    token = current_job_lease.set(outer_lease)
    try:
        with execution_actor_scope(outer_actor):
            assert worker.run_once() is True
            assert current_execution_actor.get() == outer_actor
            assert current_job_lease.get() == outer_lease
    finally:
        current_job_lease.reset(token)
    assert nested_calls and len(nested_calls) == 1
    assert runtime.queue.get(parent.id).status == "succeeded"
    assert current_execution_actor.get() is None
    assert current_job_lease.get() is None


def test_worker_failure_restores_actor_and_lease_contexts(runtime: Runtime) -> None:
    actor = _browser_actor(runtime)
    job = _enqueue(runtime, actor, "failure.noop")

    def failing_handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        assert current_execution_actor.get() == actor
        assert current_job_lease.get() is not None
        raise RuntimeError("synthetic handler failure")

    worker = Worker(runtime.queue, "failure-context-worker", {"failure.noop": failing_handler})
    outer_actor = HostActor("pandrator-cli")
    outer_lease: JobLease = {
        "job_id": "outer-job",
        "owner_id": "outer-worker",
        "lease_generation": 1,
    }
    token = current_job_lease.set(outer_lease)
    try:
        with execution_actor_scope(outer_actor):
            assert worker.run_once() is True
            assert current_execution_actor.get() == outer_actor
            assert current_job_lease.get() == outer_lease
    finally:
        current_job_lease.reset(token)
    assert runtime.queue.get(job.id).status == "failed"
    assert current_execution_actor.get() is None
    assert current_job_lease.get() is None


def test_model_guard_rechecks_role_after_worker_precheck(runtime: Runtime) -> None:
    actor = _browser_actor(runtime)
    job = _enqueue(runtime, actor, RVC_UPLOAD)
    denied: list[str] = []

    def handler(
        _payload: dict[str, Any],
        _progress: Callable[[float, str | None], None],
        _cancel_event: threading.Event,
    ) -> None:
        with runtime.database.immediate_session() as session:
            user = session.get(User, runtime.user_id)
            assert user is not None
            user.is_instance_admin = False
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()
        denied.append(job.id)
        return None

    worker = Worker(runtime.queue, "model-guard-worker", {RVC_UPLOAD: handler})
    assert worker.run_once() is True
    assert denied == [job.id]
    assert runtime.queue.get(job.id).status == "succeeded"
