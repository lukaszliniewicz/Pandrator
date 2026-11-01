from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAction,
    ProjectNotFound,
)
from pandrator.web.access.project_policy import require_project_access
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.credential_resources import (
    credential_project_clause,
    remove_user_project_resources,
)
from pandrator.web.accounts.execution_actor import UserActor
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
    AutomationCodeProjectGrant,
    AutomationCodeWorkspaceGrant,
    BrowserAuthProjectGrant,
    BrowserAuthWorkspaceGrant,
)
from pandrator.web.database import Database
from pandrator.web.model_base import new_id
from pandrator.web.models import (
    ApiToken,
    AppSetting,
    AutomationClient,
    AutomationEnrollmentGrant,
    SessionRecord,
)
from pandrator.web.projects.models import Project, ProjectMembership
from pandrator.web.projects.sharing_commands import ProjectSharingCommands
from pandrator.web.projects.sharing_contracts import ProjectSharingError
from pandrator.web.projects.sharing_queries import ProjectSharingQueries
from tests.web_test_support import prepare_web_test_data_root

SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Person:
    user_id: str
    context: AccessContext


@dataclass(frozen=True, slots=True)
class SharingCase:
    database: Database
    people: dict[str, Person]
    workspaces: dict[str, str]
    projects: dict[str, str]
    commands: ProjectSharingCommands


def _audit(
    session: Session,
    context: AccessContext,
    *,
    action: str,
    project_id: str,
    metadata: dict[str, str | int],
) -> None:
    assert set(metadata) <= {"user_id", "role", "revision"}
    session.add(
        AppSetting(
            key=f"test.project-sharing.{new_id()}",
            value_json={
                "action": action,
                "project_id": project_id,
                "metadata": metadata,
                "actor_user_id": context.actor.user_id,
            },
        )
    )


def _user(
    session: Session,
    name: str,
    *,
    admin: bool = False,
    active: bool = True,
    login_name: str | None = None,
    credential: bool = True,
) -> str:
    user_id = new_id()
    session.add(
        User(
            id=user_id,
            legacy_subject=None,
            display_name=name,
            is_active=active,
            is_instance_admin=admin,
        )
    )
    session.flush()
    if credential:
        session.add(
            UserCredential(
                user_id=user_id,
                login_name=login_name or f"{name.lower().replace(' ', '-')}-{user_id[:8]}",
                password_hash=None,
                auth_revision=1,
            )
        )
        session.flush()
    return user_id


def _actor(
    session: Session,
    user_id: str,
    *,
    project_ids: tuple[str, ...] = (),
) -> Person:
    token_id = new_id()
    subject = f"sharing-test:{token_id}"
    token_hash = hashlib.sha256(token_id.encode("ascii")).hexdigest()
    session.add(
        ApiToken(
            id=token_id,
            label="Project sharing test",
            token_hash=token_hash,
            token_prefix=token_id[:12],
            subject=subject,
            scopes_json=sorted(SCOPES),
            principal_kind="api_token",
            user_id=user_id,
            user_auth_revision=1,
            created_by="project-view-sharing-test",
        )
    )
    session.flush()
    for project_id in project_ids:
        session.add(ApiTokenProjectGrant(token_id=token_id, project_id=project_id))
    session.flush()
    return Person(
        user_id=user_id,
        context=AccessContext(
            UserActor(
                user_id=user_id,
                auth_revision=1,
                subject=subject,
                principal_kind="api_token",
                scopes=SCOPES,
                token_id=token_id,
            )
        ),
    )


def _workspace(
    session: Session,
    *,
    kind: str,
    name: str,
    owner_id: str | None = None,
    memberships: tuple[tuple[str, str], ...] = (),
) -> str:
    workspace_id = new_id()
    session.add(
        Workspace(
            id=workspace_id,
            kind=kind,
            name=name,
            personal_owner_user_id=owner_id,
            state="active",
            revision=1,
        )
    )
    session.flush()
    for user_id, role in memberships:
        session.add(
            WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role=role)
        )
    session.flush()
    return workspace_id


