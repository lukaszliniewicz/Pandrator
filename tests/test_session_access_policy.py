from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, update

from pandrator.web.access.contracts import AccessContext, AccessDenied, ProjectAction
from pandrator.web.access.session_policy import (
    SessionNotFound,
    require_session_access,
    visible_session_ids_statement,
)
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import HostActor, UserActor
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.database import Database, sqlite_url
from pandrator.web.model_base import Base, new_id, utcnow
from pandrator.web.models import ApiToken, SessionRecord
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import ProjectMembership
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class Case:
    database: Database
    context: AccessContext
    shared_project: str
    private_project: str


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Case]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        with database.immediate_session() as session:
            owner_workspace = initial_personal_workspace(session)
            reader_id = new_id()
            session.add(User(id=reader_id, display_name="Reader", is_active=True))
            session.flush()
            session.add(UserCredential(user_id=reader_id, login_name="reader", auth_revision=1))
            reader_workspace = Workspace(
                id=new_id(),
                kind="personal",
                name="Reader's workspace",
                personal_owner_user_id=reader_id,
                state="active",
            )
            session.add(reader_workspace)
            session.flush()
            session.add(
                WorkspaceMembership(
                    workspace_id=reader_workspace.id, user_id=reader_id, role="owner"
                )
            )
            private = create_session_project(
                session,
                SessionRecord(id="hidden-source", name="Alpha hidden"),
                workspace_id=owner_workspace.id,
            )
            shared = create_session_project(
                session,
                SessionRecord(id="shared-source", name="Beta shared"),
                workspace_id=owner_workspace.id,
            )
            create_session_project(
                session,
                SessionRecord(id="shared-language", name="Gamma language"),
                project_id=shared.id,
            )
            create_session_project(
                session,
                SessionRecord(id="own-source", name="Zulu own"),
                workspace_id=reader_workspace.id,
            )
            session.add(ProjectMembership(project_id=shared.id, user_id=reader_id, role="viewer"))
            scopes = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
            token_id = new_id()
            subject = f"test-user:{reader_id}"
            session.add(
                ApiToken(
                    id=token_id,
                    label="Synthetic session policy token",
                    token_hash=hashlib.sha256(token_id.encode()).hexdigest(),
                    token_prefix=token_id[:12],
                    subject=subject,
                    scopes_json=sorted(scopes),
                    principal_kind="api_token",
                    user_id=reader_id,
                    user_auth_revision=1,
                )
            )
            session.flush()
            session.add_all(
                (
                    ApiTokenWorkspaceGrant(
                        token_id=token_id,
                        workspace_id=reader_workspace.id,
                    ),
                    ApiTokenProjectGrant(
                        token_id=token_id,
                        project_id=shared.id,
                    ),
                )
            )
            session.flush()
            context = AccessContext(
                UserActor(
                    user_id=reader_id,
                    auth_revision=1,
                    subject=subject,
                    principal_kind="api_token",
                    scopes=scopes,
                    token_id=token_id,
                )
            )
            result = Case(database, context, shared.id, private.id)
        yield result
    finally:
        database.dispose()


def test_source_and_languages_share_visibility_before_pagination(case: Case) -> None:
    with case.database.snapshot_session() as session:
        ids = visible_session_ids_statement(session, case.context)
        visible = set(session.scalars(ids))
        assert visible == {"shared-source", "shared-language", "own-source"}
        first = session.scalar(
            select(SessionRecord.id)
            .where(SessionRecord.id.in_(ids))
            .order_by(SessionRecord.name)
            .limit(1)
        )
        assert first == "shared-source"
        for resource_id in ("shared-source", "shared-language"):
            access = require_session_access(session, case.context, resource_id)
            assert access.project_id == case.shared_project
            assert access.allowed_actions == frozenset({ProjectAction.READ})
            with pytest.raises(AccessDenied):
                require_session_access(session, case.context, resource_id, ProjectAction.EDIT)
        assert set(
            session.scalars(
                visible_session_ids_statement(session, case.context, ProjectAction.EDIT)
            )
        ) == {"own-source"}
        for resource_id, expected_parent in (
            ("missing", None),
            ("hidden-source", None),
            ("shared-language", case.private_project),
        ):
            with pytest.raises(SessionNotFound) as error:
                require_session_access(
                    session,
                    case.context,
                    resource_id,
                    expected_project_id=expected_parent,
                )
            assert error.value.args == ("Session not found.",)
        assert (
            require_session_access(
                session,
                case.context,
                "shared-language",
                expected_project_id=case.shared_project,
            ).project_id
            == case.shared_project
        )


