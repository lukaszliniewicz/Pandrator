"""Download authority follows the actual HTTP requester or durable job actor."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    host_model_acquisition_scope,
    require_model_acquisition,
)
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.accounts.execution_actor import HostActor, UserActor, execution_actor_scope
from pandrator.web.accounts.model_acquisition import model_acquisition_for_actor
from pandrator.web.accounts.models import User
from pandrator.web.api import create_app
from pandrator.web.auth import ALL_SCOPES, AuthService, BootstrapTokenStore
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Job, utcnow
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[Database, JobQueue, UserActor]]:
    path = tmp_path / "model-authority.sqlite3"
    upgrade_database(path)
    database = Database(path)
    queue = JobQueue(database)
    with database.session() as session:
        user = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert user is not None
        credential = session.get(UserCredential, user.id)
        assert credential is not None
        user_id, revision = user.id, credential.auth_revision
    issued = BrowserSessionStore(database).issue(
        user_id,
        revision,
        subject="owner",
        principal_kind="owner_session",
        scopes=ALL_SCOPES,
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    principal = BrowserSessionStore(database).resolve(issued.raw_token)
    assert principal is not None
    actor = UserActor(
        principal.user_id,
        principal.auth_revision,
        principal.subject,
        principal.kind,
        principal.scopes,
        None,
        principal.session_id,
        principal.client_id,
    )
    try:
        yield database, queue, actor
    finally:
        database.dispose()


def test_bound_authorizer_rechecks_actual_credential_for_every_missing_asset(
    runtime: tuple[Database, JobQueue, UserActor],
) -> None:
    database, _queue, actor = runtime
    with execution_actor_scope(actor), model_acquisition_for_actor(database):
        require_model_acquisition()
        with database.immediate_session() as session:
            credential = session.get(BrowserAuthSession, actor.browser_session_id)
            assert credential is not None
            credential.revoked_at = utcnow()
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()
    with pytest.raises(ModelAcquisitionDenied):
        require_model_acquisition()


@pytest.mark.parametrize("requester", ["administrator", "unknown", "demoted", "host"])
def test_worker_overrides_outer_host_permission_with_saved_job_requester(
    runtime: tuple[Database, JobQueue, UserActor], requester: str
) -> None:
    database, queue, actor = runtime
    admission_actor = None if requester == "unknown" else actor
    if requester == "host":
        admission_actor = HostActor("pandrator-cli")
    with execution_actor_scope(admission_actor):
        job = queue.enqueue("synthetic.model.acquire", {"actor_source": "host"})
    if requester == "demoted":
        with database.immediate_session() as session:
            user = session.get(User, actor.user_id)
            assert user is not None
            user.is_instance_admin = False
    downloads: list[str] = []

    def handler(
        _payload: dict[str, Any], _progress: Any, _cancel_event: threading.Event
    ) -> dict[str, Any]:
        require_model_acquisition()
        downloads.append("synthetic-model")
        return {"downloaded": True}

    worker = Worker(queue, "model-authority-worker", {"synthetic.model.acquire": handler})
    with execution_actor_scope(HostActor("pandrator-cli")), host_model_acquisition_scope():
        assert worker.run_once() is True
        # A denied historical/requester job must not corrupt the outer host scope.
        require_model_acquisition()
    allowed = requester in {"administrator", "host"}
    assert downloads == (["synthetic-model"] if allowed else [])
    with database.session() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        assert stored.status == ("succeeded" if allowed else "failed")


@pytest.mark.parametrize("authority", ["administrator", "without_scope", "user"])
def test_http_binds_current_requester_and_restores_outer_host_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    launch = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]
    database: Database = services["database"]
    auth: AuthService = services["auth"]
    downloads: list[str] = []

    def synthetic_missing_model(**_kwargs: Any) -> tuple[dict[str, Any], int]:
        # Synthetic sink on an existing authenticated wrapper; the real catalogue
        # remains read-only and never calls this acquisition function.
        require_model_acquisition()
        downloads.append("synthetic-model")
        return {"services": []}, 0

    monkeypatch.setattr(services["tts_catalogue"], "snapshot", synthetic_missing_model)
    try:
        response = app.test_client().post("/api/v1/auth/bootstrap", json={"token": launch})
        assert response.status_code == 200
        account = auth.initial_account()
        user_id, revision = account.user_id, account.auth_revision
        scopes = ALL_SCOPES if authority == "administrator" else ALL_SCOPES - {"app.admin"}
        if authority == "user":
            member = AccountCommands(database).create_user(
                AccountActor(user_id, revision, ALL_SCOPES),
                login_name="synthetic-member",
                display_name="Synthetic member",
            )
            user_id, revision = member.user_id, member.auth_revision
            # Isolate the download role check while named-user access stays closed.
            monkeypatch.setattr(auth, "solo_access_allowed", lambda _uid, _rev: True)
        _record, raw = auth.create_api_token(
            "synthetic download actor", scopes=scopes, user_id=user_id, auth_revision=revision
        )
        with execution_actor_scope(HostActor("pandrator-cli")), host_model_acquisition_scope():
            response = app.test_client().get(
                "/api/v1/services/tts",
                headers={"Authorization": f"Bearer {raw}"},
            )
            require_model_acquisition()
        allowed = authority == "administrator"
        assert response.status_code == (200 if allowed else 403), response.get_json()
        assert downloads == (["synthetic-model"] if allowed else [])
        if not allowed:
            assert response.get_json()["error"]["code"] == "model_acquisition_required"
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()
    finally:
        services["quick_transcriptions"].stop_maintenance()
        database.dispose()
