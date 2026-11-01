from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAction,
    ProjectNotFound,
    WorkspaceNotFound,
)
from pandrator.web.access.project_permissions import CONTENT_ACTIONS
from pandrator.web.access.project_policy import (
    personal_workspace_id,
    require_project_access,
    require_project_creation,
    visible_project_ids_statement,
)
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import HostActor, UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
)
from pandrator.web.database import Database
from pandrator.web.model_base import new_id
from pandrator.web.models import ApiToken, AutomationClient, SessionRecord
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import Project, ProjectMembership
from tests.web_test_support import prepare_web_test_data_root

ALL_SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
ALL_ACTIONS = frozenset(ProjectAction)
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

ROLE_ACTIONS: dict[str, frozenset[ProjectAction]] = {
    "viewer": frozenset({ProjectAction.READ}),
    "reviewer": frozenset({ProjectAction.READ}),
    "editor": frozenset({ProjectAction.READ}),
    "manager": frozenset({ProjectAction.READ}),
}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database
    finally:
        database.dispose()


def _insert_user(session: Session, *, admin: bool = False) -> str:
    user_id = new_id()
    session.add(
        User(
            id=user_id,
            legacy_subject=None,
            display_name=f"Test {user_id[:8]}",
            is_active=True,
            is_instance_admin=admin,
        )
    )
    session.flush()
    session.add(
        UserCredential(
            user_id=user_id,
            login_name=f"user-{user_id[:8]}",
            password_hash=None,
            auth_revision=1,
        )
    )
    session.flush()
    return user_id


def _insert_actor(
    session: Session,
    user_id: str,
    scopes: frozenset[str] = ALL_SCOPES,
    *,
    client_id: str | None = None,
) -> UserActor:
    credential = session.get(UserCredential, user_id)
    assert credential is not None
    token_id = new_id()
    session.add(
        ApiToken(
            id=token_id,
            label="synthetic policy test token",
            token_hash=hashlib.sha256(token_id.encode("ascii")).hexdigest(),
            token_prefix=token_id[:12],
            subject=f"test-user:{user_id}",
            scopes_json=sorted(scopes),
            principal_kind="api_token",
            user_id=user_id,
            user_auth_revision=credential.auth_revision,
            client_id=client_id,
            created_by="project-access-policy-test",
        )
    )
    session.flush()
    return UserActor(
        user_id=user_id,
        auth_revision=credential.auth_revision,
        subject=f"test-user:{user_id}",
        principal_kind="api_token",
        scopes=scopes,
        token_id=token_id,
        client_id=client_id,
    )


def _insert_workspace(
    session: Session,
    *,
    kind: str = "team",
    owner_id: str | None = None,
    state: str = "active",
    name: str = "Test workspace",
    member_id: str | None = None,
    role: str | None = None,
) -> str:
    workspace = Workspace(
        id=new_id(),
        kind=kind,
        name=name,
        personal_owner_user_id=owner_id,
        state=state,
        revision=1,
    )
    session.add(workspace)
    session.flush()
    if member_id is not None and role is not None:
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=member_id,
                role=role,
            )
        )
        session.flush()
    return workspace.id


def _insert_project(
    session: Session,
    workspace_id: str,
    *,
    name: str = "Test project",
    creator_id: str | None = None,
    retained_source: bool = True,
) -> str:
    if retained_source:
        source = SessionRecord(id=new_id(), name=name)
        project = create_session_project(
            session, source, workspace_id=workspace_id, legacy=True
        )
        project.name = name
        project.created_by_user_id = creator_id
    else:
        project = Project(
            id=new_id(),
            workspace_id=workspace_id,
            source_session_id=None,
            name=name,
            created_by_user_id=creator_id,
            revision=1,
        )
        session.add(project)
    session.flush()
    return project.id


def _grant_project(
    session: Session,
    project_id: str,
    user_id: str,
    role: str,
) -> ProjectMembership:
    membership = ProjectMembership(
        project_id=project_id,
        user_id=user_id,
        role=role,
    )
    session.add(membership)
    session.flush()
    return membership


