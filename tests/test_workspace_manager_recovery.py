"""Disposable-database checks for narrow workspace Manager recovery."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAction,
    ProjectNotFound,
    WorkspaceNotFound,
)
from pandrator.web.access.project_policy import require_project_access
from pandrator.web.access.workspace_policy import (
    WorkspaceAction,
    require_workspace_access,
)
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.accounts.workspace_contracts import WorkspaceCommandError
from pandrator.web.accounts.workspace_recovery import WorkspaceManagerRecovery
from pandrator.web.database import Database
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import ApiToken, AppSetting, Base
from pandrator.web.projects.models import Project, ProjectMembership

ADMIN_SCOPES = frozenset({"app.read", "app.write", "app.admin"})
CONTENT_SCOPES = frozenset({"app.read", "app.write"})


@dataclass
class RecoveryCase:
    database: Database
    users: dict[str, str]
    homes: dict[str, str]
    contexts: dict[str, AccessContext]
    team: str
    archived_team: str
    managed_team: str
    foreign_team: str
    project: str
    recovery: WorkspaceManagerRecovery


def _audit(
    session: Session,
    context: AccessContext,
    *,
    action: str,
    workspace_id: str,
    metadata: dict[str, str | int | bool | None],
) -> None:
    session.add(
        AppSetting(
            key=f"test.workspace-recovery.{new_id()}",
            value_json={
                "action": action,
                "workspace_id": workspace_id,
                "metadata": metadata,
                "user_id": context.actor.user_id,
            },
        )
    )


def _token(
    case: RecoveryCase,
    who: str,
    *,
    scopes: frozenset[str],
    workspace_ids: tuple[str, ...] = (),
    project_ids: tuple[str, ...] = (),
) -> AccessContext:
    token_id = new_id()
    subject = f"api-token:{token_id}"
    with case.database.immediate_session() as session:
        session.add(
            ApiToken(
                id=token_id,
                label="Workspace recovery test",
                token_hash=hashlib.sha256(token_id.encode()).hexdigest(),
                token_prefix="recovery-test",
                subject=subject,
                principal_kind="api_token",
                scopes_json=sorted(scopes),
                user_id=case.users[who],
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add_all(
            ApiTokenWorkspaceGrant(token_id=token_id, workspace_id=workspace_id)
            for workspace_id in workspace_ids
        )
        session.add_all(
            ApiTokenProjectGrant(token_id=token_id, project_id=project_id)
            for project_id in project_ids
        )
    return AccessContext(
        UserActor(
            user_id=case.users[who],
            auth_revision=1,
            subject=subject,
            principal_kind="api_token",
            scopes=scopes,
            token_id=token_id,
        )
    )


@pytest.fixture
def recovery_case(tmp_path: Path):
    database = Database(tmp_path / "workspace-manager-recovery.sqlite3")
    Base.metadata.create_all(database.engine)
    users: dict[str, str] = {}
    homes: dict[str, str] = {}
    team_id, archived_id, managed_id, foreign_id, project_id = (new_id() for _ in range(5))
    names = (
        "admin",
        "manager",
        "member",
        "inactive_member",
        "team_manager",
        "foreign_manager",
        "foreign_member",
    )
    with database.immediate_session() as session:
        for name in names:
            user_id = new_id()
            users[name] = user_id
            session.add(
                User(
                    id=user_id,
                    display_name=f"Recovery {name}",
                    is_active=name != "inactive_member",
                    is_instance_admin=name == "admin",
                )
            )
            session.flush()
            session.add(
                UserCredential(
                    user_id=user_id,
                    login_name=f"recovery-{name}",
                    password_hash="configured" if name == "admin" else None,
                    auth_revision=1,
                )
            )
            home = Workspace(
                kind="personal",
                name=f"Personal {name}",
                personal_owner_user_id=user_id,
            )
            session.add(home)
            session.flush()
            homes[name] = home.id
            session.add(
                WorkspaceMembership(
                    workspace_id=home.id,
                    user_id=user_id,
                    role="owner",
                )
            )

        old_timestamp = datetime(2020, 1, 1, tzinfo=UTC)
        team = Workspace(
            id=team_id,
            kind="team",
            name="Alpha recovery team",
            created_at=old_timestamp,
            updated_at=old_timestamp,
        )
        archived = Workspace(
            id=archived_id,
            kind="team",
            name="Zulu archived team",
            state="archived",
            revision=3,
        )
        managed = Workspace(id=managed_id, kind="team", name="Managed team")
        foreign = Workspace(id=foreign_id, kind="team", name="Foreign team")
        session.add_all([team, archived, managed, foreign])
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(workspace_id=team_id, user_id=users["manager"], role="admin"),
                WorkspaceMembership(workspace_id=team_id, user_id=users["member"], role="member"),
                WorkspaceMembership(
                    workspace_id=team_id,
                    user_id=users["inactive_member"],
                    role="member",
                ),
                WorkspaceMembership(
                    workspace_id=archived_id, user_id=users["member"], role="member"
                ),
                WorkspaceMembership(
                    workspace_id=managed_id,
                    user_id=users["team_manager"],
                    role="admin",
                ),
                WorkspaceMembership(
                    workspace_id=managed_id, user_id=users["member"], role="member"
                ),
                WorkspaceMembership(
                    workspace_id=foreign_id,
                    user_id=users["foreign_manager"],
                    role="admin",
                ),
                WorkspaceMembership(
                    workspace_id=foreign_id,
                    user_id=users["foreign_member"],
                    role="member",
                ),
            ]
        )
        project = Project(
            id=project_id,
            workspace_id=team_id,
            name="Recovery test project",
            created_by_user_id=users["manager"],
        )
        session.add(project)
        session.flush()
        session.add(
            ProjectMembership(
                project_id=project_id,
                user_id=users["member"],
                role="editor",
                granted_by_user_id=users["manager"],
            )
        )

    case = RecoveryCase(
        database=database,
        users=users,
        homes=homes,
        contexts={},
        team=team_id,
        archived_team=archived_id,
        managed_team=managed_id,
        foreign_team=foreign_id,
        project=project_id,
        recovery=WorkspaceManagerRecovery(audit=_audit),
    )
    case.contexts["admin"] = _token(
        case,
        "admin",
        scopes=ADMIN_SCOPES,
    )
    case.contexts["manager"] = _token(
        case,
        "manager",
        scopes=CONTENT_SCOPES,
        workspace_ids=(team_id,),
        project_ids=(project_id,),
    )
    case.contexts["member"] = _token(
        case,
        "member",
        scopes=CONTENT_SCOPES,
        workspace_ids=(team_id,),
    )
    case.contexts["inactive_member"] = _token(
        case,
        "inactive_member",
        scopes=CONTENT_SCOPES,
        workspace_ids=(team_id,),
    )
    case.contexts["team_manager"] = _token(
        case,
        "team_manager",
        scopes=CONTENT_SCOPES,
        workspace_ids=(managed_id,),
    )
    case.contexts["foreign_manager"] = _token(
        case,
        "foreign_manager",
        scopes=CONTENT_SCOPES,
        workspace_ids=(foreign_id,),
    )
    case.contexts["foreign_member"] = _token(
        case,
        "foreign_member",
        scopes=CONTENT_SCOPES,
        workspace_ids=(foreign_id,),
    )
    try:
        yield case
    finally:
        database.dispose()


def _disable_sole_manager(case: RecoveryCase) -> None:
    actor = AccountActor(
        user_id=case.users["admin"],
        auth_revision=1,
        scopes=frozenset({"app.admin"}),
    )
    with case.database.immediate_session() as session:
        result = AccountCommands(case.database).update_user_in_session(
            session,
            actor,
            case.users["manager"],
            expected_auth_revision=1,
            is_active=False,
        )
        assert result.is_active is False


def _audit_rows(case: RecoveryCase) -> list[dict[str, Any]]:
    with case.database.snapshot_session() as session:
        return list(
            session.scalars(
                select(AppSetting.value_json).where(
                    AppSetting.key.like("test.workspace-recovery.%")
                )
            ).all()
        )


def test_orphan_listing_after_account_disable_is_sql_scoped_and_paged(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    _disable_sole_manager(case)
    statements: list[str] = []

    def capture(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        lowered = statement.lower()
        if "workspace_memberships" in lowered and "users" in lowered and "display_name" in lowered:
            statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        with case.database.snapshot_session() as session:
            first_page = case.recovery.list_orphaned(session, case.contexts["admin"], limit=1)
            second_page = case.recovery.list_orphaned(
                session, case.contexts["admin"], limit=1, offset=1
            )
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)

    assert first_page["has_more"] is True
    assert first_page["offset"] == 0 and first_page["limit"] == 1
    assert first_page["items"][0]["id"] == case.team
    assert first_page["items"][0]["state"] == "active"
    assert first_page["items"][0]["candidates"] == [
        {"user_id": case.users["member"], "display_name": "Recovery member"}
    ]
    assert second_page["items"][0]["id"] == case.archived_team
    assert second_page["items"][0]["state"] == "archived"
    assert second_page["has_more"] is False
    assert len(statements) == 2
    assert all("projects" not in statement.lower() for statement in statements)
    with case.database.snapshot_session() as session:
        all_orphans = case.recovery.list_orphaned(session, case.contexts["admin"])
    assert {item["id"] for item in all_orphans["items"]} == {
        case.team,
        case.archived_team,
    }


def test_invalid_pagination_is_strict_and_rejected_before_queries(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    statements: list[str] = []

    def capture(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)

    invalid = ((True, 0), (0, 0), (101, 0), (50, True), (50, -1), (50, 100_001))
    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        with case.database.snapshot_session() as session:
            for limit, offset in invalid:
                with pytest.raises(ValueError):
                    case.recovery.list_orphaned(
                        session, case.contexts["admin"], limit=limit, offset=offset
                    )
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)
    assert statements == []


def test_candidate_query_batches_returned_workspaces_and_rejects_overflow(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    _disable_sole_manager(case)
    statements: list[str] = []

    def capture(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        lowered = statement.lower()
        if "workspace_memberships" in lowered and "users" in lowered and "display_name" in lowered:
            statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        with case.database.snapshot_session() as session:
            case.recovery.list_orphaned(session, case.contexts["admin"])
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)
    assert len(statements) == 1

    too_many = [
        User(id=new_id(), display_name=f"Overflow {index:04d}", is_active=True)
        for index in range(1000)
    ]
    with case.database.immediate_session() as session:
        session.add_all(too_many)
        session.flush()
        session.add_all(
            WorkspaceMembership(
                workspace_id=case.team,
                user_id=user.id,
                role="member",
            )
            for user in too_many
        )
    with case.database.snapshot_session() as session, pytest.raises(ValueError):
        case.recovery.list_orphaned(session, case.contexts["admin"])


def test_recovery_needs_current_admin_and_exact_scopes(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    non_admin = _token(
        case,
        "member",
        scopes=ADMIN_SCOPES,
        workspace_ids=(),
    )
    missing_read = _token(
        case,
        "admin",
        scopes=frozenset({"app.admin"}),
    )
    missing_admin = _token(
        case,
        "admin",
        scopes=frozenset({"app.read", "app.write"}),
    )
    missing_write = _token(
        case,
        "admin",
        scopes=frozenset({"app.read", "app.admin"}),
    )
    with case.database.snapshot_session() as session:
        for context in (non_admin, missing_read, missing_admin):
            with pytest.raises(AccessDenied):
                case.recovery.list_orphaned(session, context)
        with pytest.raises(AccessDenied):
            case.recovery.promote(
                session,
                missing_write,
                case.team,
                case.users["member"],
                expected_revision=1,
            )
        with pytest.raises(AccessDenied):
            case.recovery.list_orphaned(session, object())  # type: ignore[arg-type]


def test_live_auth_revision_token_revocation_and_account_disable_are_rechecked(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    stale_token = case.contexts["admin"]
    with case.database.immediate_session() as session:
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == stale_token.actor.token_id)
            .values(revoked_at=utcnow())
        )
    with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
        case.recovery.list_orphaned(session, stale_token)

    stale_revision = _token(case, "admin", scopes=ADMIN_SCOPES)
    with case.database.immediate_session() as session:
        session.execute(
            update(UserCredential)
            .where(UserCredential.user_id == case.users["admin"])
            .values(auth_revision=2)
        )
    with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
        case.recovery.list_orphaned(session, stale_revision)

    with case.database.immediate_session() as session:
        session.execute(
            update(UserCredential)
            .where(UserCredential.user_id == case.users["admin"])
            .values(auth_revision=1)
        )
    inactive_actor = _token(case, "admin", scopes=ADMIN_SCOPES)
    with case.database.immediate_session() as session:
        session.execute(update(User).where(User.id == case.users["admin"]).values(is_active=False))
    with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
        case.recovery.list_orphaned(session, inactive_actor)


def test_member_keeps_content_access_and_gains_governance_only_after_recovery(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    _disable_sole_manager(case)
    member = case.contexts["member"]
    with case.database.snapshot_session() as session:
        before_workspace = require_workspace_access(
            session, member, case.team, action=WorkspaceAction.EDIT
        )
        before_project = require_project_access(
            session, member, case.project, action=ProjectAction.EDIT
        )
        assert before_workspace.role == "member"
        assert before_project.workspace_role == "member"
        with pytest.raises(WorkspaceNotFound):
            require_workspace_access(session, member, case.team, action=WorkspaceAction.MANAGE)
        with pytest.raises(AccessDenied):
            require_project_access(
                session, member, case.project, action=ProjectAction.MANAGE_MEMBERS
            )

    with case.database.snapshot_session() as session:
        memberships_before = session.execute(
            select(
                WorkspaceMembership.workspace_id,
                WorkspaceMembership.user_id,
                WorkspaceMembership.role,
            )
        ).all()
        project_memberships_before = session.execute(
            select(ProjectMembership.project_id, ProjectMembership.user_id, ProjectMembership.role)
        ).all()
        grants_before = session.execute(
            select(ApiTokenWorkspaceGrant.token_id, ApiTokenWorkspaceGrant.workspace_id)
        ).all()
    with case.database.immediate_session() as session:
        result = case.recovery.promote(
            session,
            case.contexts["admin"],
            case.team,
            case.users["member"],
            expected_revision=1,
        )
    assert result == {
        "id": case.team,
        "name": "Alpha recovery team",
        "state": "active",
        "revision": 2,
        "recovered_user_id": case.users["member"],
    }
    with case.database.snapshot_session() as session:
        after_workspace = require_workspace_access(
            session, member, case.team, action=WorkspaceAction.MANAGE
        )
        after_project = require_project_access(
            session, member, case.project, action=ProjectAction.MANAGE_MEMBERS
        )
        assert after_workspace.role == "admin"
        assert after_project.workspace_role == "admin"
        after_content_access = require_project_access(
            session, member, case.project, action=ProjectAction.EDIT
        )
        assert ProjectAction.EDIT in before_project.allowed_actions
        assert ProjectAction.EDIT in after_content_access.allowed_actions
        memberships_after = session.execute(
            select(
                WorkspaceMembership.workspace_id,
                WorkspaceMembership.user_id,
                WorkspaceMembership.role,
            )
        ).all()
        project_memberships_after = session.execute(
            select(ProjectMembership.project_id, ProjectMembership.user_id, ProjectMembership.role)
        ).all()
        grants_after = session.execute(
            select(ApiTokenWorkspaceGrant.token_id, ApiTokenWorkspaceGrant.workspace_id)
        ).all()
        assert memberships_after != memberships_before
        assert len(memberships_after) == len(memberships_before)
        assert project_memberships_after == project_memberships_before
        assert grants_after == grants_before
        recovered_membership = session.get(WorkspaceMembership, (case.team, case.users["member"]))
        assert recovered_membership is not None and recovered_membership.role == "admin"
        updated_workspace = session.get(Workspace, case.team)
        assert updated_workspace is not None
        assert updated_workspace.updated_at.replace(tzinfo=UTC) > datetime(2020, 1, 1, tzinfo=UTC)
        with pytest.raises(WorkspaceNotFound):
            require_workspace_access(session, case.contexts["admin"], case.team)
        with pytest.raises(ProjectNotFound):
            require_project_access(session, case.contexts["admin"], case.project)

    audit_rows = _audit_rows(case)
    assert audit_rows == [
        {
            "action": "workspace.manager.recover",
            "workspace_id": case.team,
            "metadata": {
                "user_id": case.users["member"],
                "role": "manager",
                "revision": 2,
            },
            "user_id": case.users["admin"],
        }
    ]


def test_member_recovery_on_archived_team_preserves_state(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    result: dict[str, Any]
    with case.database.immediate_session() as session:
        result = case.recovery.promote(
            session,
            case.contexts["admin"],
            case.archived_team,
            case.users["member"],
            expected_revision=3,
        )
    assert result["state"] == "archived"
    assert result["revision"] == 4
    with case.database.snapshot_session() as session:
        archived = session.get(Workspace, case.archived_team)
        assert archived is not None
        assert archived.state == "archived" and archived.revision == 4


def test_promote_rejects_active_manager_personal_foreign_inactive_and_absent_targets(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    with pytest.raises(WorkspaceCommandError) as active:
        with case.database.immediate_session() as session:
            case.recovery.promote(
                session,
                case.contexts["admin"],
                case.managed_team,
                case.users["member"],
                expected_revision=1,
            )
    assert active.value.code == "workspace_manager_recovery_not_needed"

    with pytest.raises(WorkspaceCommandError) as personal:
        with case.database.immediate_session() as session:
            case.recovery.promote(
                session,
                case.contexts["admin"],
                case.homes["member"],
                case.users["member"],
                expected_revision=1,
            )
    assert personal.value.code == "personal_workspace_protected"

    with pytest.raises(WorkspaceNotFound):
        with case.database.immediate_session() as session:
            case.recovery.promote(
                session,
                case.contexts["admin"],
                new_id(),
                case.users["member"],
                expected_revision=1,
            )

    _disable_sole_manager(case)
    target_ids: tuple[Any, ...] = (
        case.users["foreign_member"],
        case.users["foreign_manager"],
        case.users["inactive_member"],
        case.users["admin"],
        new_id(),
        None,
        [],
    )
    for target_id in target_ids:
        with pytest.raises(WorkspaceCommandError) as failure:
            with case.database.immediate_session() as session:
                case.recovery.promote(
                    session,
                    case.contexts["admin"],
                    case.team,
                    target_id,
                    expected_revision=1,
                )
        assert failure.value.code == "workspace_manager_recovery_target"


def test_revision_guard_and_audit_failure_leave_no_partial_promotion(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    _disable_sole_manager(case)
    for expected_revision, code in (
        (True, "invalid_revision"),
        (0, "invalid_revision"),
        (2, "workspace_revision_conflict"),
    ):
        with pytest.raises(WorkspaceCommandError) as failure:
            with case.database.immediate_session() as session:
                case.recovery.promote(
                    session,
                    case.contexts["admin"],
                    case.team,
                    case.users["member"],
                    expected_revision=expected_revision,
                )
        assert failure.value.code == code
    assert _audit_rows(case) == []

    def failing_audit(
        session: Session,
        context: AccessContext,
        *,
        action: str,
        workspace_id: str,
        metadata: dict[str, str | int | bool | None],
    ) -> None:
        _audit(
            session,
            context,
            action=action,
            workspace_id=workspace_id,
            metadata=metadata,
        )
        raise RuntimeError("Synthetic audit sink failure")

    failing_recovery = WorkspaceManagerRecovery(audit=failing_audit)
    with pytest.raises(RuntimeError, match="Synthetic audit"):
        with case.database.immediate_session() as session:
            failing_recovery.promote(
                session,
                case.contexts["admin"],
                case.team,
                case.users["member"],
                expected_revision=1,
            )
    with case.database.snapshot_session() as session:
        workspace = session.get(Workspace, case.team)
        membership = session.get(WorkspaceMembership, (case.team, case.users["member"]))
        assert workspace is not None and workspace.revision == 1
        assert membership is not None and membership.role == "member"
    assert _audit_rows(case) == []


def test_admin_recovery_metadata_needs_no_workspace_grant_and_adds_none(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    _disable_sole_manager(case)
    admin_context = case.contexts["admin"]
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiTokenWorkspaceGrant)
                .where(
                    ApiTokenWorkspaceGrant.token_id == admin_context.actor.token_id,
                    ApiTokenWorkspaceGrant.workspace_id == case.team,
                )
            )
            == 0
        )
    with case.database.immediate_session() as session:
        case.recovery.promote(
            session,
            admin_context,
            case.team,
            case.users["member"],
            expected_revision=1,
        )
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == case.users["admin"],
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiTokenWorkspaceGrant)
                .where(ApiTokenWorkspaceGrant.token_id == admin_context.actor.token_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiTokenProjectGrant)
                .where(ApiTokenProjectGrant.token_id == admin_context.actor.token_id)
            )
            == 0
        )