def _project(
    session: Session,
    workspace_id: str,
    *,
    name: str,
    source_name: str | None = None,
    updated_at: datetime = NOW,
    trashed: bool = False,
) -> tuple[str, str | None]:
    project_id = new_id()
    project = Project(
        id=project_id,
        workspace_id=workspace_id,
        source_session_id=None,
        name=name,
        created_by_user_id=None,
        revision=1,
        updated_at=updated_at,
    )
    session.add(project)
    session.flush()
    if source_name is None:
        return project_id, None
    source_id = new_id()
    session.add(
        SessionRecord(
            id=source_id,
            project_id=project_id,
            name=source_name,
            workflow_kind="translation",
            source_language="en",
            target_language="de",
            status="trashed" if trashed else "idle",
            updated_at=updated_at,
            trashed_at=NOW if trashed else None,
        )
    )
    session.flush()
    project.source_session_id = source_id
    session.flush([project])
    return project_id, source_id


@pytest.fixture
def sharing_case(tmp_path: Path) -> Iterator[SharingCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.immediate_session() as session:
        ids = {
            "manager": _user(session, "Manager"),
            "owner": _user(session, "Owner"),
            "member": _user(session, "Member"),
            "target": _user(session, "Target"),
            "admin": _user(session, "Instance Administrator", admin=True),
        }
        workspaces = {
            "team": _workspace(
                session,
                kind="team",
                name="Private team name",
                memberships=((ids["manager"], "admin"), (ids["member"], "member")),
            ),
            "personal": _workspace(
                session,
                kind="personal",
                name="Owner private workspace",
                owner_id=ids["owner"],
                memberships=((ids["owner"], "owner"),),
            ),
        }
        projects: dict[str, str] = {}
        projects["team"], _ = _project(
            session,
            workspaces["team"],
            name="Team project",
            source_name="Team source",
        )
        projects["personal"], _ = _project(
            session,
            workspaces["personal"],
            name="Personal project",
            source_name="Personal source",
        )
        projects["no_source"], _ = _project(
            session, workspaces["team"], name="No source"
        )
        projects["trashed"], _ = _project(
            session,
            workspaces["team"],
            name="Trashed source",
            source_name="Trashed root",
            trashed=True,
        )
        session.add(
            ProjectMembership(
                project_id=projects["team"],
                user_id=ids["member"],
                role="viewer",
            )
        )
        people = {
            "manager": _actor(
                session,
                ids["manager"],
                project_ids=(projects["team"], projects["no_source"], projects["trashed"]),
            ),
            "owner": _actor(session, ids["owner"], project_ids=(projects["personal"],)),
            "member": _actor(session, ids["member"], project_ids=(projects["team"],)),
            "target": _actor(session, ids["target"]),
            "admin": _actor(
                session,
                ids["admin"],
                project_ids=(projects["team"],),
            ),
        }
        session.add(
            ProjectMembership(
                project_id=projects["team"], user_id=ids["admin"], role="viewer"
            )
        )
    case = SharingCase(
        database=database,
        people=people,
        workspaces=workspaces,
        projects=projects,
        commands=ProjectSharingCommands(audit=_audit),
    )
    try:
        yield case
    finally:
        database.dispose()


def _audit_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(AppSetting.key)).where(
                AppSetting.key.like("test.project-sharing.%")
            )
        )
        or 0
    )


