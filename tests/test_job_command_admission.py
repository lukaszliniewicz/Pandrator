"""Caller-owned admission checks for typed HTTP job commands."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update

from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    HostActor,
    actor_from_principal,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
)
from pandrator.web.job_commands import (
    BundleExportJobPayload,
    DiagnosticJobPayload,
    enqueue_bundle_export_in_session,
    enqueue_diagnostic_in_session,
    parse_diagnostic_payload,
)
from pandrator.web.models import AutomationClient, Job, JobEvent, new_id
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import SCOPES as ROUTE_SCOPES
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential
from tests.test_job_access_routes import no_network as no_network  # noqa: F401


def _job_side_effect_counts(database) -> tuple[int, int, int]:
    with database.snapshot_session() as session:
        return _job_side_effect_counts_in_session(session)


def _job_side_effect_counts_in_session(session) -> tuple[int, int, int]:
    with session.no_autoflush:
        return cast(
            tuple[int, int, int],
            tuple(
                session.scalar(select(func.count()).select_from(model)) or 0
                for model in (Job, JobActorRecord, JobEvent)
            ),
        )


def _project_only_credential(case):
    issued = credential(case, "editor", grant_team=False)
    with case.database.immediate_session() as session:
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(ApiTokenWorkspaceGrant.token_id == issued.token.id)
        )
        session.add(
            ApiTokenProjectGrant(
                token_id=issued.token.id,
                project_id=case.a.project_id,
            )
        )
    return issued


def _diagnostic(case, session, actor, session_id=None, command=None, queue=None, **kwargs):
    return enqueue_diagnostic_in_session(
        session,
        AccessContext(actor),
        queue or case.services.jobs,
        command or DiagnosticJobPayload(),
        session_id=session_id,
        **kwargs,
    )


def _bundle(case, session, actor, session_id, command=None, queue=None, **kwargs):
    return enqueue_bundle_export_in_session(
        session,
        AccessContext(actor),
        queue or case.services.jobs,
        command or BundleExportJobPayload(session_id=session_id),
        **kwargs,
    )


def test_diagnostic_parser_copies_input_and_drops_only_legacy_actor_claims():
    supplied = {
        "duration": 2,
        "echo": {"nested": [True, None, 2.5]},
        "actor_user_id": "forged-user",
        "actor_source": "forged-source",
    }

    command = parse_diagnostic_payload(supplied)

    assert supplied["actor_user_id"] == "forged-user"
    assert supplied["actor_source"] == "forged-source"
    assert command.model_dump(exclude_unset=True) == {
        "duration": 2.0,
        "echo": {"nested": [True, None, 2.5]},
    }
    assert parse_diagnostic_payload({}).model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {"duration": -0.1},
        {"duration": 30.1},
        {"duration": float("inf")},
        {"duration": float("nan")},
        {"duration": True},
        {"duration": "2"},
        {"echo": object()},
    ],
)
def test_diagnostic_payload_rejects_noncanonical_values(payload):
    with pytest.raises(ValidationError):
        parse_diagnostic_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": ""},
        {"session_id": "   "},
        {"session_id": " padded "},
        {"session_id": "x" * 81},
        {"session_id": "valid", "include_sources": 1},
        {"session_id": "valid", "unexpected": True},
    ],
)
def test_bundle_payload_has_only_trimmed_bounded_fields(payload):
    with pytest.raises(ValidationError):
        BundleExportJobPayload.model_validate(payload)


def test_unscoped_run_only_diagnostic_uses_actual_users_personal_workspace(case):
    caller = credential(case, "editor", scopes=frozenset({"app.run"}), grant_team=False)
    personal_id = case.personal[caller.actor.user_id]
    with case.database.snapshot_session() as session:
        personal_grants = set(
            session.scalars(
                select(ApiTokenWorkspaceGrant.workspace_id).where(
                    ApiTokenWorkspaceGrant.token_id == caller.token.id
                )
            )
        )
        assert personal_grants == {personal_id}
        user = session.get(User, caller.actor.user_id)
        assert user is not None and not user.is_instance_admin

    with case.database.immediate_session() as session:
        queued = _diagnostic(case, session, caller.actor, max_attempts=10)
        assert (queued.kind, queued.workspace_id, queued.project_id, queued.session_id) == (
            "noop",
            personal_id,
            None,
            None,
        )
        assert queued.payload_json == {}
        assert queued.max_attempts == 10

    with case.database.snapshot_session() as session:
        actor_record = session.get(JobActorRecord, queued.id)
        assert actor_record is not None
        assert actor_record.user_id == caller.actor.user_id
        assert actor_record.auth_revision == caller.actor.auth_revision


def test_project_diagnostics_and_bundle_exports_follow_current_run_roles(case):
    editor = credential(case, "editor")
    viewer = credential(case, "viewer")
    owner = credential(case, "owner")

    with case.database.immediate_session() as session:
        before = _job_side_effect_counts_in_session(session)
        with pytest.raises(AccessDenied):
            _diagnostic(case, session, viewer.actor, case.a.id)
        assert _job_side_effect_counts_in_session(session) == before

        stale_viewer_membership = session.get(
            ProjectMembership,
            (case.a.project_id, case.users["viewer"]),
        )
        assert stale_viewer_membership is not None
        assert stale_viewer_membership.role == "viewer"
        session.execute(
            update(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["viewer"],
            )
            .values(role="reviewer"),
            execution_options={"synchronize_session": False},
        )
        with pytest.raises(AccessDenied):
            _bundle(case, session, viewer.actor, case.a.id)
        assert stale_viewer_membership.role == "viewer"
        assert _job_side_effect_counts_in_session(session) == before

        session.execute(
            update(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["viewer"],
            )
            .values(role="manager"),
            execution_options={"synchronize_session": False},
        )
        with pytest.raises(AccessDenied):
            _bundle(case, session, viewer.actor, case.a.id)
        assert _job_side_effect_counts_in_session(session) == before

        # Processing rights come from workspace membership. Legacy project
        # roles remain viewing grants, including their old "manager" label.
        session.add_all(
            [
                WorkspaceMembership(
                    workspace_id=case.team, user_id=case.users[name], role="member"
                )
                for name in ("viewer", "editor")
            ]
        )
        session.flush()
        member_job = _bundle(case, session, viewer.actor, case.a.id)
        editor_job = _diagnostic(
            case,
            session,
            editor.actor,
            case.a.id,
            parse_diagnostic_payload(
                {"duration": 4, "actor_user_id": "ignored", "actor_source": "ignored"}
            ),
        )
        owner_job = _diagnostic(case, session, owner.actor, case.a.id)

        assert member_job.kind == "session.bundle.export"
        assert member_job.payload_json == {"session_id": case.a.id, "include_sources": True}
        assert (member_job.workspace_id, member_job.project_id, member_job.session_id) == (
            case.team,
            case.a.project_id,
            case.a.id,
        )
        assert editor_job.kind == owner_job.kind == "noop"
        assert editor_job.payload_json == {"duration": 4.0}
        assert "actor_user_id" not in editor_job.payload_json
        assert (editor_job.project_id, owner_job.project_id) == (case.a.project_id,) * 2


def test_project_only_token_grant_admits_its_session_but_not_private_work(case):
    caller = _project_only_credential(case)

    with case.database.immediate_session() as session:
        before = _job_side_effect_counts_in_session(session)
        queued = _diagnostic(case, session, caller.actor, case.a.id)
        assert (queued.workspace_id, queued.project_id, queued.session_id) == (
            case.team,
            case.a.project_id,
            case.a.id,
        )
        before = _job_side_effect_counts_in_session(session)
        with pytest.raises(AccessDenied):
            _diagnostic(case, session, caller.actor)
        assert _job_side_effect_counts_in_session(session) == before

    with case.database.immediate_session() as session:
        before = _job_side_effect_counts_in_session(session)
        session.execute(
            delete(ApiTokenProjectGrant).where(
                ApiTokenProjectGrant.token_id == caller.token.id,
                ApiTokenProjectGrant.project_id == case.a.project_id,
            )
        )
        with pytest.raises(SessionNotFound):
            _bundle(case, session, caller.actor, case.a.id)
        assert _job_side_effect_counts_in_session(session) == before


def test_outside_instance_admin_without_membership_cannot_override_content_policy(case):
    outsider = credential(case, "outsider", scopes=ROUTE_SCOPES | {"app.admin"})
    context = AccessContext(outsider.actor)
    assert ROUTE_SCOPES | {"app.admin"} <= outsider.actor.scopes
    with case.database.snapshot_session() as session:
        user = session.get(User, outsider.actor.user_id)
        assert user is not None and user.is_instance_admin
        assert session.get(WorkspaceMembership, (case.team, outsider.actor.user_id)) is None
    before = _job_side_effect_counts(case.database)

    with case.database.session() as session:
        pending = User(id=new_id(), display_name="Unflushed", is_active=True)
        session.add(pending)
        before_session = _job_side_effect_counts_in_session(session)
        with pytest.raises(SessionNotFound):
            _diagnostic(case, session, context.actor, case.a.id)
        assert pending in session.new
        assert _job_side_effect_counts_in_session(session) == before_session
        session.rollback()

    assert _job_side_effect_counts(case.database) == before


def test_client_project_grant_is_current_and_does_not_become_workspace_authority(case):
    client_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic job client",
                subject=client_id,
                redirect_uris_json=["http://localhost/callback"],
                allowed_scopes_json=["app.read", "app.run"],
                target_instance_id="synthetic",
                canonical_origin="http://localhost",
                created_by="test",
                user_id=case.users["editor"],
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(AutomationClientWorkspaceGrant(client_id=client_id, workspace_id=case.team))

    token, raw = case.auth.create_api_token(
        "Synthetic project-only client token",
        user_id=case.users["editor"],
        auth_revision=1,
        client_id=client_id,
        scopes={"app.read", "app.run"},
        resource_grants=CredentialResources(workspace_ids=frozenset({case.team})),
    )
    principal = case.auth.resolve_api_token(
        raw,
        network_zone="loopback",
        target_instance_id=None,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None
    with case.database.immediate_session() as session:
        session.execute(
            delete(AutomationClientWorkspaceGrant).where(
                AutomationClientWorkspaceGrant.client_id == client_id
            )
        )
        session.add(AutomationClientProjectGrant(client_id=client_id, project_id=case.a.project_id))
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(ApiTokenWorkspaceGrant.token_id == token.id)
        )
        session.add(ApiTokenProjectGrant(token_id=token.id, project_id=case.a.project_id))
        session.flush()
        queued = _diagnostic(case, session, actor, case.a.id)
        assert queued.project_id == case.a.project_id
        before = _job_side_effect_counts_in_session(session)
        with pytest.raises(AccessDenied):
            _diagnostic(case, session, actor)
        assert _job_side_effect_counts_in_session(session) == before

        session.execute(
            delete(AutomationClientProjectGrant).where(
                AutomationClientProjectGrant.client_id == client_id
            )
        )
        with pytest.raises(SessionNotFound):
            _bundle(case, session, actor, case.a.id)
        assert _job_side_effect_counts_in_session(session) == before


@pytest.mark.parametrize("max_attempts", [True, False, 0, 11, 1.0, "1", None])
def test_direct_enqueue_rejects_invalid_attempt_counts_before_queueing(case, max_attempts):
    caller = credential(case, "editor")
    before = _job_side_effect_counts(case.database)
    with case.database.immediate_session() as session:
        with pytest.raises(ValueError, match="max_attempts"):
            _diagnostic(case, session, caller.actor, case.a.id, max_attempts=max_attempts)
    assert _job_side_effect_counts(case.database) == before


def test_enqueuers_require_exact_context_and_command_types(case):
    caller = credential(case, "editor")
    context = AccessContext(caller.actor)

    class DerivedContext(AccessContext):
        pass

    class DerivedDiagnostic(DiagnosticJobPayload):
        pass

    class DerivedBundle(BundleExportJobPayload):
        pass

    with case.database.immediate_session() as session:
        with pytest.raises(TypeError, match="access context"):
            enqueue_diagnostic_in_session(
                session,
                DerivedContext(caller.actor),
                case.services.jobs,
                DiagnosticJobPayload(),
                session_id=case.a.id,
            )
        with pytest.raises(TypeError, match="job command"):
            enqueue_diagnostic_in_session(
                session,
                context,
                case.services.jobs,
                DerivedDiagnostic(),
                session_id=case.a.id,
            )
        with pytest.raises(TypeError, match="job command"):
            _bundle(case, session, caller.actor, case.a.id, DerivedBundle(session_id=case.a.id))


def test_caller_rollback_and_actor_scope_restoration_on_queue_failure(case):
    caller = credential(case, "editor")
    with case.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=case.team, user_id=case.users["editor"], role="member"
            )
        )
    host = HostActor("pandrator-cli")
    identifier = None

    with execution_actor_scope(host):
        with case.database.session() as session:
            queued = _diagnostic(case, session, caller.actor, case.a.id)
            identifier = queued.id
            assert current_execution_actor.get() == host
            session.rollback()
        assert current_execution_actor.get() == host
    assert current_execution_actor.get() is None
    with case.database.snapshot_session() as session:
        assert session.get(Job, identifier) is None
        assert session.get(JobActorRecord, identifier) is None
        assert session.scalar(select(JobEvent.id).where(JobEvent.job_id == identifier)) is None

    class FailingQueue:
        def enqueue_in_session(self, *_args, **_kwargs):
            assert current_execution_actor.get() == caller.actor
            raise RuntimeError("queue failure")

    with execution_actor_scope(host):
        with case.database.session() as session:
            with pytest.raises(RuntimeError, match="queue failure"):
                _bundle(case, session, caller.actor, case.a.id, queue=FailingQueue())
            assert current_execution_actor.get() == host
        assert current_execution_actor.get() == host
    assert current_execution_actor.get() is None
