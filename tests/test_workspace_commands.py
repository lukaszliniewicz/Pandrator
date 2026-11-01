"""Workspace aggregate transitions, credential enrollment and removal integrity."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from pandrator.web.access.contracts import AccessDenied, WorkspaceNotFound
from pandrator.web.access.workspace_policy import require_workspace_access
from pandrator.web.accounts.auth_models import BrowserAuthSession
from pandrator.web.accounts.credential_resources import CredentialOwner, resources_for_owner
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.accounts.workspace_commands import WorkspaceCommands
from pandrator.web.accounts.workspace_contracts import WorkspaceCommandError
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import ApiToken, AutomationClient, AutomationEnrollmentGrant
from pandrator.web.projects.models import Project, ProjectMembership
from tests.test_credential_resource_storage import GRANTS
from tests.test_workspace_policy import SCOPES, CoreCase, audit, browser, present, snapshot, token
from tests.test_workspace_policy import core as core


def test_create_team_is_shared_and_only_current_credential_enrolls(core: CoreCase) -> None:
    context = core.contexts["member"]
    old = token(core, "member")
    with core.database.immediate_session() as session:
        result = core.commands.create(session, context, name="  Shared team  ")
        assert result["name"] == "Shared team"
        assert (result["kind"], result["role"], result["revision"]) == ("team", "manager", 1)
        workspace = session.get(Workspace, result["id"])
        assert workspace is not None and workspace.personal_owner_user_id is None
        assert (
            present(session.get(WorkspaceMembership, (result["id"], core.users["member"]))).role
            == "admin"
        )
        assert (
            result["id"]
            in resources_for_owner(
                session, CredentialOwner.TOKEN, present(context.actor.token_id)
            ).workspace_ids
        )
        assert (
            result["id"]
            not in resources_for_owner(
                session, CredentialOwner.TOKEN, present(old.actor.token_id)
            ).workspace_ids
        )
        assert (
            session.scalar(select(Project.id).where(Project.workspace_id == result["id"])) is None
        )
    with core.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=result["id"], user_id=core.users["manager"], role="admin"
            )
        )
    manager = token(core, "manager", workspace_ids=[result["id"]])
    with core.database.immediate_session() as session:
        core.commands.remove_member(
            session, manager, result["id"], user_id=core.users["member"], expected_revision=1
        )
    with core.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            require_workspace_access(session, context, result["id"])


def test_interactive_browser_needs_no_resource_rows(core: CoreCase) -> None:
    context = browser(core, "member")
    with core.database.immediate_session() as session:
        created = core.commands.create(session, context, name="Browser team")
        assert (
            resources_for_owner(
                session, CredentialOwner.BROWSER, present(context.actor.browser_session_id)
            ).workspace_ids
            == frozenset()
        )
        assert require_workspace_access(session, context, created["id"]).role == "admin"


def test_old_token_and_manager_bootstrap_cannot_enroll(core: CoreCase) -> None:
    old = token(core, "manager", scopes=frozenset({"app.read", "app.write"}))
    before = snapshot(core)
    with pytest.raises(AccessDenied), core.database.immediate_session() as session:
        core.commands.create(session, old, name="Denied")
    assert snapshot(core) == before


def test_client_bound_creation_enrolls_only_live_current_client_and_token(core: CoreCase) -> None:
    client_id = new_id()
    with core.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic client",
                subject=f"client:{client_id}",
                user_id=core.users["member"],
                user_auth_revision=1,
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id=new_id(),
                canonical_origin="https://invalid.example",
                created_by="test",
            )
        )
    context = token(core, "member", client_id=client_id, workspace_ids=[core.homes["member"]])
    with core.database.immediate_session() as session:
        created = core.commands.create(session, context, name="Client team")
        for kind, identifier in (
            (CredentialOwner.TOKEN, context.actor.token_id),
            (CredentialOwner.CLIENT, client_id),
        ):
            assert (
                created["id"]
                in resources_for_owner(session, kind, present(identifier)).workspace_ids
            )
        assert (
            created["id"]
            not in resources_for_owner(
                session, CredentialOwner.TOKEN, present(core.contexts["member"].actor.token_id)
            ).workspace_ids
        )


@pytest.mark.parametrize(
    "value,code",
    [(0, "invalid_revision"), (True, "invalid_revision"), (9, "workspace_revision_conflict")],
)
def test_revision_errors_have_zero_writes_or_audit(core: CoreCase, value: int, code: str) -> None:
    before = snapshot(core)
    with (
        pytest.raises(WorkspaceCommandError) as failure,
        core.database.immediate_session() as session,
    ):
        core.commands.rename(
            session, core.contexts["manager"], core.team, name="Changed", expected_revision=value
        )
    assert failure.value.code == code
    assert snapshot(core) == before


def test_last_active_manager_guard_ignores_inactive_manager(core: CoreCase) -> None:
    from pandrator.web.accounts.models import User

    with core.database.immediate_session() as session:
        session.execute(
            update(User).where(User.id == core.users["manager2"]).values(is_active=False)
        )
    before = snapshot(core)
    for method, kwargs in (
        ("set_member_role", {"user_id": core.users["manager"], "role": "member"}),
        ("remove_member", {"user_id": core.users["manager"]}),
        ("leave", {}),
    ):
        with (
            pytest.raises(WorkspaceCommandError) as failure,
            core.database.immediate_session() as session,
        ):
            getattr(core.commands, method)(
                session, core.contexts["manager"], core.team, expected_revision=1, **kwargs
            )
        assert failure.value.code == "last_workspace_manager"
        assert snapshot(core) == before


def test_rename_archive_restore_leave_and_protected_personal(core: CoreCase) -> None:
    manager = core.contexts["manager"]
    with core.database.immediate_session() as session:
        renamed = core.commands.rename(
            session, manager, core.team, name="Renamed", expected_revision=1
        )
        assert (renamed["name"], renamed["revision"]) == ("Renamed", 2)
        assert (
            core.commands.restore(session, manager, core.team, expected_revision=2)["revision"] == 2
        )
        assert (
            core.commands.rename(session, manager, core.team, name="Renamed", expected_revision=2)[
                "revision"
            ]
            == 2
        )
        assert (
            core.commands.archive(session, manager, core.team, expected_revision=2)["revision"] == 3
        )
        assert (
            core.commands.restore(session, manager, core.team, expected_revision=3)["revision"] == 4
        )
        left = core.commands.leave(session, core.contexts["member"], core.team, expected_revision=4)
        assert left == {
            "workspace_id": core.team,
            "user_id": core.users["member"],
            "revision": 5,
            "removed": True,
        }
    for method, kwargs in (("rename", {"name": "No"}), ("archive", {}), ("leave", {})):
        with (
            pytest.raises(WorkspaceCommandError) as failure,
            core.database.immediate_session() as session,
        ):
            getattr(core.commands, method)(
                session, manager, core.homes["manager"], expected_revision=1, **kwargs
            )
        assert failure.value.code == "personal_workspace_protected"


def test_audit_failure_rolls_back_membership_grants_and_workspace(core: CoreCase) -> None:

    def failing_audit(*args, **kwargs):
        audit(*args, **kwargs)
        raise RuntimeError("Synthetic audit sink failure")

    commands = WorkspaceCommands(audit=failing_audit)
    before = snapshot(core)
    with (
        pytest.raises(RuntimeError, match="Synthetic audit"),
        core.database.immediate_session() as session,
    ):
        commands.create(session, core.contexts["member"], name="Rolled back")
    assert snapshot(core) == before
    with pytest.raises(RuntimeError), core.database.immediate_session() as session:
        commands.remove_member(
            session,
            core.contexts["manager"],
            core.team,
            user_id=core.users["member"],
            expected_revision=1,
        )
    assert snapshot(core) == before
    with core.database.snapshot_session() as session:
        assert (
            core.team
            in resources_for_owner(
                session, CredentialOwner.TOKEN, present(core.contexts["member"].actor.token_id)
            ).workspace_ids
        )


def test_removal_clears_all_eight_grant_surfaces_stale_owners_and_legacy_memberships(
    core: CoreCase,
) -> None:
    now = utcnow()
    client_id, code_id, browser_id = (new_id(), new_id(), new_id())
    stale = token(core, "member")
    with core.database.immediate_session() as session:
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == stale.actor.token_id)
            .values(revoked_at=now, user_auth_revision=99)
        )
        session.add(
            AutomationClient(
                id=client_id,
                name="Stale client",
                subject=f"client:{client_id}",
                user_id=core.users["member"],
                user_auth_revision=99,
                revoked_at=now,
                allowed_scopes_json=[],
                target_instance_id=new_id(),
                canonical_origin="https://invalid.example",
                created_by="test",
            )
        )
        session.add(
            BrowserAuthSession(
                id=browser_id,
                user_id=core.users["member"],
                auth_revision=99,
                token_hash=new_id(),
                subject="stale-browser",
                principal_kind="owner_session",
                scopes_json=[],
                revoked_at=now,
                last_seen_at=now - timedelta(days=2),
                idle_expires_at=now - timedelta(days=1),
                expires_at=now - timedelta(days=1),
            )
        )
        other_project = Project(workspace_id=core.homes["member"], name="Other retained project")
        session.add(other_project)
        session.flush()
        session.add(
            AutomationEnrollmentGrant(
                id=code_id,
                client_id=client_id,
                code_hash=new_id(),
                code_prefix="test",
                scopes_json=[],
                code_challenge="synthetic",
                expires_at=now - timedelta(days=1),
                token_expires_at=now,
                consumed_at=now,
            )
        )
        session.flush()
        owner_ids = {
            "token_id": stale.actor.token_id,
            "client_id": client_id,
            "browser_session_id": browser_id,
            "code_grant_id": code_id,
        }
        for model, _table, owner_col, _owner_table, resource_col, _resource_table in GRANTS:
            owner_id = owner_ids[owner_col]
            team_resource = core.team if resource_col == "workspace_id" else core.project
            retained_resource = (
                core.homes["member"] if resource_col == "workspace_id" else other_project.id
            )
            if session.get(model, (owner_id, team_resource)) is None:
                session.add(model(**{owner_col: owner_id, resource_col: team_resource}))
            if session.get(model, (owner_id, retained_resource)) is None:
                session.add(model(**{owner_col: owner_id, resource_col: retained_resource}))
            session.flush()
        session.add_all(
            [
                ProjectMembership(
                    project_id=core.project, user_id=core.users["member"], role="manager"
                ),
                ProjectMembership(
                    project_id=other_project.id, user_id=core.users["member"], role="viewer"
                ),
            ]
        )
    with core.database.immediate_session() as session:
        core.commands.remove_member(
            session,
            core.contexts["manager"],
            core.team,
            user_id=core.users["member"],
            expected_revision=1,
        )
    with core.database.snapshot_session() as session:
        for model, _table, owner_col, _owner_table, resource_col, _resource_table in GRANTS:
            owner_id = owner_ids[owner_col]
            team_resource = core.team if resource_col == "workspace_id" else core.project
            retained_resource = (
                core.homes["member"] if resource_col == "workspace_id" else other_project.id
            )
            assert session.get(model, (owner_id, team_resource)) is None
            assert session.get(model, (owner_id, retained_resource)) is not None
        assert session.get(ApiToken, stale.actor.token_id) is not None
        assert session.get(AutomationClient, client_id) is not None
        assert session.get(AutomationEnrollmentGrant, code_id) is not None
        assert session.get(BrowserAuthSession, browser_id) is not None
        assert session.get(ProjectMembership, (core.project, core.users["member"])) is None
        assert session.get(ProjectMembership, (other_project.id, core.users["member"])) is not None
        assert (
            core.team
            in resources_for_owner(
                session, CredentialOwner.TOKEN, present(core.contexts["manager"].actor.token_id)
            ).workspace_ids
        )
