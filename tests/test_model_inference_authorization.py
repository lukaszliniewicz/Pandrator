"""Synthetic database checks for current inference and acquisition authority."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    model_inference_uses_prepared_endpoint,
    require_model_acquisition,
    require_prepared_model_inference,
)
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    execution_actor_scope,
)
from pandrator.web.accounts.model_acquisition import model_acquisition_for_actor
from pandrator.web.accounts.models import User
from pandrator.web.database import Database, upgrade_database
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, utcnow


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "model-inference-authorization.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        yield database
    finally:
        database.dispose()


def _actor(
    database: Database,
    *,
    scopes: frozenset[str],
    is_instance_admin: bool,
) -> UserActor:
    with database.immediate_session() as session:
        user = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert user is not None
        credential = session.get(UserCredential, user.id)
        assert credential is not None
        user.is_instance_admin = is_instance_admin
        user_id, revision = user.id, credential.auth_revision

    store = BrowserSessionStore(database)
    issued = store.issue(
        user_id,
        revision,
        subject="owner",
        principal_kind="owner_session",
        scopes=scopes,
        idle_seconds=3600,
        absolute_seconds=7200,
    )
    principal = store.resolve(issued.raw_token)
    assert principal is not None
    return UserActor(
        principal.user_id,
        principal.auth_revision,
        principal.subject,
        principal.kind,
        principal.scopes,
        None,
        principal.session_id,
        principal.client_id,
    )


@pytest.mark.parametrize(
    ("is_instance_admin", "scopes", "uses_prepared_endpoint", "can_acquire"),
    [
        (True, frozenset({"app.run", "app.admin"}), False, True),
        (True, frozenset({"app.run"}), True, False),
        (False, frozenset({"app.run"}), True, False),
    ],
)
def test_role_and_scopes_separate_prepared_inference_from_acquisition(
    database: Database,
    is_instance_admin: bool,
    scopes: frozenset[str],
    uses_prepared_endpoint: bool,
    can_acquire: bool,
):
    actor = _actor(
        database,
        scopes=scopes,
        is_instance_admin=is_instance_admin,
    )

    with execution_actor_scope(actor), model_acquisition_for_actor(database):
        assert model_inference_uses_prepared_endpoint() is uses_prepared_endpoint
        assert require_prepared_model_inference() is None
        if can_acquire:
            assert require_model_acquisition() is None
        else:
            with pytest.raises(ModelAcquisitionDenied):
                require_model_acquisition()


def test_missing_app_run_scope_is_denied_instead_of_downgraded(database: Database):
    actor = _actor(
        database,
        scopes=frozenset({"app.admin"}),
        is_instance_admin=True,
    )
    with execution_actor_scope(actor), model_acquisition_for_actor(database):
        with pytest.raises(ModelAcquisitionDenied):
            model_inference_uses_prepared_endpoint()
        with pytest.raises(ModelAcquisitionDenied):
            require_prepared_model_inference()


def test_falsely_claimed_app_admin_is_denied_by_current_actor_authorization(
    database: Database,
):
    actor = _actor(
        database,
        scopes=frozenset({"app.run"}),
        is_instance_admin=False,
    )
    falsely_elevated = UserActor(
        actor.user_id,
        actor.auth_revision,
        actor.subject,
        actor.principal_kind,
        frozenset({"app.run", "app.admin"}),
        actor.token_id,
        actor.browser_session_id,
        actor.client_id,
    )

    with execution_actor_scope(falsely_elevated), model_acquisition_for_actor(database):
        with pytest.raises(ModelAcquisitionDenied):
            model_inference_uses_prepared_endpoint()
        with pytest.raises(ModelAcquisitionDenied):
            require_prepared_model_inference()
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()


def test_verified_host_is_authorized_and_missing_actor_is_denied(database: Database):
    with execution_actor_scope(HostActor("pandrator-cli")), model_acquisition_for_actor(database):
        assert model_inference_uses_prepared_endpoint() is False
        assert require_prepared_model_inference() is None
        assert require_model_acquisition() is None

    with execution_actor_scope(None), model_acquisition_for_actor(database):
        with pytest.raises(ModelAcquisitionDenied):
            model_inference_uses_prepared_endpoint()
        with pytest.raises(ModelAcquisitionDenied):
            require_prepared_model_inference()
        with pytest.raises(ModelAcquisitionDenied):
            require_model_acquisition()


def test_current_credential_revocation_removes_inference_authority(database: Database):
    actor = _actor(
        database,
        scopes=frozenset({"app.run"}),
        is_instance_admin=True,
    )
    assert actor.browser_session_id is not None

    with execution_actor_scope(actor), model_acquisition_for_actor(database):
        assert model_inference_uses_prepared_endpoint() is True
        assert require_prepared_model_inference() is None
        with database.immediate_session() as session:
            credential = session.get(BrowserAuthSession, actor.browser_session_id)
            assert credential is not None
            credential.revoked_at = utcnow()
        with pytest.raises(ModelAcquisitionDenied):
            model_inference_uses_prepared_endpoint()
        with pytest.raises(ModelAcquisitionDenied):
            require_prepared_model_inference()


def test_worker_lease_takeover_invalidates_captured_inference_authority(
    database: Database,
):
    actor = _actor(
        database,
        scopes=frozenset({"app.run"}),
        is_instance_admin=True,
    )
    queue = JobQueue(database)
    with execution_actor_scope(actor):
        job = queue.enqueue("synthetic.model.inference", {}, max_attempts=3)
    first_claim = queue.claim("inference-worker-old")
    assert first_claim is not None
    assert first_claim.id == job.id
    old_lease: JobLease = {
        "job_id": first_claim.id,
        "owner_id": "inference-worker-old",
        "lease_generation": first_claim.lease_generation,
    }
    token = current_job_lease.set(old_lease)
    try:
        with execution_actor_scope(actor), model_acquisition_for_actor(database):
            assert model_inference_uses_prepared_endpoint() is True
            assert require_prepared_model_inference() is None

            with database.immediate_session() as session:
                row = session.get(Job, job.id)
                assert row is not None
                row.lease_expires_at = utcnow() - timedelta(seconds=1)
            replacement = queue.claim("inference-worker-new")
            assert replacement is not None
            assert replacement.id == job.id
            assert replacement.lease_generation > first_claim.lease_generation

            with pytest.raises(ModelAcquisitionDenied):
                model_inference_uses_prepared_endpoint()
            with pytest.raises(ModelAcquisitionDenied):
                require_prepared_model_inference()
    finally:
        current_job_lease.reset(token)