def _grant_actor_resources(
    session: Session,
    actor: UserActor,
    *,
    workspace_ids: tuple[str, ...] = (),
    project_ids: tuple[str, ...] = (),
) -> None:
    assert actor.token_id is not None

    pending_token_workspaces = {
        (grant.token_id, grant.workspace_id)
        for grant in session.new
        if isinstance(grant, ApiTokenWorkspaceGrant)
    }
    for workspace_id in dict.fromkeys(workspace_ids):
        key = (actor.token_id, workspace_id)
        if key not in pending_token_workspaces and session.get(
            ApiTokenWorkspaceGrant, key
        ) is None:
            session.add(
                ApiTokenWorkspaceGrant(
                    token_id=actor.token_id,
                    workspace_id=workspace_id,
                )
            )
            pending_token_workspaces.add(key)

    pending_token_projects = {
        (grant.token_id, grant.project_id)
        for grant in session.new
        if isinstance(grant, ApiTokenProjectGrant)
    }
    for project_id in dict.fromkeys(project_ids):
        key = (actor.token_id, project_id)
        if key not in pending_token_projects and session.get(
            ApiTokenProjectGrant, key
        ) is None:
            session.add(
                ApiTokenProjectGrant(
                    token_id=actor.token_id,
                    project_id=project_id,
                )
            )
            pending_token_projects.add(key)

    if actor.client_id is None:
        session.flush()
        return

    pending_client_workspaces = {
        (grant.client_id, grant.workspace_id)
        for grant in session.new
        if isinstance(grant, AutomationClientWorkspaceGrant)
    }
    for workspace_id in dict.fromkeys(workspace_ids):
        key = (actor.client_id, workspace_id)
        if key not in pending_client_workspaces and session.get(
            AutomationClientWorkspaceGrant, key
        ) is None:
            session.add(
                AutomationClientWorkspaceGrant(
                    client_id=actor.client_id,
                    workspace_id=workspace_id,
                )
            )
            pending_client_workspaces.add(key)

    pending_client_projects = {
        (grant.client_id, grant.project_id)
        for grant in session.new
        if isinstance(grant, AutomationClientProjectGrant)
    }
    for project_id in dict.fromkeys(project_ids):
        key = (actor.client_id, project_id)
        if key not in pending_client_projects and session.get(
            AutomationClientProjectGrant, key
        ) is None:
            session.add(
                AutomationClientProjectGrant(
                    client_id=actor.client_id,
                    project_id=project_id,
                )
            )
            pending_client_projects.add(key)
    session.flush()


def _context(actor: UserActor) -> AccessContext:
    return AccessContext(actor)


def _visible_ids(
    session: Session,
    context: AccessContext,
    action: ProjectAction = ProjectAction.READ,
) -> set[str]:
    statement = visible_project_ids_statement(session, context, action)
    return set(session.scalars(select(Project.id).where(Project.id.in_(statement))))


def test_role_action_matrix_matches_direct_and_sql_visibility(database: Database) -> None:
    with database.session() as session:
        workspace_id = _insert_workspace(session)
        cases: list[tuple[str, str, AccessContext, frozenset[ProjectAction]]] = []
        for role, expected in ROLE_ACTIONS.items():
            assert expected == frozenset({ProjectAction.READ})
            user_id = _insert_user(session)
            actor = _insert_actor(session, user_id)
            project_id = _insert_project(session, workspace_id, name=f"{role} project")
            _grant_project(session, project_id, user_id, role)
            _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
            cases.append((project_id, role, _context(actor), expected))

        for project_id, _role, context, expected_actions in cases:
            access = require_project_access(session, context, project_id)
            assert access.allowed_actions == expected_actions
            for action in ProjectAction:
                if action in expected_actions:
                    result = require_project_access(session, context, project_id, action)
                    assert action in result.allowed_actions
                else:
                    with pytest.raises(AccessDenied):
                        require_project_access(session, context, project_id, action)

                visible = project_id in _visible_ids(session, context, action)
                assert visible is (action in expected_actions)