def test_owner_and_team_manager_grant_and_revoke(sharing_case: SharingCase) -> None:
    case = sharing_case
    with case.database.immediate_session() as session:
        team_result = case.commands.grant(
            session,
            case.people["manager"].context,
            case.projects["team"],
            case.people["target"].user_id,
            expected_revision=1,
        )
        assert team_result["revision"] == 2
        target_item = next(
            item
            for item in team_result["items"]
            if item["user_id"] == case.people["target"].user_id
        )
        assert target_item["access"] == "view"
        assert target_item["granted_by_user_id"] == case.people["manager"].user_id
        assert session.get(
            ProjectMembership,
            (case.projects["team"], case.people["target"].user_id),
        ) is not None
        assert session.get(
            WorkspaceMembership,
            (case.workspaces["team"], case.people["target"].user_id),
        ) is None
        assert session.scalar(
            select(func.count(ApiTokenProjectGrant.token_id)).where(
                ApiTokenProjectGrant.token_id == case.people["target"].context.actor.token_id
            )
        ) == 0

        personal_result = case.commands.grant(
            session,
            case.people["owner"].context,
            case.projects["personal"],
            case.people["target"].user_id,
            expected_revision=1,
        )
        assert personal_result["items"][0]["user_id"] == case.people["target"].user_id
        personal_revoke = case.commands.revoke(
            session,
            case.people["owner"].context,
            case.projects["personal"],
            case.people["target"].user_id,
            expected_revision=2,
        )
        assert personal_revoke["revision"] == 3

        revoked = case.commands.revoke(
            session,
            case.people["manager"].context,
            case.projects["team"],
            case.people["target"].user_id,
            expected_revision=2,
        )
        assert revoked["revision"] == 3
        assert all(item["user_id"] != case.people["target"].user_id for item in revoked["items"])
        assert session.get(
            ProjectMembership,
            (case.projects["team"], case.people["target"].user_id),
        ) is None
        assert _audit_count(session) == 4
        repeated_revoke = case.commands.revoke(
            session,
            case.people["manager"].context,
            case.projects["team"],
            case.people["target"].user_id,
            expected_revision=3,
        )
        assert repeated_revoke["revision"] == 3
        assert _audit_count(session) == 4


def test_members_and_unrelated_instance_admin_cannot_manage(sharing_case: SharingCase) -> None:
    case = sharing_case
    for actor_name in ("member", "admin"):
        with pytest.raises(AccessDenied), case.database.immediate_session() as session:
            case.commands.grant(
                session,
                case.people[actor_name].context,
                case.projects["team"],
                case.people["target"].user_id,
                expected_revision=1,
            )
    with case.database.snapshot_session() as session:
        assert session.get(
            ProjectMembership,
            (case.projects["team"], case.people["target"].user_id),
        ) is None


def test_target_source_and_revision_validation_and_legacy_role_normalization(
    sharing_case: SharingCase,
) -> None:
    case = sharing_case
    with case.database.immediate_session() as session:
        inactive = _user(session, "Inactive", active=False)
        no_credential = _user(session, "No Credential", credential=False)
        member_id = case.people["member"].user_id
        for user_id in ("missing-user", inactive, no_credential):
            with pytest.raises(ProjectSharingError) as failure:
                case.commands.grant(
                    session,
                    case.people["manager"].context,
                    case.projects["team"],
                    user_id,
                    expected_revision=1,
                )
            assert failure.value.code == "user_not_available"
        with pytest.raises(ProjectSharingError) as failure:
            case.commands.grant(
                session,
                case.people["manager"].context,
                case.projects["team"],
                member_id,
                expected_revision=1,
            )
        assert failure.value.code == "already_workspace_member"

        for project_id in (case.projects["no_source"], case.projects["trashed"]):
            with pytest.raises(ProjectSharingError) as failure:
                case.commands.grant(
                    session,
                    case.people["manager"].context,
                    project_id,
                    case.people["target"].user_id,
                    expected_revision=1,
                )
            assert failure.value.code == "project_unavailable"

        for revision in (0, True):
            with pytest.raises(ProjectSharingError) as failure:
                case.commands.grant(
                    session,
                    case.people["manager"].context,
                    case.projects["team"],
                    case.people["target"].user_id,
                    expected_revision=revision,
                )
            assert failure.value.code == "invalid_revision"
        with pytest.raises(ProjectSharingError) as failure:
            case.commands.grant(
                session,
                case.people["manager"].context,
                case.projects["team"],
                case.people["target"].user_id,
                expected_revision=2,
            )
        assert failure.value.code == "project_revision_conflict"

        session.add(
            ProjectMembership(
                project_id=case.projects["team"],
                user_id=case.people["target"].user_id,
                role="editor",
            )
        )
        session.flush()
        before_audit_count = _audit_count(session)
        result = case.commands.grant(
            session,
            case.people["manager"].context,
            case.projects["team"],
            case.people["target"].user_id,
            expected_revision=1,
        )
        membership = session.get(
            ProjectMembership,
            (case.projects["team"], case.people["target"].user_id),
        )
        assert membership is not None
        assert membership.role == "viewer"
        assert membership.granted_by_user_id == case.people["manager"].user_id
        assert result["revision"] == 2
        assert _audit_count(session) == before_audit_count + 1

        repeated = case.commands.grant(
            session,
            case.people["manager"].context,
            case.projects["team"],
            case.people["target"].user_id,
            expected_revision=2,
        )
        assert repeated["revision"] == 2
        assert _audit_count(session) == before_audit_count + 1
        with pytest.raises(ProjectSharingError) as failure:
            case.commands.grant(
                session,
                case.people["manager"].context,
                case.projects["team"],
                case.people["target"].user_id,
                expected_revision=1,
            )
        assert failure.value.code == "project_revision_conflict"


