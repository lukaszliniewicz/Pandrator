"""Target-only invitation admission, revocation and non-resurrecting replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update

from pandrator.web.access.contracts import AccessDenied, WorkspaceNotFound
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialOwner, resources_for_owner
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.workspace_commands import WorkspaceCommands
from pandrator.web.accounts.workspace_contracts import WorkspaceCommandError
from pandrator.web.accounts.workspace_models import WorkspaceInvitation
from pandrator.web.accounts.workspace_queries import WorkspaceQueries
from pandrator.web.model_base import new_id
from pandrator.web.models import ApiToken, AppSetting, AutomationClient
from tests.test_workspace_policy import SCOPES, CoreCase, audit, present, revision, token
from tests.test_workspace_policy import core as core

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
PUBLIC_INVITATION_KEYS = {
    "id",
    "workspace_id",
    "workspace_name",
    "user_id",
    "display_name",
    "invited_by_user_id",
    "invited_by_display_name",
    "role",
    "state",
    "revision",
    "created_at",
    "expires_at",
    "responded_at",
}


def invite(
    core: CoreCase, *, who: str = "target", by: str = "manager", role: str = "member"
) -> dict:
    commands = WorkspaceCommands(audit=audit, clock=lambda: NOW)
    with core.database.immediate_session() as session:
        return commands.invite(
            session,
            core.contexts[by],
            core.team,
            user_id=core.users[who],
            role=role,
            expected_revision=revision(core),
        )


def invitation_snapshot(core: CoreCase) -> tuple:
    with core.database.snapshot_session() as session:
        return tuple(
            (
                tuple(session.execute(select(model.__table__)).all())
                for model in (WorkspaceInvitation, Workspace, WorkspaceMembership, AppSetting)
            )
        )


def test_inbox_preaccept_metadata_and_target_only_acceptance(core: CoreCase) -> None:
    invitation = invite(core, role="manager")
    assert set(invitation) == PUBLIC_INVITATION_KEYS
    assert invitation["role"] == "manager" and invitation["state"] == "pending"
    with core.database.snapshot_session() as session:
        inbox = WorkspaceQueries.inbox(session, core.contexts["target"])
        assert [row["id"] for row in inbox] == [invitation["id"]]
        assert set(inbox[0]) == PUBLIC_INVITATION_KEYS
        assert inbox[0]["workspace_name"] == "Private team"
        assert core.team not in {
            row["id"] for row in WorkspaceQueries.list_workspaces(session, core.contexts["target"])
        }
        assert WorkspaceQueries.inbox(session, core.contexts["outsider"]) == []
    before = invitation_snapshot(core)
    for action in ("accept_invitation", "decline_invitation"):
        with (
            pytest.raises(WorkspaceCommandError) as failure,
            core.database.immediate_session() as session,
        ):
            getattr(core.commands, action)(session, core.contexts["outsider"], invitation["id"])
        assert failure.value.code == "invitation_not_found"
        assert invitation_snapshot(core) == before
    commands = WorkspaceCommands(audit=audit, clock=lambda: NOW)
    with core.database.immediate_session() as session:
        accepted = commands.accept_invitation(session, core.contexts["target"], invitation["id"])
        assert accepted["state"] == "accepted" and accepted["revision"] == 2
        assert (
            present(session.get(WorkspaceMembership, (core.team, core.users["target"]))).role
            == "admin"
        )
        assert (
            core.team
            in resources_for_owner(
                session, CredentialOwner.TOKEN, present(core.contexts["target"].actor.token_id)
            ).workspace_ids
        )


def test_inbox_requires_actual_personal_home_grant(core: CoreCase) -> None:
    invite(core)
    no_home = token(core, "target", workspace_ids=[core.team])
    with core.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            WorkspaceQueries.inbox(session, no_home)


def test_accept_is_explicit_enrollment_of_current_token_only(core: CoreCase) -> None:
    invitation = invite(core)
    current = token(core, "target", workspace_ids=[core.homes["target"]])
    old = token(core, "target", workspace_ids=[core.homes["target"]])
    commands = WorkspaceCommands(audit=audit, clock=lambda: NOW)
    with core.database.immediate_session() as session:
        commands.accept_invitation(session, current, invitation["id"])
        assert (
            core.team
            in resources_for_owner(
                session, CredentialOwner.TOKEN, present(current.actor.token_id)
            ).workspace_ids
        )
        assert (
            core.team
            not in resources_for_owner(
                session, CredentialOwner.TOKEN, present(old.actor.token_id)
            ).workspace_ids
        )
    before = invitation_snapshot(core)
    with pytest.raises(WorkspaceNotFound), core.database.immediate_session() as session:
        commands.accept_invitation(session, old, invitation["id"])
    assert invitation_snapshot(core) == before
    with core.database.immediate_session() as session:
        assert commands.accept_invitation(session, current, invitation["id"])["state"] == "accepted"
    assert invitation_snapshot(core) == before


def test_client_bound_accept_enrolls_token_and_current_client(core: CoreCase) -> None:
    invitation = invite(core)
    client_id = new_id()
    with core.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic target client",
                subject=f"client:{client_id}",
                user_id=core.users["target"],
                user_auth_revision=1,
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id=new_id(),
                canonical_origin="https://invalid.example",
                created_by="test",
            )
        )
    current = token(core, "target", client_id=client_id, workspace_ids=[core.homes["target"]])
    with core.database.immediate_session() as session:
        WorkspaceCommands(audit=audit, clock=lambda: NOW).accept_invitation(
            session, current, invitation["id"]
        )
        assert (
            core.team
            in resources_for_owner(
                session, CredentialOwner.TOKEN, present(current.actor.token_id)
            ).workspace_ids
        )
        assert (
            core.team
            in resources_for_owner(
                session, CredentialOwner.CLIENT, present(client_id)
            ).workspace_ids
        )


@pytest.mark.parametrize(
    "reason",
    [
        "expired",
        "inviter_auth",
        "inviter_inactive",
        "inviter_demoted",
        "inviter_removed",
        "archived",
    ],
)
def test_invalid_invitation_never_changes_membership_grants_audit_or_status(
    core: CoreCase, reason: str
) -> None:
    invitation = invite(core)
    with core.database.immediate_session() as session:
        if reason == "inviter_auth":
            session.execute(
                update(UserCredential)
                .where(UserCredential.user_id == core.users["manager"])
                .values(auth_revision=2)
            )
        elif reason == "inviter_inactive":
            session.execute(
                update(User).where(User.id == core.users["manager"]).values(is_active=False)
            )
        elif reason == "inviter_demoted":
            session.execute(
                update(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == core.team,
                    WorkspaceMembership.user_id == core.users["manager"],
                )
                .values(role="member")
            )
        elif reason == "inviter_removed":
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == core.team,
                    WorkspaceMembership.user_id == core.users["manager"],
                )
            )
        elif reason == "archived":
            session.execute(
                update(Workspace).where(Workspace.id == core.team).values(state="archived")
            )
    before = invitation_snapshot(core)
    clock = NOW + timedelta(days=8) if reason == "expired" else NOW
    with (
        pytest.raises(WorkspaceCommandError) as failure,
        core.database.immediate_session() as session,
    ):
        WorkspaceCommands(audit=audit, clock=lambda: clock).accept_invitation(
            session, core.contexts["target"], invitation["id"]
        )
    assert failure.value.code == "invitation_unavailable"
    assert invitation_snapshot(core) == before


def test_old_no_enrollment_scope_and_bootstrap_principal_cannot_accept(core: CoreCase) -> None:
    invitation = invite(core)
    no_enroll = token(core, "target", scopes=frozenset({"app.read", "app.write"}))
    before = invitation_snapshot(core)
    with pytest.raises(AccessDenied), core.database.immediate_session() as session:
        core.commands.accept_invitation(session, no_enroll, invitation["id"])
    assert invitation_snapshot(core) == before
    from pandrator.web.access.contracts import AccessContext
    from pandrator.web.accounts.execution_actor import UserActor

    bootstrap = token(core, "target")
    with core.database.immediate_session() as session:
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == bootstrap.actor.token_id)
            .values(principal_kind="manager_bootstrap")
        )
    actor = bootstrap.actor
    context = AccessContext(
        UserActor(
            actor.user_id,
            actor.auth_revision,
            actor.subject,
            "manager_bootstrap",
            actor.scopes,
            token_id=actor.token_id,
        )
    )
    with pytest.raises(AccessDenied), core.database.immediate_session() as session:
        core.commands.accept_invitation(session, context, invitation["id"])
    assert invitation_snapshot(core) == before


def test_accepted_replay_after_removal_cannot_restore_access(core: CoreCase) -> None:
    invitation = invite(core)
    target = token(core, "target", workspace_ids=[core.homes["target"]])
    commands = WorkspaceCommands(audit=audit, clock=lambda: NOW)
    with core.database.immediate_session() as session:
        commands.accept_invitation(session, target, invitation["id"])
    with core.database.immediate_session() as session:
        commands.remove_member(
            session,
            core.contexts["manager"],
            core.team,
            user_id=core.users["target"],
            expected_revision=3,
        )
    before = invitation_snapshot(core)
    with pytest.raises(WorkspaceNotFound), core.database.immediate_session() as session:
        commands.accept_invitation(session, target, invitation["id"])
    assert invitation_snapshot(core) == before
    with core.database.snapshot_session() as session:
        assert session.get(WorkspaceMembership, (core.team, core.users["target"])) is None
        assert (
            core.team
            not in resources_for_owner(
                session, CredentialOwner.TOKEN, present(target.actor.token_id)
            ).workspace_ids
        )


def test_pending_accept_preserves_existing_membership_role(core: CoreCase) -> None:
    invitation = invite(core, role="manager")
    with core.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(workspace_id=core.team, user_id=core.users["target"], role="member")
        )
    with core.database.immediate_session() as session:
        WorkspaceCommands(audit=audit, clock=lambda: NOW).accept_invitation(
            session, core.contexts["target"], invitation["id"]
        )
        assert (
            present(session.get(WorkspaceMembership, (core.team, core.users["target"]))).role
            == "member"
        )


def test_replacement_decline_revoke_and_finite_transitions(core: CoreCase) -> None:
    old = invite(core)
    current = invite(core, role="manager")
    with core.database.snapshot_session() as session:
        assert present(session.get(WorkspaceInvitation, old["id"])).state == "revoked"
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceInvitation)
                .where(
                    WorkspaceInvitation.workspace_id == core.team,
                    WorkspaceInvitation.user_id == core.users["target"],
                    WorkspaceInvitation.state == "pending",
                )
            )
            == 1
        )
    commands = WorkspaceCommands(audit=audit, clock=lambda: NOW)
    with core.database.immediate_session() as session:
        assert (
            commands.decline_invitation(session, core.contexts["target"], current["id"])["state"]
            == "declined"
        )
    assert revision(core) == 3
    for action in ("accept_invitation", "decline_invitation"):
        with (
            pytest.raises(WorkspaceCommandError) as failure,
            core.database.immediate_session() as session,
        ):
            getattr(commands, action)(session, core.contexts["target"], current["id"])
        assert failure.value.code == "invitation_unavailable"
    newer = invite(core)
    with core.database.immediate_session() as session:
        assert (
            commands.revoke_invitation(
                session,
                core.contexts["manager"],
                core.team,
                invitation_id=newer["id"],
                expected_revision=4,
            )["state"]
            == "revoked"
        )
    with (
        pytest.raises(WorkspaceCommandError) as failure,
        core.database.immediate_session() as session,
    ):
        commands.revoke_invitation(
            session,
            core.contexts["manager"],
            core.team,
            invitation_id=newer["id"],
            expected_revision=5,
        )
    assert failure.value.code == "invitation_unavailable"


@pytest.mark.parametrize("operation", ["demote", "remove", "archive"])
def test_manager_demotion_removal_archive_revoke_pending_issued_invitations(
    core: CoreCase, operation: str
) -> None:
    invitation = invite(core, by="manager2")
    with core.database.immediate_session() as session:
        if operation == "demote":
            core.commands.set_member_role(
                session,
                core.contexts["manager"],
                core.team,
                user_id=core.users["manager2"],
                role="member",
                expected_revision=2,
            )
        elif operation == "remove":
            core.commands.remove_member(
                session,
                core.contexts["manager"],
                core.team,
                user_id=core.users["manager2"],
                expected_revision=2,
            )
        else:
            core.commands.archive(session, core.contexts["manager"], core.team, expected_revision=2)
    with core.database.snapshot_session() as session:
        assert present(session.get(WorkspaceInvitation, invitation["id"])).state == "revoked"


def test_removal_revokes_invitations_targeting_removed_member(core: CoreCase) -> None:
    invitation = invite(core)
    with core.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(workspace_id=core.team, user_id=core.users["target"], role="member")
        )
    with core.database.immediate_session() as session:
        core.commands.remove_member(
            session,
            core.contexts["manager"],
            core.team,
            user_id=core.users["target"],
            expected_revision=2,
        )
    with core.database.snapshot_session() as session:
        assert present(session.get(WorkspaceInvitation, invitation["id"])).state == "revoked"


def test_expired_pending_is_read_projection_without_writes(core: CoreCase) -> None:
    past = NOW.replace(year=2000)
    with core.database.immediate_session() as session:
        invitation = WorkspaceCommands(audit=audit, clock=lambda: past).invite(
            session,
            core.contexts["manager"],
            core.team,
            user_id=core.users["target"],
            role="member",
            expected_revision=1,
        )
    before = invitation_snapshot(core)
    with core.database.snapshot_session() as session:
        assert WorkspaceQueries.inbox(session, core.contexts["target"])[0]["state"] == "expired"
        assert (
            WorkspaceQueries.invitations(session, core.contexts["manager"], core.team)[0]["state"]
            == "expired"
        )
        assert present(session.get(WorkspaceInvitation, invitation["id"])).state == "pending"
    assert invitation_snapshot(core) == before


def test_invitation_audit_failure_rolls_back_acceptance(core: CoreCase) -> None:
    invitation = invite(core)

    def fail(*args, **kwargs):
        audit(*args, **kwargs)
        raise RuntimeError("Synthetic audit failure")

    before = invitation_snapshot(core)
    context = token(core, "target", workspace_ids=[core.homes["target"]])
    with pytest.raises(RuntimeError), core.database.immediate_session() as session:
        WorkspaceCommands(audit=fail, clock=lambda: NOW).accept_invitation(
            session, context, invitation["id"]
        )
    assert invitation_snapshot(core) == before
    with core.database.snapshot_session() as session:
        assert (
            core.team
            not in resources_for_owner(
                session, CredentialOwner.TOKEN, present(context.actor.token_id)
            ).workspace_ids
        )


def test_read_only_manager_sees_invites_without_manage_scope(core: CoreCase) -> None:
    invitation = invite(core)
    context = token(core, "manager", scopes=frozenset({"app.read"}))
    with core.database.snapshot_session() as session:
        assert (
            WorkspaceQueries.invitations(session, context, core.team)[0]["id"] == invitation["id"]
        )
        assert WorkspaceQueries.search_users(session, context, core.team, "target")
    with pytest.raises(AccessDenied), core.database.immediate_session() as session:
        core.commands.revoke_invitation(
            session, context, core.team, invitation_id=invitation["id"], expected_revision=2
        )


def test_inbox_is_bounded_newest_first_and_contains_only_current_target(core: CoreCase) -> None:
    with core.database.immediate_session() as session:
        invitations = [
            WorkspaceInvitation(
                id=new_id(),
                workspace_id=core.team,
                user_id=core.users["target"],
                invited_by_user_id=core.users["manager"],
                inviter_auth_revision=1,
                role="member",
                state="accepted",
                revision=2,
                created_at=NOW + timedelta(minutes=index),
                expires_at=NOW + timedelta(days=7),
                responded_at=NOW,
            )
            for index in range(105)
        ]
        expected = [invitation.id for invitation in reversed(invitations)][0:100]
        session.add_all(invitations)
        session.add(
            WorkspaceInvitation(
                workspace_id=core.team,
                user_id=core.users["outsider"],
                invited_by_user_id=core.users["manager"],
                inviter_auth_revision=1,
                role="member",
                state="pending",
                revision=1,
                created_at=NOW + timedelta(days=1),
                expires_at=NOW + timedelta(days=7),
            )
        )
    with core.database.snapshot_session() as session:
        rows = WorkspaceQueries.inbox(session, core.contexts["target"])
        assert len(rows) == 100 and [row["id"] for row in rows] == expected
        assert {row["user_id"] for row in rows} == {core.users["target"]}


def test_foreign_workspace_invitation_ids_and_membership_target_checks(core: CoreCase) -> None:
    invitation = invite(core)
    with core.database.immediate_session() as session:
        another = core.commands.create(
            session, core.contexts["manager"], name="Other invitation scope"
        )
    before = invitation_snapshot(core)
    with (
        pytest.raises(WorkspaceCommandError) as failure,
        core.database.immediate_session() as session,
    ):
        core.commands.revoke_invitation(
            session,
            core.contexts["manager"],
            another["id"],
            invitation_id=invitation["id"],
            expected_revision=1,
        )
    assert failure.value.code == "invitation_not_found"
    assert invitation_snapshot(core) == before
    with (
        pytest.raises(WorkspaceCommandError) as failure,
        core.database.immediate_session() as session,
    ):
        core.commands.invite(
            session,
            core.contexts["manager"],
            core.team,
            user_id=core.users["member"],
            role="member",
            expected_revision=2,
        )
    assert failure.value.code == "user_not_available"
    assert invitation_snapshot(core) == before