def test_workspace_owner_admin_and_manager_purge_boundary(database: Database) -> None:
    with database.session() as session:
        owner_id = _insert_user(session)
        workspace_id = _insert_workspace(
            session,
            member_id=owner_id,
            role="owner",
        )
        project_id = _insert_project(session, workspace_id)
        manager_id = _insert_user(session)
        manager_actor = _insert_actor(session, manager_id)
        _grant_project(session, project_id, manager_id, "manager")
        owner_actor = _insert_actor(session, owner_id)
        _grant_actor_resources(session, manager_actor, workspace_ids=(workspace_id,))
        _grant_actor_resources(session, owner_actor, workspace_ids=(workspace_id,))

        owner_access = require_project_access(session, _context(owner_actor), project_id)
        assert owner_access.workspace_role == "owner"
        assert owner_access.allowed_actions == ALL_ACTIONS
        assert _visible_ids(session, _context(owner_actor), ProjectAction.PURGE) == {project_id}

        administrator_id = _insert_user(session)
        session.add(
            WorkspaceMembership(
                workspace_id=workspace_id,
                user_id=administrator_id,
                role="admin",
            )
        )
        session.flush()
        administrator_actor = _insert_actor(session, administrator_id)
        _grant_actor_resources(
            session,
            administrator_actor,
            workspace_ids=(workspace_id,),
        )
        administrator_access = require_project_access(
            session, _context(administrator_actor), project_id
        )
        assert administrator_access.workspace_role == "admin"
        assert administrator_access.allowed_actions == ALL_ACTIONS
        assert _visible_ids(session, _context(administrator_actor), ProjectAction.PURGE) == {
            project_id
        }

        manager_access = require_project_access(session, _context(manager_actor), project_id)
        assert manager_access.allowed_actions == ROLE_ACTIONS["manager"]
        with pytest.raises(AccessDenied):
            require_project_access(
                session, _context(manager_actor), project_id, ProjectAction.PURGE
            )
        assert _visible_ids(session, _context(manager_actor), ProjectAction.PURGE) == set()


def test_literal_scopes_admin_flag_and_creator_attribution_do_not_grant_content(
    database: Database,
) -> None:
    with database.session() as session:
        administrator_id = _insert_user(session, admin=True)
        administrator_actor = _insert_actor(
            session,
            administrator_id,
            frozenset({"app.read", "app.admin"}),
        )
        administrator_writer = _insert_actor(
            session,
            administrator_id,
            frozenset({"app.read", "app.write", "app.admin"}),
        )
        workspace_id = _insert_workspace(session)
        project_id = _insert_project(session, workspace_id, creator_id=administrator_id)
        _grant_actor_resources(session, administrator_actor, workspace_ids=(workspace_id,))
        _grant_actor_resources(session, administrator_writer, workspace_ids=(workspace_id,))

        with pytest.raises(ProjectNotFound) as hidden:
            require_project_access(session, _context(administrator_actor), project_id)
        assert hidden.value.args == ("Project not found.",)
        assert _visible_ids(session, _context(administrator_actor)) == set()
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, _context(administrator_writer), workspace_id)

        admin_only = _insert_actor(session, administrator_id, frozenset({"app.admin"}))
        with pytest.raises(AccessDenied):
            require_project_access(session, _context(admin_only), project_id)

        editor_id = _insert_user(session)
        editor_actor = _insert_actor(
            session,
            editor_id,
            frozenset({"app.read", "app.cancel"}),
        )
        editor_project_id = _insert_project(session, workspace_id)
        _grant_project(session, editor_project_id, editor_id, "editor")
        _grant_actor_resources(session, editor_actor, project_ids=(editor_project_id,))
        access = require_project_access(session, _context(editor_actor), editor_project_id)
        assert access.allowed_actions == frozenset({ProjectAction.READ})
        with pytest.raises(AccessDenied):
            require_project_access(
                session, _context(editor_actor), editor_project_id, ProjectAction.CANCEL_OWN
            )
        assert _visible_ids(session, _context(editor_actor), ProjectAction.CANCEL_OWN) == set()
        with pytest.raises(AccessDenied):
            visible_project_ids_statement(session, _context(editor_actor), ProjectAction.RUN)
        with pytest.raises(AccessDenied):
            require_project_access(
                session, _context(editor_actor), editor_project_id, ProjectAction.EDIT
            )


def test_team_member_inherits_content_and_can_still_create_projects(database: Database) -> None:
    with database.session() as session:
        user_id = _insert_user(session)
        actor = _insert_actor(session, user_id)
        workspace_id = _insert_workspace(
            session,
            member_id=user_id,
            role="member",
        )
        project_id = _insert_project(session, workspace_id)
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))

        context = _context(actor)
        access = require_project_access(session, context, project_id)
        assert access.allowed_actions == CONTENT_ACTIONS
        for action in ProjectAction:
            if action in CONTENT_ACTIONS:
                assert project_id in _visible_ids(session, context, action)
                assert (
                    action
                    in require_project_access(session, context, project_id, action).allowed_actions
                )
            else:
                with pytest.raises(AccessDenied):
                    require_project_access(session, context, project_id, action)
                assert project_id not in _visible_ids(session, context, action)
        assert require_project_creation(session, _context(actor), workspace_id) == workspace_id
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, _context(actor), "missing-workspace-id")