def test_stale_session_and_membership_objects_cannot_preserve_access(case: Case) -> None:
    # Preserve defense against stale pre-contract roots without bypassing current constraints.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0075_mandatory_ownership")
    # This one deliberately corrupted policy fixture restores only the two
    # independent credential ceiling tables; it is not migration qualification.
    Base.metadata.create_all(
        case.database.engine,
        tables=[
            ApiTokenWorkspaceGrant.__table__,
            ApiTokenProjectGrant.__table__,
        ],
    )
    with case.database.immediate_session() as session:
        reader_workspace_id = session.scalar(
            select(Workspace.id).where(
                Workspace.personal_owner_user_id == case.context.actor.user_id
            )
        )
        assert reader_workspace_id is not None
        session.add_all(
            (
                ApiTokenWorkspaceGrant(
                    token_id=case.context.actor.token_id,
                    workspace_id=reader_workspace_id,
                ),
                ApiTokenProjectGrant(
                    token_id=case.context.actor.token_id,
                    project_id=case.shared_project,
                ),
            )
        )
        session.flush()
        cached = session.get(SessionRecord, "shared-language")
        membership = session.get(
            ProjectMembership, (case.shared_project, case.context.actor.user_id)
        )
        assert cached is not None and membership is not None
        session.execute(
            update(SessionRecord)
            .where(SessionRecord.id == cached.id)
            .values(project_id=case.private_project)
            .execution_options(synchronize_session=False)
        )
        assert cached.project_id == case.shared_project
        with pytest.raises(SessionNotFound):
            require_session_access(session, case.context, cached.id)
        session.execute(
            delete(ProjectMembership)
            .where(
                ProjectMembership.project_id == case.shared_project,
                ProjectMembership.user_id == case.context.actor.user_id,
            )
            .execution_options(synchronize_session=False)
        )
        assert membership.role == "viewer"
        with pytest.raises(SessionNotFound):
            require_session_access(session, case.context, "shared-source")
        assert set(session.scalars(visible_session_ids_statement(session, case.context))) == {
            "own-source"
        }


def test_revoked_transport_denies_reused_context(case: Case) -> None:
    with case.database.immediate_session() as session:
        require_session_access(session, case.context, "own-source")
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == case.context.actor.token_id)
            .values(revoked_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(AccessDenied):
            require_session_access(session, case.context, "own-source")
        with pytest.raises(AccessDenied):
            visible_session_ids_statement(session, case.context)


def test_policy_never_flushes_or_commits_and_requires_exact_context(case: Case) -> None:
    with pytest.raises(RuntimeError, match="outer failure"):
        with case.database.immediate_session() as session:
            own = session.get(SessionRecord, "own-source")
            assert own is not None
            own.name = "Uncommitted edit"
            pending = User(display_name="Uncommitted user")
            session.add(pending)
            require_session_access(session, case.context, own.id, ProjectAction.EDIT)
            assert pending.id is None
            for invalid in (None, HostActor("pandrator-cli")):
                with pytest.raises(TypeError, match="access context"):
                    require_session_access(session, cast(AccessContext, invalid), own.id)
            with pytest.raises(TypeError, match="project action"):
                require_session_access(session, case.context, own.id, cast(ProjectAction, "read"))
            assert pending.id is None
            raise RuntimeError("outer failure")
    with case.database.snapshot_session() as session:
        own = session.get(SessionRecord, "own-source")
        assert own is not None and own.name == "Zulu own"
        assert (
            session.scalar(select(User.id).where(User.display_name == "Uncommitted user")) is None
        )