def _insert_extra_token(session: Session, user_id: str, *, revoked: bool) -> str:
    token_id = new_id()
    session.add(
        ApiToken(
            id=token_id,
            label="Stale resource test token",
            token_hash=hashlib.sha256(token_id.encode("ascii")).hexdigest(),
            token_prefix=token_id[:12],
            subject=f"stale-token:{token_id}",
            scopes_json=sorted(SCOPES),
            principal_kind="api_token",
            user_id=user_id,
            user_auth_revision=1,
            created_by="project-view-sharing-test",
            revoked_at=NOW if revoked else None,
        )
    )
    session.flush()
    return token_id


def test_revoke_removes_all_four_credential_grants_and_never_revives_old_grants(
    sharing_case: SharingCase,
) -> None:
    case = sharing_case
    target_id = case.people["target"].user_id
    other_id = new_id()
    with case.database.immediate_session() as session:
        other_project, _ = _project(
            session,
            case.workspaces["team"],
            name="Other project",
            source_name="Other source",
        )
        session.add(
            ProjectMembership(project_id=case.projects["team"], user_id=target_id, role="viewer")
        )
        active_token_id = case.people["target"].context.actor.token_id
        assert active_token_id is not None
        stale_token_id = _insert_extra_token(session, target_id, revoked=True)
        session.add_all(
            [
                ApiTokenProjectGrant(token_id=active_token_id, project_id=case.projects["team"]),
                ApiTokenProjectGrant(token_id=active_token_id, project_id=other_project),
                ApiTokenProjectGrant(token_id=stale_token_id, project_id=case.projects["team"]),
                ApiTokenProjectGrant(token_id=stale_token_id, project_id=other_project),
                ApiTokenWorkspaceGrant(token_id=stale_token_id, workspace_id=case.workspaces["team"]),
            ]
        )

        client_id = new_id()
        session.add(
            AutomationClient(
                id=client_id,
                name="Stale sharing client",
                subject=f"stale-client:{client_id}",
                allowed_scopes_json=sorted(SCOPES),
                target_instance_id=new_id(),
                canonical_origin="https://invalid.example",
                created_by="project-view-sharing-test",
                user_id=target_id,
                user_auth_revision=1,
                revoked_at=NOW,
            )
        )
        session.flush()
        code_id = new_id()
        session.add(
            AutomationEnrollmentGrant(
                id=code_id,
                client_id=client_id,
                code_hash=f"hash-{code_id}",
                code_prefix=code_id[:12],
                scopes_json=sorted(SCOPES),
                code_challenge="challenge",
                expires_at=NOW + timedelta(hours=1),
                token_expires_at=NOW + timedelta(hours=2),
            )
        )
        session.flush()
        session.add_all(
            [
                AutomationClientProjectGrant(client_id=client_id, project_id=case.projects["team"]),
                AutomationClientProjectGrant(client_id=client_id, project_id=other_project),
                AutomationClientWorkspaceGrant(client_id=client_id, workspace_id=case.workspaces["team"]),
                AutomationCodeProjectGrant(code_grant_id=code_id, project_id=case.projects["team"]),
                AutomationCodeProjectGrant(code_grant_id=code_id, project_id=other_project),
                AutomationCodeWorkspaceGrant(code_grant_id=code_id, workspace_id=case.workspaces["team"]),
            ]
        )

        browser_id = new_id()
        session.add(
            BrowserAuthSession(
                id=browser_id,
                user_id=target_id,
                token_hash=hashlib.sha256(browser_id.encode("ascii")).hexdigest(),
                auth_revision=1,
                subject=f"stale-browser:{browser_id}",
                principal_kind="owner_session",
                scopes_json=["app.read"],
                created_at=NOW,
                last_seen_at=NOW,
                idle_expires_at=NOW + timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
                revoked_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            [
                BrowserAuthProjectGrant(browser_session_id=browser_id, project_id=case.projects["team"]),
                BrowserAuthProjectGrant(browser_session_id=browser_id, project_id=other_project),
                BrowserAuthWorkspaceGrant(browser_session_id=browser_id, workspace_id=case.workspaces["team"]),
            ]
        )
        session.flush()
        assert require_project_access(
            session, case.people["target"].context, case.projects["team"]
        ).project_id == case.projects["team"]

        result = case.commands.revoke(
            session,
            case.people["manager"].context,
            case.projects["team"],
            target_id,
            expected_revision=1,
        )
        assert result["revision"] == 2
        for model, owner_column, owners in (
            (ApiTokenProjectGrant, ApiTokenProjectGrant.token_id, (active_token_id, stale_token_id)),
            (AutomationClientProjectGrant, AutomationClientProjectGrant.client_id, (client_id,)),
            (BrowserAuthProjectGrant, BrowserAuthProjectGrant.browser_session_id, (browser_id,)),
            (AutomationCodeProjectGrant, AutomationCodeProjectGrant.code_grant_id, (code_id,)),
        ):
            assert session.scalar(
                select(func.count()).select_from(model).where(
                    owner_column.in_(owners), model.project_id == case.projects["team"]
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(model).where(
                    owner_column.in_(owners), model.project_id == other_project
                )
            ) == len(owners)
        assert session.scalar(
            select(func.count()).select_from(ApiTokenWorkspaceGrant).where(
                ApiTokenWorkspaceGrant.token_id.in_((active_token_id, stale_token_id))
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AutomationClientWorkspaceGrant).where(
                AutomationClientWorkspaceGrant.client_id == client_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AutomationCodeWorkspaceGrant).where(
                AutomationCodeWorkspaceGrant.code_grant_id == code_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(BrowserAuthWorkspaceGrant).where(
                BrowserAuthWorkspaceGrant.browser_session_id == browser_id
            )
        ) == 1
        active_token = session.get(ApiToken, active_token_id)
        assert active_token is not None and active_token.revoked_at is None
        with pytest.raises(ProjectNotFound):
            require_project_access(session, case.people["target"].context, case.projects["team"])

        case.commands.grant(
            session,
            case.people["manager"].context,
            case.projects["team"],
            target_id,
            expected_revision=2,
        )
        assert session.get(
            ApiTokenProjectGrant,
            (active_token_id, case.projects["team"]),
        ) is None
        assert session.get(
            WorkspaceMembership,
            (case.workspaces["team"], target_id),
        ) is None
        assert session.scalar(
            select(credential_project_clause(case.people["target"].context.actor))
            .select_from(Project)
            .where(Project.id == case.projects["team"])
        ) is False
        with pytest.raises(ProjectNotFound):
            require_project_access(session, case.people["target"].context, case.projects["team"])

    with case.database.immediate_session() as session:
        assert remove_user_project_resources(session, target_id, other_id) is False


def test_shared_discovery_filters_before_pagination_and_escapes_search(
    sharing_case: SharingCase,
) -> None:
    case = sharing_case
    target = case.people["target"]
    with case.database.immediate_session() as session:
        alpha, _ = _project(
            session,
            case.workspaces["team"],
            name="Alpha root",
            source_name="Needle alpha",
            updated_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        beta, _ = _project(
            session,
            case.workspaces["team"],
            name="Beta root",
            source_name="Needle beta",
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        literal, _ = _project(
            session,
            case.workspaces["team"],
            name="Literal root",
            source_name="Needle% literal",
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        wildcard, _ = _project(
            session,
            case.workspaces["team"],
            name="Wildcard root",
            source_name="NeedleX literal",
            updated_at=datetime(2025, 12, 31, tzinfo=UTC),
        )
        inherited, _ = _project(
            session,
            case.workspaces["team"],
            name="Inherited root",
            source_name="Needle inherited",
            updated_at=datetime(2026, 1, 5, tzinfo=UTC),
        )
        member_id = case.people["member"].user_id
        target_token_id = target.context.actor.token_id
        member_token_id = case.people["member"].context.actor.token_id
        assert target_token_id is not None and member_token_id is not None
        for project_id in (alpha, beta, literal, wildcard):
            session.add(ProjectMembership(project_id=project_id, user_id=target.user_id, role="viewer"))
            session.add(
                ApiTokenProjectGrant(
                    token_id=target_token_id,
                    project_id=project_id,
                )
            )
        # Even a legacy direct row does not put an inherited workspace member
        # in Shared with me.
        session.add(
            ProjectMembership(project_id=inherited, user_id=member_id, role="viewer")
        )
        session.add(
            ApiTokenProjectGrant(token_id=target_token_id, project_id=inherited)
        )
        session.add(
            ApiTokenProjectGrant(token_id=member_token_id, project_id=inherited)
        )
        first_candidate = _user(session, "Candidate underscore", login_name="cand_a_b")
        _user(session, "Candidate wildcard", login_name="cand_axb")
        session.flush()

    statements: list[str] = []

    def record_sql(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", record_sql)
    try:
        with case.database.snapshot_session() as session:
            rows = ProjectSharingQueries.shared_with_me(
                session, target.context, query="needle", limit=1, offset=1
            )
            literal_rows = ProjectSharingQueries.shared_with_me(
                session, target.context, query="needle%", limit=100, offset=0
            )
            candidates = ProjectSharingQueries.search_users(
                session,
                case.people["manager"].context,
                case.projects["team"],
                query="a_",
            )
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record_sql)

    with case.database.snapshot_session() as session:
        inherited_rows = ProjectSharingQueries.shared_with_me(
            session, case.people["member"].context, query="needle"
        )

    assert [row["project_id"] for row in rows] == [beta]
    assert [row["project_id"] for row in literal_rows] == [literal]
    assert "workspace_id" not in rows[0] and "workspace_name" not in rows[0]
    assert [row["user_id"] for row in candidates] == [first_candidate]
    assert inherited_rows == []
    scoped_project_selects = [
        statement
        for statement in statements
        if statement.lstrip().lower().startswith("select")
        and "from projects" in statement.lower()
        and "sessions" in statement.lower()
        and "as source_session_id" in statement.lower()
    ]
    assert len(scoped_project_selects) == 2


def test_audit_failure_rolls_back_grant_revision_and_revoke_cleanup(
    sharing_case: SharingCase,
) -> None:
    case = sharing_case

    def failing_audit(*args: Any, **kwargs: Any) -> None:
        _audit(*args, **kwargs)
        raise RuntimeError("Synthetic project sharing audit failure")

    commands = ProjectSharingCommands(audit=failing_audit)
    with pytest.raises(RuntimeError, match="Synthetic project sharing audit"):
        with case.database.immediate_session() as session:
            commands.grant(
                session,
                case.people["manager"].context,
                case.projects["team"],
                case.people["target"].user_id,
                expected_revision=1,
            )
    with case.database.snapshot_session() as session:
        assert session.scalar(
            select(Project.revision).where(Project.id == case.projects["team"])
        ) == 1
        assert session.get(
            ProjectMembership,
            (case.projects["team"], case.people["target"].user_id),
        ) is None
        assert _audit_count(session) == 0

    with case.database.immediate_session() as session:
        session.add(
            ProjectMembership(
                project_id=case.projects["team"],
                user_id=case.people["target"].user_id,
                role="viewer",
            )
        )
        session.add(
            ApiTokenProjectGrant(
                token_id=case.people["target"].context.actor.token_id,
                project_id=case.projects["team"],
            )
        )
    with pytest.raises(RuntimeError, match="Synthetic project sharing audit"):
        with case.database.immediate_session() as session:
            commands.revoke(
                session,
                case.people["manager"].context,
                case.projects["team"],
                case.people["target"].user_id,
                expected_revision=1,
            )
    with case.database.snapshot_session() as session:
        assert session.scalar(
            select(Project.revision).where(Project.id == case.projects["team"])
        ) == 1
        assert session.get(
            ProjectMembership,
            (case.projects["team"], case.people["target"].user_id),
        ) is not None
        assert session.get(
            ApiTokenProjectGrant,
            (case.people["target"].context.actor.token_id, case.projects["team"]),
        ) is not None
        assert _audit_count(session) == 0


def test_viewer_list_has_an_explicit_bound(sharing_case: SharingCase) -> None:
    case = sharing_case
    with case.database.immediate_session() as session:
        user_ids = [new_id() for _ in range(1001)]
        session.add_all(
            [
                User(
                    id=user_id,
                    display_name="Bounded viewer",
                    is_active=True,
                    is_instance_admin=False,
                )
                for user_id in user_ids
            ]
        )
        session.flush()
        session.add_all(
            [
                UserCredential(
                    user_id=user_id,
                    login_name=f"bounded-viewer-{user_id[:12]}",
                    password_hash=None,
                    auth_revision=1,
                )
                for user_id in user_ids
            ]
        )
        session.add_all(
            [
                ProjectMembership(
                    project_id=case.projects["team"], user_id=user_id, role="viewer"
                )
                for user_id in user_ids
            ]
        )
        session.flush()
        with pytest.raises(ProjectSharingError) as failure:
            ProjectSharingQueries.list_viewers(
                session, case.people["manager"].context, case.projects["team"]
            )
    assert failure.value.code == "validation_error"
    assert failure.value.status_code == 422
    assert failure.value.message == "Too many viewing grants to display."


def test_shared_query_bounds_are_strict(sharing_case: SharingCase) -> None:
    case = sharing_case
    with case.database.snapshot_session() as session:
        for limit, offset in ((True, 0), (101, 0), (50, -1), (50, False)):
            with pytest.raises(ProjectSharingError) as failure:
                ProjectSharingQueries.shared_with_me(
                    session, case.people["target"].context, limit=limit, offset=offset
                )
            assert failure.value.code == "validation_error"
            assert failure.value.message == "Invalid shared-project pagination."
        with pytest.raises(ProjectSharingError) as failure:
            ProjectSharingQueries.shared_with_me(
                session, case.people["target"].context, query="x" * 256
            )
        assert failure.value.message == "Search text must contain at most 255 characters."


def test_manager_can_inspect_with_read_scope_but_cannot_change_viewers(
    sharing_case: SharingCase,
) -> None:
    case = sharing_case
    actor = case.people["owner"].context.actor
    with case.database.immediate_session() as session:
        session.get_one(ApiToken, actor.token_id).scopes_json = ["app.read"]
    context = AccessContext(replace(actor, scopes=frozenset({"app.read"})))
    with case.database.immediate_session() as session:
        assert ProjectSharingQueries.list_viewers(session, context, case.projects["personal"])["revision"] == 1
        assert ProjectSharingQueries.search_users(session, context, case.projects["personal"], "Target")
        with pytest.raises(AccessDenied):
            case.commands.grant(session, context, case.projects["personal"], case.people["target"].user_id, expected_revision=1)
        with pytest.raises(AccessDenied):
            case.commands.revoke(session, context, case.projects["personal"], case.people["target"].user_id, expected_revision=1)


@pytest.mark.parametrize("role", ["manager", "editor", "reviewer", "viewer"])
def test_every_project_only_role_is_read_only(sharing_case: SharingCase, role: str) -> None:
    case = sharing_case
    target = case.people["target"]
    with case.database.immediate_session() as session:
        session.add(ProjectMembership(project_id=case.projects["personal"], user_id=target.user_id, role=role))
        session.add(ApiTokenProjectGrant(token_id=target.context.actor.token_id, project_id=case.projects["personal"]))
    with case.database.snapshot_session() as session:
        access = require_project_access(session, target.context, case.projects["personal"])
        assert access.allowed_actions == frozenset({ProjectAction.READ})
        for action in ProjectAction:
            if action is not ProjectAction.READ:
                with pytest.raises(AccessDenied):
                    require_project_access(session, target.context, case.projects["personal"], action)