def test_personal_project_guest_sees_only_shared_project_and_own_workspace(
    database: Database,
) -> None:
    with database.session() as session:
        owner_id = _insert_user(session)
        owner_workspace_id = _insert_workspace(
            session,
            kind="personal",
            owner_id=owner_id,
            member_id=owner_id,
            role="owner",
            name="Owner personal",
        )
        shared_id = _insert_project(session, owner_workspace_id, name="Shared project")
        private_id = _insert_project(session, owner_workspace_id, name="Private project")
        missing_source_id = _insert_project(
            session,
            owner_workspace_id,
            name="Project without a source",
            retained_source=False,
        )
        trashed_source_id = _insert_project(
            session, owner_workspace_id, name="Project with a trashed source"
        )
        trashed_project = session.get(Project, trashed_source_id)
        assert trashed_project is not None and trashed_project.source_session_id is not None
        trashed_source = session.get(SessionRecord, trashed_project.source_session_id)
        assert trashed_source is not None
        trashed_source.trashed_at = NOW

        guest_id = _insert_user(session)
        guest_actor = _insert_actor(session, guest_id)
        guest_workspace_id = _insert_workspace(
            session,
            kind="personal",
            owner_id=guest_id,
            member_id=guest_id,
            role="owner",
            name="Guest personal",
        )
        _grant_project(session, shared_id, guest_id, "viewer")
        _grant_project(session, missing_source_id, guest_id, "viewer")
        _grant_project(session, trashed_source_id, guest_id, "viewer")
        _grant_actor_resources(
            session,
            guest_actor,
            workspace_ids=(guest_workspace_id,),
            project_ids=(shared_id, missing_source_id, trashed_source_id),
        )

        assert _visible_ids(session, _context(guest_actor)) == {shared_id}
        assert (
            require_project_access(session, _context(guest_actor), shared_id).workspace_role is None
        )
        with pytest.raises(ProjectNotFound) as hidden:
            require_project_access(session, _context(guest_actor), private_id)
        assert hidden.value.args == ("Project not found.",)
        for unavailable_source_id in (missing_source_id, trashed_source_id):
            assert unavailable_source_id not in _visible_ids(session, _context(guest_actor))
            with pytest.raises(ProjectNotFound) as unavailable:
                require_project_access(session, _context(guest_actor), unavailable_source_id)
            assert unavailable.value.args == ("Project not found.",)
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, _context(guest_actor), owner_workspace_id)
        assert personal_workspace_id(session, _context(guest_actor)) == guest_workspace_id


def test_archived_workspaces_and_malformed_personal_roles_do_not_grant_access(
    database: Database,
) -> None:
    with database.session() as session:
        archived_owner = _insert_user(session)
        archived_workspace = _insert_workspace(
            session,
            state="archived",
            member_id=archived_owner,
            role="owner",
        )
        archived_project = _insert_project(
            session, archived_workspace, retained_source=False
        )
        _grant_project(session, archived_project, archived_owner, "manager")
        archived_actor = _insert_actor(session, archived_owner)
        _grant_actor_resources(
            session,
            archived_actor,
            workspace_ids=(archived_workspace,),
        )
        with pytest.raises(ProjectNotFound):
            require_project_access(session, _context(archived_actor), archived_project)
        with pytest.raises(WorkspaceNotFound):
            require_project_creation(session, _context(archived_actor), archived_workspace)

        owner_id = _insert_user(session)
        personal_id = _insert_workspace(
            session,
            kind="personal",
            owner_id=owner_id,
            member_id=owner_id,
            role="owner",
        )
        personal_project = _insert_project(session, personal_id)
        for malformed_role in ("admin", "owner"):
            intruder_id = _insert_user(session)
            session.add(
                WorkspaceMembership(
                    workspace_id=personal_id,
                    user_id=intruder_id,
                    role=malformed_role,
                )
            )
            session.flush()
            intruder_actor = _insert_actor(session, intruder_id)
            _grant_actor_resources(
                session,
                intruder_actor,
                workspace_ids=(personal_id,),
            )
            with pytest.raises(ProjectNotFound):
                require_project_access(session, _context(intruder_actor), personal_project)
            with pytest.raises(WorkspaceNotFound):
                require_project_creation(session, _context(intruder_actor), personal_id)
            with pytest.raises(WorkspaceNotFound):
                personal_workspace_id(session, _context(intruder_actor))


