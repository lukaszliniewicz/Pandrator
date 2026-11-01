"""Disposable SQL fixtures and common workspace authorization acceptance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import AccessContext, AccessDenied, WorkspaceNotFound
from pandrator.web.access.folder_policy import require_folder_workspace
from pandrator.web.access.workspace_policy import (
    WorkspaceAction,
    require_workspace_access,
    workspace_payload,
)
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.accounts.workspace_commands import WorkspaceCommands
from pandrator.web.accounts.workspace_queries import WorkspaceQueries
from pandrator.web.database import Database
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import ApiToken, AppSetting, Base
from pandrator.web.projects.models import Project, ProjectMembership

T = TypeVar("T")


def present(value: T | None) -> T:
    assert value is not None
    return value


SCOPES = frozenset({"app.read", "app.write", "app.workspaces.enroll"})


@dataclass
class CoreCase:
    database: Database
    users: dict[str, str] = field(default_factory=dict)
    homes: dict[str, str] = field(default_factory=dict)
    contexts: dict[str, AccessContext] = field(default_factory=dict)
    team: str = ""
    project: str = ""
    commands: WorkspaceCommands = field(init=False)

    def __post_init__(self) -> None:
        self.commands = WorkspaceCommands(audit=audit)


def audit(
    session: Session,
    context: AccessContext,
    *,
    action: str,
    workspace_id: str,
    metadata: dict[str, str | int | bool | None],
) -> None:
    assert set(metadata) <= {"user_id", "invitation_id", "role", "state", "revision"}
    session.add(
        AppSetting(
            key=f"test.audit.{new_id()}",
            value_json={
                "action": action,
                "workspace_id": workspace_id,
                "metadata": metadata,
                "user_id": context.actor.user_id,
            },
        )
    )


def token(
    case: CoreCase,
    who: str,
    *,
    scopes: frozenset[str] = SCOPES,
    workspace_ids: list[str] | None = None,
    client_id: str | None = None,
) -> AccessContext:
    token_id = new_id()
    subject = f"api-token:{token_id}"
    with case.database.immediate_session() as session:
        session.add(
            ApiToken(
                id=token_id,
                label="Synthetic workspace test",
                token_hash=new_id(),
                token_prefix="synthetic",
                subject=subject,
                principal_kind="api_token",
                scopes_json=sorted(scopes),
                user_id=case.users[who],
                user_auth_revision=1,
                client_id=client_id,
            )
        )
        session.flush()
        for workspace_id in (
            workspace_ids if workspace_ids is not None else [case.homes[who], case.team]
        ):
            session.add(ApiTokenWorkspaceGrant(token_id=token_id, workspace_id=workspace_id))
    return AccessContext(
        UserActor(
            user_id=case.users[who],
            auth_revision=1,
            subject=subject,
            principal_kind="api_token",
            scopes=scopes,
            token_id=token_id,
            client_id=client_id,
        )
    )


def browser(case: CoreCase, who: str) -> AccessContext:
    identifier, now = new_id(), utcnow()
    with case.database.immediate_session() as session:
        session.add(
            BrowserAuthSession(
                id=identifier,
                user_id=case.users[who],
                auth_revision=1,
                token_hash=new_id(),
                subject=f"browser:{identifier}",
                principal_kind="owner_session",
                scopes_json=sorted(SCOPES),
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                expires_at=now + timedelta(days=1),
            )
        )
    return AccessContext(
        UserActor(
            user_id=case.users[who],
            auth_revision=1,
            subject=f"browser:{identifier}",
            principal_kind="owner_session",
            scopes=SCOPES,
            browser_session_id=identifier,
        )
    )


@pytest.fixture
def core(tmp_path: Path) -> Any:
    database = Database(tmp_path / "workspace-core.sqlite3")
    Base.metadata.create_all(database.engine)
    case = CoreCase(database)
    with database.immediate_session() as session:
        for name in ("manager", "manager2", "member", "target", "outsider", "admin"):
            identifier = new_id()
            case.users[name] = identifier
            session.add(
                User(
                    id=identifier,
                    display_name=f"Synthetic {name}",
                    is_instance_admin=name == "admin",
                    is_active=True,
                )
            )
            session.flush()
            session.add(
                UserCredential(user_id=identifier, login_name=f"test-{name}", auth_revision=1)
            )
            home = Workspace(
                kind="personal", name=f"Private {name}", personal_owner_user_id=identifier
            )
            session.add(home)
            session.flush()
            case.homes[name] = home.id
            session.add(WorkspaceMembership(workspace_id=home.id, user_id=identifier, role="owner"))
        team = Workspace(kind="team", name="Private team")
        session.add(team)
        session.flush()
        case.team = team.id
        for name, role in (("manager", "admin"), ("manager2", "admin"), ("member", "member")):
            session.add(
                WorkspaceMembership(workspace_id=team.id, user_id=case.users[name], role=role)
            )
        project = Project(workspace_id=team.id, name="Synthetic content")
        session.add(project)
        session.flush()
        case.project = project.id
    for who in case.users:
        case.contexts[who] = token(case, who)
    yield case
    database.engine.dispose()


def revision(core: CoreCase) -> int:
    with core.database.snapshot_session() as session:
        return session.execute(
            select(Workspace.revision).where(Workspace.id == core.team)
        ).scalar_one()


def snapshot(core: CoreCase) -> tuple[Any, ...]:
    with core.database.snapshot_session() as session:
        return tuple(
            tuple(session.execute(select(model.__table__)).all())
            for model in (Workspace, WorkspaceMembership, AppSetting)
        )


@pytest.mark.parametrize("who", ["manager", "member", "admin"])
@pytest.mark.parametrize("action", list(WorkspaceAction))
@pytest.mark.parametrize("write", [False, True])
def test_workspace_role_scope_sql_parity(
    core: CoreCase, who: str, action: WorkspaceAction, write: bool
) -> None:
    context = token(core, who, scopes=SCOPES if write else frozenset({"app.read"}))
    needs_write = action in {WorkspaceAction.EDIT, WorkspaceAction.MANAGE}
    needs_manager = action in {WorkspaceAction.MANAGE_READ, WorkspaceAction.MANAGE}
    with core.database.snapshot_session() as session:
        if not write and needs_write:
            with pytest.raises(AccessDenied):
                require_workspace_access(session, context, core.team, action=action)
        elif who == "admin" or (needs_manager and who == "member"):
            with pytest.raises(WorkspaceNotFound):
                require_workspace_access(session, context, core.team, action=action)
        else:
            access = require_workspace_access(session, context, core.team, action=action)
            assert action in access.allowed_actions
            if not write:
                assert "manage" not in workspace_payload(access)["allowed_actions"]


def test_default_home_personal_owner_and_no_admin_wildcard(core: CoreCase) -> None:
    context = token(
        core, "member", workspace_ids=[core.homes["member"], core.homes["manager"], core.team]
    )
    with core.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(
                workspace_id=core.homes["manager"], user_id=core.users["member"], role="owner"
            )
        )
    with core.database.snapshot_session() as session:
        assert require_workspace_access(session, context).id == core.homes["member"]
        with pytest.raises(WorkspaceNotFound):
            require_workspace_access(session, context, core.homes["manager"])
        assert {w["id"] for w in WorkspaceQueries.list_workspaces(session, context)} == {
            core.homes["member"],
            core.team,
        }
        for who in ("admin", "outsider"):
            assert [
                w["id"] for w in WorkspaceQueries.list_workspaces(session, core.contexts[who])
            ] == [core.homes[who]]


def test_project_guest_and_credential_project_grants_do_not_list_team(core: CoreCase) -> None:
    context = token(core, "outsider", workspace_ids=[core.homes["outsider"]])
    with core.database.immediate_session() as session:
        session.add_all(
            [
                ProjectMembership(
                    project_id=core.project, user_id=core.users["outsider"], role="manager"
                ),
                ApiTokenProjectGrant(token_id=context.actor.token_id, project_id=core.project),
            ]
        )
    with core.database.snapshot_session() as session:
        assert [w["id"] for w in WorkspaceQueries.list_workspaces(session, context)] == [
            core.homes["outsider"]
        ]
        with pytest.raises(WorkspaceNotFound):
            require_workspace_access(session, context, core.team)


def test_live_credential_and_account_revision_checks(core: CoreCase) -> None:
    with core.database.immediate_session() as session:
        session.execute(
            update(UserCredential)
            .where(UserCredential.user_id == core.users["member"])
            .values(auth_revision=2)
        )
    with core.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            WorkspaceQueries.list_workspaces(session, core.contexts["member"])
    context = token(core, "manager", workspace_ids=[])
    with core.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            require_workspace_access(session, context, core.team)


def test_single_scoped_select_and_folder_resolution_parity(core: CoreCase) -> None:
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(core.database.engine, "before_cursor_execute", capture)
    try:
        with core.database.snapshot_session() as session:
            statements.clear()
            folder = require_folder_workspace(
                session, core.contexts["member"], core.team, write=True
            )
            assert (folder.id, folder.kind) == (core.team, "team")
            assert len(statements) == 3  # current account, current token, one workspace projection
            assert "workspace_memberships" in statements[-1]
            statements.clear()
            WorkspaceQueries.list_workspaces(session, core.contexts["member"])
            assert len(statements) == 3
    finally:
        event.remove(core.database.engine, "before_cursor_execute", capture)


def test_roster_projections_and_bounded_literal_search(core: CoreCase) -> None:
    with core.database.immediate_session() as session:
        session.execute(update(User).where(User.id == core.users["member"]).values(is_active=False))
        session.execute(
            update(User)
            .where(User.id == core.users["target"])
            .values(display_name="Literal %_ person")
        )
    with core.database.snapshot_session() as session:
        rows = WorkspaceQueries.members(session, core.contexts["manager"], core.team)
        assert all(
            set(row) == {"user_id", "display_name", "login_name", "role", "is_active"} for row in rows
        )
        assert next(row for row in rows if row["user_id"] == core.users["member"])["is_active"] is False
        assert (
            WorkspaceQueries.search_users(session, core.contexts["manager"], core.team, "x") == []
        )
        assert (
            WorkspaceQueries.search_users(session, core.contexts["manager"], core.team, "x" * 65)
            == []
        )
        result = WorkspaceQueries.search_users(session, core.contexts["manager"], core.team, "%_")
        assert [row["user_id"] for row in result] == [core.users["target"]]
        assert set(result[0]) == {"user_id", "login_name", "display_name"}


def test_archived_manager_enumeration_never_opens_content(core: CoreCase) -> None:
    with core.database.immediate_session() as session:
        core.commands.archive(session, core.contexts["manager"], core.team, expected_revision=1)
    with core.database.snapshot_session() as session:
        assert core.team not in {
            w["id"] for w in WorkspaceQueries.list_workspaces(session, core.contexts["manager"])
        }
        assert core.team in {
            w["id"]
            for w in WorkspaceQueries.list_workspaces(
                session, core.contexts["manager"], include_archived=True
            )
        }
        assert core.team not in {
            w["id"]
            for w in WorkspaceQueries.list_workspaces(
                session, core.contexts["member"], include_archived=True
            )
        }
        with pytest.raises(WorkspaceNotFound):
            require_folder_workspace(session, core.contexts["manager"], core.team)
        with pytest.raises(AccessDenied):
            require_workspace_access(
                session, core.contexts["manager"], core.team, allow_archived=True
            )
        manager = require_workspace_access(
            session,
            core.contexts["manager"],
            core.team,
            action=WorkspaceAction.MANAGE_READ,
            allow_archived=True,
        )
        assert WorkspaceAction.READ not in manager.allowed_actions


def test_user_search_is_bounded_and_excludes_existing_members(core: CoreCase) -> None:
    with core.database.immediate_session() as session:
        for index in range(25):
            identifier = new_id()
            session.add(User(id=identifier, display_name=f"Synthetic bounded-{index:02}"))
            session.flush()
            session.add(
                UserCredential(
                    user_id=identifier, login_name=f"bounded-{index:02}", auth_revision=1
                )
            )
    with core.database.snapshot_session() as session:
        results = WorkspaceQueries.search_users(
            session, core.contexts["manager"], core.team, "bounded-"
        )
        assert len(results) == 20
        assert [row["login_name"] for row in results] == [
            f"bounded-{index:02}" for index in range(20)
        ]
        names = WorkspaceQueries.search_users(session, core.contexts["manager"], core.team, "test-")
        assert not {core.users["manager"], core.users["manager2"], core.users["member"]} & {
            row["user_id"] for row in names
        }
