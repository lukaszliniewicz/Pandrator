"""Disposable authority and ordering checks for purge governance and recovery."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import delete, event, select

from pandrator.web import session_purge as service_module
from pandrator.web.access import purge_policy as policy
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.job_policy import JobNotFound, job_visibility_clause, require_job_access
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.actor_authorization import ActorAuthorizationDenied
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.administration import AdministratorRequired
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_to_storage,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, WorkspaceMembership
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import ApiToken, Job, SessionPurge, SessionRecord, new_id, utcnow
from pandrator.web.projects.models import Project, ProjectMembership
from pandrator.web.session_purge import PurgeBlocked, SessionPurgeService
from tests.test_job_access_policy import _context, _job
from tests.test_job_access_policy import block_network_attempts as block_network_attempts
from tests.test_job_access_policy import case as case

READ = frozenset({"app.read"})
WRITE = frozenset({"app.read", "app.write"})


def _synthetic_actor():
    return UserActor(
        "synthetic-user", 1, "synthetic", "api_token", WRITE, token_id="synthetic-token"
    )


def _target(case):
    with case.database.session() as unit:
        identifier = unit.scalar(select(Project.source_session_id).where(Project.id == case.a))
        assert identifier is not None
        return identifier


def _manager(case, *, scopes=READ):
    return _context(case, case.owner, scopes=scopes, projects=(case.a,))


@contextmanager
def _leased(actor, lease):
    token = current_job_lease.set(lease)
    try:
        with execution_actor_scope(actor):
            yield
    finally:
        current_job_lease.reset(token)


@pytest.fixture
def completed(case):
    context = _manager(case, scopes=WRITE)
    target = _target(case)
    lease = {"job_id": new_id(), "owner_id": "synthetic-worker", "lease_generation": 3}
    stored = actor_to_storage(context.actor)
    assert stored is not None
    with case.database.session() as unit:
        unit.add(
            Job(
                id=lease["job_id"],
                kind="maintenance.session_purge",
                workspace_id=case.team,
                project_id=case.a,
                status="running",
                payload_json={"target_session_id": target},
                lease_owner=lease["owner_id"],
                lease_generation=3,
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
        )
        unit.flush()
        unit.add(JobActorRecord(job_id=lease["job_id"], **stored))
        unit.add(
            SessionPurge(
                session_id=target,
                state="complete",
                expected_revision=1,
                impact_token="token",
                manifest_json=["private-manifest-do-not-load"],
            )
        )
        unit.execute(delete(SessionRecord).where(SessionRecord.id == target))
    return SimpleNamespace(case=case, context=context, target=target, lease=lease)


def test_none_leaves_perform_zero_database_work():
    unit: Any = MagicMock()
    with execution_actor_scope(None):
        policy.require_purge_inspection_access(unit, "target")
        policy.require_purge_deletion_access(unit, "target", job_id="legacy", lease_generation=1)
        policy.require_purge_policy_administration(unit)
        policy.require_purge_sweep_access(unit)
    assert unit.mock_calls == []


def test_trusted_host_uses_existing_fresh_proof_without_user_content_lookup():
    unit: Any = MagicMock()
    with execution_actor_scope(HostActor("pandrator-cli")):
        policy.require_purge_inspection_access(unit, "target")
        policy.require_purge_deletion_access(unit, "target")
        policy.require_purge_policy_administration(unit)
        policy.require_purge_sweep_access(unit)
    unit.scalar.assert_not_called()
    unit.execute.assert_not_called()
    unit.get.assert_not_called()


def test_read_only_manager_inspects_but_deletion_needs_write_scope(case):
    target = _target(case)
    with execution_actor_scope(_manager(case).actor), case.database.session() as unit:
        policy.require_purge_inspection_access(unit, target)
        with pytest.raises(AccessDenied):
            policy.require_purge_deletion_access(unit, target)
    with execution_actor_scope(_manager(case, scopes=WRITE).actor), case.database.session() as unit:
        policy.require_purge_deletion_access(unit, target)


@pytest.mark.parametrize("role", ["member", "project_viewer", "foreign_admin"])
def test_content_read_does_not_grant_purge_governance(case, role):
    target = _target(case)
    user = (
        case.user if role == "member" else case.other if role == "project_viewer" else case.outsider
    )
    with case.database.session() as unit:
        if role == "member":
            unit.add(WorkspaceMembership(workspace_id=case.team, user_id=user, role="member"))
        else:
            unit.add(ProjectMembership(project_id=case.a, user_id=user, role="viewer"))
    scopes = WRITE | ({"app.admin"} if role == "foreign_admin" else set())
    context = _context(case, user, scopes=frozenset(scopes), projects=(case.a,))
    with execution_actor_scope(context.actor), case.database.session() as unit:
        with pytest.raises(AccessDenied):
            policy.require_purge_inspection_access(unit, target)
        with pytest.raises(AccessDenied):
            policy.require_purge_deletion_access(unit, target)


def test_invisible_purge_target_remains_not_found(case):
    context = _context(case, case.outsider, scopes=WRITE | {"app.admin"},
        workspaces=(case.personal[case.outsider],))
    with execution_actor_scope(context.actor), case.database.session() as unit:
        with pytest.raises(SessionNotFound):
            policy.require_purge_inspection_access(unit, _target(case))


@pytest.mark.parametrize("revoked", ["token", "role"])
def test_inspection_rechecks_current_authority(case, revoked):
    context = _manager(case, scopes=WRITE)
    target = _target(case)
    with execution_actor_scope(context.actor), case.database.session() as unit:
        policy.require_purge_inspection_access(unit, target)
        if revoked == "token":
            token = unit.get(ApiToken, context.actor.token_id)
            assert token is not None
            token.revoked_at = utcnow()
        else:
            member = unit.get(WorkspaceMembership, (case.team, case.owner))
            assert member is not None
            member.role = "member"
        unit.flush()
        with pytest.raises(AccessDenied):
            policy.require_purge_inspection_access(unit, target)


@pytest.mark.parametrize("role", ["manager", "member", "project_viewer"])
def test_generic_job_visibility_restricts_governance_but_keeps_content(case, role):
    user = case.owner if role == "manager" else case.user if role == "member" else case.other
    with case.database.session() as unit:
        if role == "member":
            unit.add(WorkspaceMembership(workspace_id=case.team, user_id=user, role="member"))
        elif role == "project_viewer":
            unit.add(ProjectMembership(project_id=case.a, user_id=user, role="viewer"))
        ordinary = _job(unit, case.team, project_id=case.a, author=user)
        maintenance = [
            _job(unit, case.team, project_id=case.a, author=user, kind=kind)
            for kind in ("maintenance.session_purge_preview", "maintenance.session_purge")
        ]
    context = _context(case, user, scopes=READ, projects=(case.a,))
    with case.database.session() as unit:
        assert require_job_access(unit, context, ordinary).id == ordinary
        for identifier in maintenance:
            if role == "manager":
                assert require_job_access(unit, context, identifier).id == identifier
            else:
                with pytest.raises(JobNotFound):
                    require_job_access(unit, context, identifier)


def test_lazy_governance_case_adds_no_per_job_roundtrips(case):
    context = _manager(case)
    with case.database.session() as unit:
        for _ in range(25):
            _job(unit, case.team, project_id=case.a, kind="maintenance.session_purge")
        _job(unit, case.team, project_id=case.a)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        with case.database.session() as unit:
            clause = job_visibility_clause(unit, context)
            statement = select(Job.id).where(clause)
            sql = str(
                statement.compile(
                    dialect=case.database.engine.dialect, compile_kwargs={"literal_binds": True}
                )
            )
            assert "CASE WHEN (jobs.kind IN" in sql and "ELSE 1 END" in sql
            before = len(statements)
            visible = unit.scalars(statement).all()
            assert len(visible) == 26 and len(statements) == before + 1
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)


def test_complete_journal_retry_proves_actual_lease_without_hydrating_manifest(completed):
    c = completed
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(c.case.database.engine, "before_cursor_execute", capture)
    try:
        with _leased(c.context.actor, c.lease), c.case.database.session() as unit:
            policy.require_purge_deletion_access(
                unit, c.target, job_id=c.lease["job_id"], lease_generation=3
            )
            assert not unit.identity_map
    finally:
        event.remove(c.case.database.engine, "before_cursor_execute", capture)
    assert all(
        "session_purges.manifest_json" not in statement
        and "jobs.result_json" not in statement
        and "sessions.name" not in statement
        for statement in statements
    )


def test_admitted_retry_survives_token_expiry(completed):
    c = completed
    with c.case.database.session() as unit:
        token = unit.get(ApiToken, c.context.actor.token_id)
        assert token is not None
        token.expires_at = utcnow() - timedelta(minutes=1)
    with _leased(c.context.actor, c.lease), c.case.database.session() as unit:
        policy.require_purge_deletion_access(
            unit, c.target, job_id=c.lease["job_id"], lease_generation=3
        )


@pytest.mark.parametrize(
    "defect",
    [
        "no_lease",
        "different_job",
        "generation",
        "owner",
        "expired_lease",
        "saved_actor",
        "revoked_token",
        "disabled_user",
        "reset_revision",
        "lost_scope",
        "lost_role",
        "journal_missing",
        "journal_failed",
        "kind",
        "target",
        "invalid_json",
        "rootless",
        "project",
        "workspace",
    ],
)
def test_complete_journal_retry_rejects_missing_or_mismatched_proof(completed, defect):
    c = completed
    lease = dict(c.lease)
    with c.case.database.session() as unit:
        job = unit.get(Job, c.lease["job_id"])
        assert job is not None
        if defect == "owner":
            lease["owner_id"] = "different-worker"
        elif defect == "expired_lease":
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
        elif defect == "saved_actor":
            actor = unit.get(JobActorRecord, job.id)
            assert actor is not None
            actor.auth_revision = 9
        elif defect == "revoked_token":
            token = unit.get(ApiToken, c.context.actor.token_id)
            assert token is not None
            token.revoked_at = utcnow()
        elif defect == "disabled_user":
            user = unit.get(User, c.context.actor.user_id)
            assert user is not None
            user.is_active = False
        elif defect == "reset_revision":
            credential = unit.get(UserCredential, c.context.actor.user_id)
            assert credential is not None
            credential.auth_revision += 1
        elif defect == "lost_scope":
            token = unit.get(ApiToken, c.context.actor.token_id)
            assert token is not None
            token.scopes_json = ["app.read"]
        elif defect == "lost_role":
            member = unit.get(WorkspaceMembership, (c.case.team, c.case.owner))
            assert member is not None
            member.role = "member"
        elif defect == "journal_missing":
            journal = unit.get(SessionPurge, c.target)
            assert journal is not None
            unit.delete(journal)
        elif defect == "journal_failed":
            journal = unit.get(SessionPurge, c.target)
            assert journal is not None
            journal.state = "failed"
        elif defect == "kind":
            job.kind = "ordinary.content"
        elif defect == "target":
            job.payload_json = {"target_session_id": "different"}
        elif defect == "invalid_json":
            unit.flush()
            unit.connection().exec_driver_sql(
                "UPDATE jobs SET payload_json = ? WHERE id = ?", ("{", job.id)
            )
        elif defect in {"rootless", "project", "workspace"}:
            # Simulate corrupt retained metadata in this disposable database.
            # Ordinary writes already reject changing these immutable roots.
            connection = unit.connection()
            names = (
                "pandrator_execution_scope_jobs_update",
                "pandrator_library_scope_jobs_update",
            )
            triggers = [
                connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).scalar_one()
                for name in names
            ]
            for name in names:
                connection.exec_driver_sql(f"DROP TRIGGER {name}")
            try:
                if defect == "rootless":
                    job.project_id = None
                elif defect == "project":
                    job.project_id = c.case.b
                else:
                    job.workspace_id = c.case.personal[c.case.owner]
                unit.flush()
            finally:
                for trigger in triggers:
                    connection.exec_driver_sql(trigger)
    supplied_id = "different-job" if defect == "different_job" else c.lease["job_id"]
    generation = 4 if defect == "generation" else 3
    with (
        _leased(c.context.actor, None if defect == "no_lease" else lease),
        c.case.database.session() as unit,
    ):
        with pytest.raises((AccessDenied, SessionNotFound)):
            policy.require_purge_deletion_access(
                unit, c.target, job_id=supplied_id, lease_generation=generation
            )


def test_unleased_browser_cannot_acknowledge_a_missing_session(completed):
    c = completed
    with execution_actor_scope(c.context.actor), c.case.database.session() as unit:
        with pytest.raises(SessionNotFound):
            policy.require_purge_deletion_access(unit, c.target)


def test_policy_mutation_requires_admin_write_and_sweep_rejects_every_user(case):
    for scopes, allowed in (
        (READ | {"app.admin"}, False),
        (frozenset({"app.admin", "app.write"}), True),
    ):
        context = _context(case, case.owner, scopes=frozenset(scopes))
        with execution_actor_scope(context.actor), case.database.session() as unit:
            if allowed:
                policy.require_purge_policy_administration(unit)
            else:
                with pytest.raises(AdministratorRequired):
                    policy.require_purge_policy_administration(unit)
            with pytest.raises(AccessDenied):
                policy.require_purge_sweep_access(unit)
    member = _context(case, case.user, scopes=WRITE, projects=(case.a,))
    with execution_actor_scope(member.actor), case.database.session() as unit:
        with pytest.raises(AdministratorRequired):
            policy.require_purge_policy_administration(unit)


def _mock_service():
    unit: Any = MagicMock()
    database: Any = SimpleNamespace(
        snapshot_session=Mock(return_value=nullcontext(unit)),
        immediate_session=Mock(return_value=nullcontext(unit)),
    )
    return SessionPurgeService(database, MagicMock()), database, unit


@pytest.mark.parametrize("operation", ["preview", "purge"])
def test_outer_authority_precedes_activity_storage(monkeypatch, operation):
    service, _database, _unit = _mock_service()
    activity = Mock()
    monkeypatch.setattr(service_module, "upload_activity", activity)
    guard = (
        "require_purge_inspection_access"
        if operation == "preview"
        else "require_purge_deletion_access"
    )
    monkeypatch.setattr(service_module, guard, Mock(side_effect=AccessDenied()))
    with execution_actor_scope(_synthetic_actor()), pytest.raises(AccessDenied):
        if operation == "preview":
            service.preview("target")
        else:
            service.purge("target", 1, "token")
    activity.assert_not_called()


@pytest.mark.parametrize("denial", [AccessDenied(), ActorAuthorizationDenied("revoked")])
def test_writer_auth_denials_keep_their_type_outside_storage_handler(monkeypatch, denial):
    service, _database, unit = _mock_service()
    monkeypatch.setattr(service_module, "upload_activity", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(
        service_module, "require_purge_deletion_access", Mock(side_effect=[None, denial])
    )
    with execution_actor_scope(_synthetic_actor()), pytest.raises(type(denial)) as caught:
        service.purge("target", 1, "token")
    assert caught.value is denial
    unit.get.assert_not_called()


def test_raw_filesystem_permission_error_keeps_existing_storage_mapping(monkeypatch):
    service, _database, _unit = _mock_service()
    monkeypatch.setattr(
        service_module, "upload_activity", Mock(side_effect=PermissionError("filesystem"))
    )
    with execution_actor_scope(None), pytest.raises(PurgeBlocked) as caught:
        service.purge("target", 1, "token")
    assert caught.value.blockers == ["unsafe_upload_activity:filesystem"]


def test_preview_writer_denial_precedes_preflight_catch(monkeypatch):
    service, _database, unit = _mock_service()
    unit.get.side_effect = [SimpleNamespace(revision=1), None]
    monkeypatch.setattr(
        service,
        "_inspect",
        Mock(return_value={"can_purge": True, "_retained": set(), "blockers": []}),
    )
    preflight = Mock()
    monkeypatch.setattr(service, "_preflight_delete", preflight)
    monkeypatch.setattr(
        service_module, "require_purge_inspection_access", Mock(side_effect=[None, AccessDenied()])
    )
    with execution_actor_scope(_synthetic_actor()), pytest.raises(AccessDenied):
        service._preview("target", [])
    preflight.assert_not_called()
    assert unit.get.call_count == 2


def test_none_outer_paths_add_no_snapshot_and_committed_cleanup_stays_structural(monkeypatch):
    service, database, unit = _mock_service()
    monkeypatch.setattr(service_module, "upload_activity", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(service, "_preview", Mock(return_value={"can_purge": True}))
    monkeypatch.setattr(service, "_purge_locked", Mock(return_value={"state": "complete"}))
    with execution_actor_scope(None):
        service.preview("target")
        service.purge("target", 1, "token")
    database.snapshot_session.assert_not_called()
    unit.get.return_value = SimpleNamespace(state="complete")
    forbidden = Mock(side_effect=AssertionError("Committed cleanup has no new actor gates"))
    monkeypatch.setattr(service_module, "require_purge_deletion_access", forbidden)
    monkeypatch.setattr(service_module, "require_purge_inspection_access", forbidden)
    with execution_actor_scope(_synthetic_actor()):
        assert service._finish_locked("target") == {"state": "complete"}
    forbidden.assert_not_called()


def test_user_sweep_denies_before_policy_or_global_queries(monkeypatch):
    service, _database, unit = _mock_service()
    metadata = Mock()
    monkeypatch.setattr(service, "policy", metadata)
    with execution_actor_scope(_synthetic_actor()), pytest.raises(AccessDenied):
        service.sweep()
    metadata.assert_not_called()
    unit.get.assert_not_called()
    unit.execute.assert_not_called()
    unit.scalars.assert_not_called()


def test_update_policy_denies_before_settings_hydration(monkeypatch):
    service, _database, unit = _mock_service()
    monkeypatch.setattr(
        service_module,
        "require_purge_policy_administration",
        Mock(side_effect=AdministratorRequired()),
    )
    with pytest.raises(AdministratorRequired):
        service.update_policy(0, 30)
    unit.get.assert_not_called()


@pytest.mark.parametrize("present", [False, True])
def test_extra_claim_job_ownership_check_is_present_actor_only(monkeypatch, present):
    service, _database, unit = _mock_service()
    job = SimpleNamespace(
        kind="maintenance.session_purge",
        payload_json={"target_session_id": "target"},
        status="running",
        lease_generation=3,
    )
    if present:
        job.project_id, job.workspace_id = "wrong-project", "workspace"
    unit.get.side_effect = [None, job, SimpleNamespace(revision=1)]
    unit.execute.return_value.one_or_none.return_value = SimpleNamespace(
        project_id="actual-project", workspace_id="workspace"
    )
    monkeypatch.setattr(service_module, "require_purge_deletion_access", Mock())
    monkeypatch.setattr(
        service,
        "_inspect",
        Mock(return_value={"impact_token": "token", "blockers": ["controlled_stop"]}),
    )
    with (
        execution_actor_scope(_synthetic_actor() if present else None),
        pytest.raises(PurgeBlocked) as caught,
    ):
        service._purge_locked("target", 1, "token", job_id="job", lease_generation=3)
    assert caught.value.blockers == [
        "purge_job_cancelled_or_unclaimed" if present else "controlled_stop"
    ]
    assert unit.execute.call_count == (1 if present else 0)


def test_complete_journal_does_not_acknowledge_an_invisible_live_target(completed):
    c = completed
    # The retained job/journal still belongs to allowed project A, but a live
    # target in project B is outside this credential's project-A ceiling.
    with c.case.database.session() as unit:
        unit.add(SessionRecord(id=c.target, project_id=c.case.b, name="Live foreign target"))
    with _leased(c.context.actor, c.lease), c.case.database.session() as unit:
        with pytest.raises(SessionNotFound):
            policy.require_purge_deletion_access(
                unit,
                c.target,
                job_id=c.lease["job_id"],
                lease_generation=3,
            )