def test_sql_visibility_filters_before_limit_and_hides_unknown_ids(database: Database) -> None:
    with database.session() as session:
        user_id = _insert_user(session)
        actor = _insert_actor(session, user_id, frozenset({"app.read"}))
        workspace_id = _insert_workspace(session)
        hidden_id = _insert_project(session, workspace_id, name="A hidden project")
        visible_id = _insert_project(session, workspace_id, name="Z visible project")
        _grant_project(session, visible_id, user_id, "viewer")
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
        context = _context(actor)

        first_visible = session.scalar(
            select(Project.id)
            .where(Project.id.in_(visible_project_ids_statement(session, context)))
            .order_by(Project.name)
            .limit(1)
        )
        assert first_visible == visible_id
        assert hidden_id not in _visible_ids(session, context)
        assert require_project_access(session, context, visible_id).project_id == visible_id
        with pytest.raises(ProjectNotFound) as hidden:
            require_project_access(session, context, hidden_id)
        with pytest.raises(ProjectNotFound) as missing:
            require_project_access(session, context, "not-a-project-id")
        assert hidden.value.args == missing.value.args == ("Project not found.",)


def test_preloaded_workspace_membership_is_not_a_stale_grant(database: Database) -> None:
    with database.session() as session:
        user_id = _insert_user(session)
        actor = _insert_actor(session, user_id)
        workspace_id = _insert_workspace(
            session,
            member_id=user_id,
            role="owner",
        )
        project_id = _insert_project(session, workspace_id)
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
        context = _context(actor)
        assert require_project_access(session, context, project_id).workspace_role == "owner"

        cached_membership = session.get(WorkspaceMembership, (workspace_id, user_id))
        cached_user = session.get(User, user_id)
        cached_credential = session.get(UserCredential, user_id)
        cached_token = session.get(ApiToken, actor.token_id)
        assert cached_membership is not None and cached_membership.role == "owner"
        assert cached_user is not None and cached_user.is_active
        assert cached_credential is not None and cached_token is not None

        session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
            .values(role="member"),
            execution_options={"synchronize_session": False},
        )
        assert cached_membership.role == "owner"
        downgraded = require_project_access(session, context, project_id)
        assert downgraded.workspace_role == "member"
        assert downgraded.allowed_actions == CONTENT_ACTIONS
        with pytest.raises(AccessDenied):
            require_project_access(session, context, project_id, ProjectAction.MANAGE_MEMBERS)
        assert project_id in _visible_ids(session, context)
        assert project_id not in _visible_ids(session, context, ProjectAction.MANAGE_MEMBERS)


def test_project_membership_removal_is_fresh_for_same_context(database: Database) -> None:
    with database.session() as session:
        user_id = _insert_user(session)
        actor = _insert_actor(session, user_id)
        workspace_id = _insert_workspace(session)
        project_id = _insert_project(session, workspace_id)
        membership = _grant_project(session, project_id, user_id, "manager")
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
        context = _context(actor)
        assert require_project_access(session, context, project_id).project_role == "manager"
        cached_membership = session.get(ProjectMembership, (project_id, user_id))
        assert cached_membership is membership

        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            ),
            execution_options={"synchronize_session": False},
        )
        assert session.get(ProjectMembership, (project_id, user_id)) is cached_membership
        with pytest.raises(ProjectNotFound):
            require_project_access(session, context, project_id)


def test_account_disable_and_auth_revision_change_invalidate_same_context(
    database: Database,
) -> None:
    with database.session() as session:
        user_id = _insert_user(session)
        actor = _insert_actor(session, user_id)
        workspace_id = _insert_workspace(session)
        project_id = _insert_project(session, workspace_id)
        _grant_project(session, project_id, user_id, "viewer")
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
        context = _context(actor)
        assert require_project_access(session, context, project_id)
        cached_user = session.get(User, user_id)
        cached_credential = session.get(UserCredential, user_id)
        cached_token = session.get(ApiToken, actor.token_id)
        assert (
            cached_user is not None and cached_credential is not None and cached_token is not None
        )

        session.execute(
            update(User).where(User.id == user_id).values(is_active=False),
            execution_options={"synchronize_session": False},
        )
        assert cached_user.is_active is True
        with pytest.raises(AccessDenied):
            require_project_access(session, context, project_id)

        session.execute(
            update(User).where(User.id == user_id).values(is_active=True),
            execution_options={"synchronize_session": False},
        )
        session.execute(
            update(UserCredential).where(UserCredential.user_id == user_id).values(auth_revision=2),
            execution_options={"synchronize_session": False},
        )
        with pytest.raises(AccessDenied):
            require_project_access(session, context, project_id)


def test_token_and_client_revocation_invalidate_same_context(database: Database) -> None:
    with database.session() as session:
        user_id = _insert_user(session)
        scopes = frozenset({"app.read"})
        client = AutomationClient(
            id="synthetic-policy-client",
            name="Synthetic policy client",
            subject="synthetic-policy-client-subject",
            redirect_uris_json=["http://localhost/callback"],
            allowed_scopes_json=sorted(scopes),
            target_instance_id="synthetic-instance",
            canonical_origin="http://localhost",
            created_by="test",
            user_id=user_id,
            user_auth_revision=1,
        )
        session.add(client)
        session.flush()
        actor = _insert_actor(session, user_id, scopes, client_id=client.id)
        second_actor = _insert_actor(session, user_id, scopes, client_id=client.id)
        workspace_id = _insert_workspace(session)
        project_id = _insert_project(session, workspace_id)
        _grant_project(session, project_id, user_id, "viewer")
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
        _grant_actor_resources(session, second_actor, workspace_ids=(workspace_id,))
        context = _context(actor)
        second_context = _context(second_actor)
        assert require_project_access(session, context, project_id)
        cached_token = session.get(ApiToken, actor.token_id)
        cached_client = session.get(AutomationClient, client.id)
        assert cached_token is not None and cached_client is not None

        session.execute(
            update(ApiToken).where(ApiToken.id == actor.token_id).values(revoked_at=NOW),
            execution_options={"synchronize_session": False},
        )
        assert cached_token.revoked_at is None
        with pytest.raises(AccessDenied):
            require_project_access(session, context, project_id)

        assert require_project_access(session, second_context, project_id)
        session.execute(
            update(AutomationClient).where(AutomationClient.id == client.id).values(revoked_at=NOW),
            execution_options={"synchronize_session": False},
        )
        assert cached_client.revoked_at is None
        with pytest.raises(AccessDenied):
            require_project_access(session, second_context, project_id)


def test_policy_keeps_caller_transaction_and_rolls_back_outer_failure(database: Database) -> None:
    with database.session() as setup:
        user_id = _insert_user(setup)
        actor = _insert_actor(setup, user_id)
        workspace_id = _insert_workspace(setup)
        project_id = _insert_project(setup, workspace_id, name="Original")
        _grant_project(setup, project_id, user_id, "viewer")
        _grant_actor_resources(setup, actor, workspace_ids=(workspace_id,))
        context = _context(actor)

    with pytest.raises(RuntimeError, match="outer rollback sentinel"):
        with database.session() as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.name = "Uncommitted caller change"
            assert require_project_access(session, context, project_id).project_id == project_id
            assert session.in_transaction()
            raise RuntimeError("outer rollback sentinel")

    with database.session() as verify:
        project = verify.get(Project, project_id)
        assert project is not None and project.name == "Original"


def test_context_and_action_inputs_are_exact_and_host_actors_are_rejected(
    database: Database,
) -> None:
    with pytest.raises(TypeError, match="authenticated user actor"):
        AccessContext(cast(UserActor, HostActor("pandrator-cli")))
    with pytest.raises(TypeError, match="authenticated user actor"):
        AccessContext(cast(UserActor, None))

    with database.session() as session:
        user_id = _insert_user(session)
        actor = _insert_actor(session, user_id)
        workspace_id = _insert_workspace(session)
        project_id = _insert_project(session, workspace_id)
        _grant_project(session, project_id, user_id, "viewer")
        _grant_actor_resources(session, actor, workspace_ids=(workspace_id,))
        context = _context(actor)

        invalid_context = cast(AccessContext, None)
        for operation in (
            lambda: require_project_access(session, invalid_context, project_id),
            lambda: visible_project_ids_statement(session, invalid_context),
            lambda: require_project_creation(session, invalid_context, workspace_id),
            lambda: personal_workspace_id(session, invalid_context),
        ):
            with pytest.raises(TypeError, match="access context is required"):
                operation()

        with pytest.raises(TypeError, match="valid project action"):
            require_project_access(session, context, project_id, cast(ProjectAction, "read"))
        with pytest.raises(TypeError, match="valid project action"):
            visible_project_ids_statement(session, context, cast(ProjectAction, "read"))
