from __future__ import annotations

import socket
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest
import requests.sessions
from flask import Flask, g, has_request_context, request
from flask.testing import FlaskClient
from pydantic import ValidationError
from sqlalchemy import Connection, delete, event, select, update
from sqlalchemy.orm import Session
from werkzeug.test import TestResponse

from pandrator.runtime import DataPaths
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.api import create_app
from pandrator.web.auth import AuthService, BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    Artifact,
    Job,
    ProjectFolder,
    ProjectFolderMembership,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.project_folders import (
    create_project_folder,
    delete_project_folder,
    replace_folder_projects,
    set_session_project_folders,
)
from pandrator.web.projects.models import Project, ProjectMembership
from pandrator.web.schemas import SessionCreate
from tests.web_test_support import prepare_web_test_data_root

WRITE_SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})


@pytest.fixture(scope="module", autouse=True)
def block_network_attempts() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked_request(*_args: object, **_kwargs: object) -> None:
        attempts.append("requests.sessions.Session.request")
        raise AssertionError("Protected-session creation tests must not use the network.")

    def blocked_connect(*_args: object, **_kwargs: object) -> None:
        attempts.append("socket.socket.connect")
        raise AssertionError("Protected-session creation tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked_request)
    patcher.setattr(socket.socket, "connect", blocked_connect)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts, f"Unexpected network attempts: {attempts!r}"


@dataclass(frozen=True, slots=True)
class CreationCase:
    app: Flask
    bootstrap: BootstrapTokenStore
    auth: AuthService
    database: Database
    paths: DataPaths
    services: Any
    users: dict[str, str]
    personal_workspaces: dict[str, str]
    migration_workspace_id: str
    team_workspace_id: str


@dataclass(frozen=True, slots=True)
class Credential:
    client: FlaskClient
    headers: dict[str, str]
    subject: str
    token_id: str | None = None


def _insert_user(
    session: Session,
    *,
    name: str,
    is_instance_admin: bool = False,
) -> tuple[str, str]:
    user_id = new_id()
    session.add(
        User(
            id=user_id,
            display_name=name,
            is_active=True,
            is_instance_admin=is_instance_admin,
        )
    )
    session.flush()
    session.add(
        UserCredential(
            user_id=user_id,
            login_name=f"session-create-{user_id[:12]}",
            password_hash=None,
            auth_revision=1,
        )
    )
    workspace = Workspace(
        id=new_id(),
        kind="personal",
        name=f"{name}'s workspace",
        personal_owner_user_id=user_id,
        state="active",
        revision=1,
    )
    session.add(workspace)
    session.flush()
    session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user_id,
            role="owner",
        )
    )
    session.flush()
    return user_id, workspace.id


@pytest.fixture
def creation_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CreationCase]:
    data_root = tmp_path / "data"
    paths = prepare_web_test_data_root(data_root)
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=data_root,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]["services"]
    database: Database = services.database
    try:
        with database.immediate_session() as session:
            migration_workspace_id = initial_personal_workspace(session).id
            users: dict[str, str] = {}
            personal_workspaces: dict[str, str] = {}
            for name, is_instance_admin in (
                ("owner", False),
                ("member", False),
                ("manager", False),
                ("editor", False),
                ("guest", False),
                ("instance_admin", True),
                ("other_owner", False),
            ):
                users[name], personal_workspaces[name] = _insert_user(
                    session,
                    name=f"Session creation {name}",
                    is_instance_admin=is_instance_admin,
                )
            team = Workspace(
                id=new_id(),
                kind="team",
                name="Session creation team",
                personal_owner_user_id=None,
                state="active",
                revision=1,
            )
            session.add(team)
            session.flush()
            for name in ("member", "manager", "editor"):
                session.add(
                    WorkspaceMembership(
                        workspace_id=team.id,
                        user_id=users[name],
                        role="member",
                    )
                )
            session.flush()

        # Keep the documented local fixture login bypass restricted to these
        # test users at auth_revision 1; production authentication is untouched.
        synthetic_users = frozenset(users.values())
        real_solo_access_allowed = services.auth.solo_access_allowed

        def allow_only_synthetic_users(
            user_id: str | None,
            auth_revision: int | None,
        ) -> bool:
            if user_id in synthetic_users and auth_revision == 1:
                return True
            return real_solo_access_allowed(user_id, auth_revision)

        monkeypatch.setattr(services.auth, "solo_access_allowed", allow_only_synthetic_users)
        yield CreationCase(
            app=app,
            bootstrap=bootstrap,
            auth=services.auth,
            database=database,
            paths=paths,
            services=services,
            users=users,
            personal_workspaces=personal_workspaces,
            migration_workspace_id=migration_workspace_id,
            team_workspace_id=team.id,
        )
    finally:
        services.close()


def _browser_credential(case: CreationCase, user_key: str) -> Credential:
    user_id = case.users[user_key]
    client = case.app.test_client()
    grant = case.bootstrap.issue(
        user_id=user_id,
        auth_revision=1,
        subject=f"session-creation-test:{user_id}",
        scopes=WRITE_SCOPES | ({"app.admin"} if user_key == "instance_admin" else set()),
    )
    response = client.post("/api/v1/auth/bootstrap", json={"token": grant})
    assert response.status_code == 200, response.get_json()
    return Credential(
        client=client,
        headers={"X-CSRF-Token": response.get_json()["csrf_token"]},
        subject=f"session-creation-test:{user_id}",
    )


def _api_credential(
    case: CreationCase,
    user_key: str,
    *,
    include_team: bool = False,
    project_ids: tuple[str, ...] = (),
) -> Credential:
    user_id = case.users[user_key]
    token, raw_token = case.auth.create_api_token(
        "Synthetic protected-session creation token",
        scopes=WRITE_SCOPES | ({"app.admin"} if user_key == "instance_admin" else set()),
        user_id=user_id,
        auth_revision=1,
        resource_grants=(
            CredentialResources(
                workspace_ids=frozenset(
                    {case.personal_workspaces[user_key], case.team_workspace_id}
                    if include_team
                    else {case.personal_workspaces[user_key]}
                ),
                project_ids=frozenset(project_ids),
            )
            if include_team or project_ids
            else None
        ),
    )
    return Credential(
        client=case.app.test_client(),
        headers={"Authorization": f"Bearer {raw_token}"},
        subject=token.subject,
        token_id=token.id,
    )


def _create(
    credential: Credential,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> TestResponse:
    headers = dict(credential.headers)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return credential.client.post("/api/v1/sessions", json=payload, headers=headers)


def _new_team_workspace(
    case: CreationCase,
    name: str,
    *,
    state: str = "active",
    members: dict[str, str] | None = None,
) -> str:
    with case.database.immediate_session() as session:
        workspace = Workspace(
            id=new_id(),
            kind="team",
            name=name,
            state=state,
            revision=1,
        )
        session.add(workspace)
        session.flush()
        for user_key, role in (members or {}).items():
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    user_id=case.users[user_key],
                    role=role,
                )
            )
        session.flush()
        return workspace.id


def _seed_session(
    case: CreationCase,
    *,
    workspace_id: str,
    name: str,
    members: dict[str, str] | None = None,
    file_contents: str | None = None,
) -> SessionRecord:
    record = case.services.sessions.create(
        name,
        workflow_kind="subtitles",
        source_language="en",
        target_language="pl",
        workspace_id=workspace_id,
    )
    if members:
        with case.database.immediate_session() as session:
            for user_key, role in members.items():
                session.add(
                    ProjectMembership(
                        project_id=record.project_id,
                        user_id=case.users[user_key],
                        role=role,
                        granted_by_user_id=None,
                    )
                )
    if file_contents is not None:
        directory = case.paths.sessions / record.storage_key
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "retained.txt").write_text(file_contents, encoding="utf-8")
    return record


def _add_active_job(case: CreationCase, record: SessionRecord) -> None:
    workspace_id = _project_workspace(case, record.id)
    with case.database.immediate_session() as session:
        session.add(
            Job(
                id=new_id(),
                kind="protected-session-creation-fixture",
                workspace_id=workspace_id,
                project_id=record.project_id,
                session_id=record.id,
                status="queued",
            )
        )


def _project_workspace(case: CreationCase, session_id: str) -> str:
    with case.database.snapshot_session() as session:
        record = session.get(SessionRecord, session_id)
        assert record is not None
        project = session.get(Project, record.project_id)
        assert project is not None
        return project.workspace_id


def _catalog(case: CreationCase) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    with case.database.snapshot_session() as session:
        projects = tuple(
            sorted(
                (
                    row.id,
                    row.workspace_id,
                    row.source_session_id,
                    row.created_by_user_id,
                )
                for row in session.scalars(select(Project))
            )
        )
        records = tuple(
            sorted(
                (
                    row.id,
                    row.project_id,
                    row.name,
                    row.storage_key,
                    row.revision,
                    row.status,
                    row.trashed_at,
                )
                for row in session.scalars(select(SessionRecord))
            )
        )
        return projects, records


def _session_snapshot(case: CreationCase, session_id: str) -> dict[str, Any] | None:
    with case.database.snapshot_session() as session:
        row = (
            session.execute(select(SessionRecord.__table__).where(SessionRecord.id == session_id))
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


def _receipt(
    case: CreationCase,
    *,
    subject: str,
    key: str,
) -> dict[str, Any] | None:
    with case.database.snapshot_session() as session:
        row = (
            session.execute(
                select(ApiIdempotency.__table__).where(
                    ApiIdempotency.principal_subject == subject,
                    ApiIdempotency.operation_id == "createSession",
                    ApiIdempotency.idempotency_key == key,
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


def _storage_snapshot(case: CreationCase) -> tuple[tuple[str, str, bytes], ...]:
    root = case.paths.sessions
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                "directory" if path.is_dir() else "file",
                b"" if path.is_dir() else path.read_bytes(),
            )
            for path in root.rglob("*")
        )
    )


def _error_signature(response: TestResponse) -> tuple[int, str, str]:
    error = response.get_json()["error"]
    return response.status_code, error["code"], error["message"]


def _create_folder(case: CreationCase, name: str, workspace_id: str) -> dict[str, Any]:
    with case.database.immediate_session() as session:
        return create_project_folder(
            session,
            name,
            workspace_id=workspace_id,
        )["folder"]


def _creation_graph_snapshot(case: CreationCase) -> tuple[Any, ...]:
    with case.database.snapshot_session() as session:
        return (
            tuple(
                sorted(
                    (row.id, row.workspace_id, row.source_session_id, row.created_by_user_id)
                    for row in session.scalars(select(Project))
                )
            ),
            tuple(
                sorted(
                    (
                        row.id,
                        row.project_id,
                        row.name,
                        row.storage_key,
                        row.revision,
                        row.status,
                        row.trashed_at,
                    )
                    for row in session.scalars(select(SessionRecord))
                )
            ),
            tuple(
                sorted(
                    (row.project_id, row.user_id, row.role)
                    for row in session.scalars(select(ProjectMembership))
                )
            ),
            tuple(
                sorted(
                    (row.id, row.project_root_id, row.source_session_id)
                    for row in session.scalars(select(TranslationProject))
                )
            ),
            tuple(
                sorted(
                    (row.id, row.project_id, row.session_id, row.target_language)
                    for row in session.scalars(select(TranslationProjectBranch))
                )
            ),
            tuple(
                sorted(
                    (row.id, row.workspace_id, row.revision)
                    for row in session.scalars(select(ProjectFolder))
                )
            ),
            tuple(
                sorted(
                    (
                        row.folder_id,
                        row.project_session_id,
                        row.project_id,
                        row.position,
                    )
                    for row in session.scalars(select(ProjectFolderMembership))
                )
            ),
        )


@pytest.mark.parametrize("paired_fields", [{"folder_id": "folder"}, {"folder_revision": 1}])
def test_session_create_schema_requires_initial_folder_fields_together(
    paired_fields: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"name": "Initial folder", **paired_fields})

    with pytest.raises(ValidationError):
        SessionCreate.model_validate(
            {"name": "Initial folder", "folder_id": "folder", "folder_revision": "1"}
        )


@pytest.mark.parametrize("actor_kind", ["solo", "shared_member"])
def test_session_create_assigns_initial_folder_and_replays_without_refencing(
    creation_case: CreationCase,
    actor_kind: str,
) -> None:
    user_key = "owner" if actor_kind == "solo" else "member"
    workspace_id = (
        creation_case.personal_workspaces[user_key]
        if actor_kind == "solo"
        else creation_case.team_workspace_id
    )
    credential = (
        _browser_credential(creation_case, user_key)
        if actor_kind == "solo"
        else _api_credential(creation_case, user_key, include_team=True)
    )
    folder = _create_folder(creation_case, f"{actor_kind} initial folder", workspace_id)
    key = f"initial-folder-create-{actor_kind}-001"
    payload = {
        "name": f"{actor_kind} filed session",
        "workflow_kind": "subtitles",
        "source_language": "en",
        "workspace_id": workspace_id,
        "multilingual_setup": {"target_languages": ["pl", "de"]},
        "folder_id": folder["id"],
        "folder_revision": folder["revision"],
    }

    first = _create(credential, payload, idempotency_key=key)
    assert first.status_code == 201, first.get_json()
    created_id = first.get_json()["id"]
    before_replay = _creation_graph_snapshot(creation_case)
    receipt_before = _receipt(creation_case, subject=credential.subject, key=key)
    replay = _create(credential, payload, idempotency_key=key)

    assert replay.status_code == 201, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first.get_json()
    assert _creation_graph_snapshot(creation_case) == before_replay
    assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before

    with creation_case.database.immediate_session() as session:
        source = session.get(SessionRecord, created_id)
        assert source is not None
        project = session.get(Project, source.project_id)
        assert project is not None
        project_root_id = project.id
        branch = creation_case.services.sessions.create(
            "Filed Polish branch",
            workflow_kind="subtitles",
            source_language="en",
            target_language="pl",
            project_id=source.project_id,
            workspace_id=workspace_id,
            db_session=session,
        )
        artifact_id = new_id()
        artifact = Artifact(
            id=artifact_id,
            workspace_id=workspace_id,
            project_id=project.id,
            session_id=source.id,
            kind="srt",
            role="correction",
            relative_path=f"synthetic/{new_id()}.srt",
            content_hash="a" * 64,
            state="current",
        )
        translation_project = TranslationProject(
            project_root_id=project.id,
            name="Filed multilingual source",
            source_session_id=source.id,
            checkpoint_artifact_id=artifact_id,
            source_content_hash=artifact.content_hash,
            source_language="en",
        )
        session.add_all([artifact, translation_project])
        session.flush()
        session.add(
            TranslationProjectBranch(
                project_id=translation_project.id,
                session_id=branch.id,
                target_language="pl",
                source_checkpoint_artifact_id=artifact.id,
                source_content_hash=artifact.content_hash,
            )
        )
        session.flush()

        set_session_project_folders(
            session,
            branch.id,
            folder_ids=[folder["id"]],
            expected_revisions={folder["id"]: folder["revision"] + 1},
        )
        memberships = session.scalars(
            select(ProjectFolderMembership).where(
                ProjectFolderMembership.folder_id == folder["id"]
            )
        ).all()
        current_folder = session.get(ProjectFolder, folder["id"])
        membership_values = [
            (membership.project_session_id, membership.project_id)
            for membership in memberships
        ]
        current_folder_revision = current_folder.revision if current_folder is not None else None

    assert current_folder_revision == folder["revision"] + 1
    assert len(membership_values) == 1
    assert membership_values[0] == (created_id, project_root_id)


def test_session_create_without_initial_folder_stays_unfiled(
    creation_case: CreationCase,
) -> None:
    workspace_id = creation_case.personal_workspaces["owner"]
    credential = _browser_credential(creation_case, "owner")
    folder = _create_folder(creation_case, "No initial assignment", workspace_id)

    response = _create(
        credential,
        {"name": "Unfiled session", "workspace_id": workspace_id},
    )

    assert response.status_code == 201, response.get_json()
    with creation_case.database.snapshot_session() as session:
        assert session.scalar(
            select(ProjectFolderMembership).where(
                ProjectFolderMembership.folder_id == folder["id"]
            )
        ) is None
        current_folder = session.get(ProjectFolder, folder["id"])
        assert current_folder is not None and current_folder.revision == folder["revision"]


def test_completed_initial_folder_create_replays_after_folder_deletion(
    creation_case: CreationCase,
) -> None:
    workspace_id = creation_case.personal_workspaces["owner"]
    credential = _browser_credential(creation_case, "owner")
    folder = _create_folder(creation_case, "Deleted after create", workspace_id)
    payload = {
        "name": "Replay after folder deletion",
        "workspace_id": workspace_id,
        "folder_id": folder["id"],
        "folder_revision": folder["revision"],
    }
    key = "initial-folder-replay-after-delete-001"
    first = _create(credential, payload, idempotency_key=key)
    assert first.status_code == 201, first.get_json()

    with creation_case.database.immediate_session() as session:
        delete_project_folder(
            session,
            folder["id"],
            expected_revision=folder["revision"] + 1,
        )
    before_replay = _creation_graph_snapshot(creation_case)
    receipt_before = _receipt(creation_case, subject=credential.subject, key=key)

    replay = _create(credential, payload, idempotency_key=key)

    assert replay.status_code == 201, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first.get_json()
    assert _creation_graph_snapshot(creation_case) == before_replay
    assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before


def test_stale_initial_folder_revision_rolls_back_replacement(
    creation_case: CreationCase,
) -> None:
    target = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Stale folder overwrite",
        members={"manager": "manager"},
    )
    folder = _create_folder(
        creation_case,
        "Concurrent folder change",
        creation_case.team_workspace_id,
    )
    with creation_case.database.immediate_session() as session:
        replace_folder_projects(
            session,
            folder["id"],
            expected_revision=folder["revision"],
            project_ids=[target.id],
        )

    credential = _api_credential(creation_case, "manager", include_team=True)
    key = "stale-initial-folder-replacement-001"
    before_graph = _creation_graph_snapshot(creation_case)
    before_storage = _storage_snapshot(creation_case)
    response = _create(
        credential,
        {
            "name": target.name,
            "workspace_id": creation_case.team_workspace_id,
            "overwrite_session_id": target.id,
            "overwrite_revision": target.revision,
            "folder_id": folder["id"],
            "folder_revision": folder["revision"],
        },
        idempotency_key=key,
    )

    assert _error_signature(response) == (
        409,
        "revision_conflict",
        "A project folder changed in another client.",
    )
    assert _creation_graph_snapshot(creation_case) == before_graph
    assert _storage_snapshot(creation_case) == before_storage
    assert _session_snapshot(creation_case, target.id)["trashed_at"] is None
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


@pytest.mark.parametrize("failure", ["missing", "foreign", "inaccessible", "revoked"])
def test_unavailable_initial_folder_returns_404_without_content_changes(
    creation_case: CreationCase,
    failure: str,
) -> None:
    selected_workspace = creation_case.team_workspace_id
    credential = _api_credential(creation_case, "manager", include_team=True)
    if failure == "missing":
        folder_id = new_id()
    elif failure == "foreign":
        foreign_workspace = _new_team_workspace(
            creation_case,
            "Unrelated initial-folder workspace",
            members={"other_owner": "member"},
        )
        folder_id = _create_folder(
            creation_case,
            "Foreign private folder",
            foreign_workspace,
        )["id"]
    elif failure == "inaccessible":
        selected_workspace = _new_team_workspace(
            creation_case,
            "Inaccessible initial-folder workspace",
            members={"other_owner": "member"},
        )
        folder_id = _create_folder(
            creation_case,
            "Inaccessible folder",
            selected_workspace,
        )["id"]
    else:
        folder_id = _create_folder(
            creation_case,
            "Revoked workspace folder",
            selected_workspace,
        )["id"]
        with creation_case.database.immediate_session() as session:
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == selected_workspace,
                    WorkspaceMembership.user_id == creation_case.users["manager"],
                )
            )

    key = f"unavailable-initial-folder-{failure}-001"
    before_graph = _creation_graph_snapshot(creation_case)
    before_storage = _storage_snapshot(creation_case)
    response = _create(
        credential,
        {
            "name": f"Unavailable folder {failure}",
            "workspace_id": selected_workspace,
            "folder_id": folder_id,
            "folder_revision": 1,
        },
        idempotency_key=key,
    )

    assert response.status_code == 404, response.get_json()
    assert response.get_json()["error"]["code"] == "not_found"
    assert _creation_graph_snapshot(creation_case) == before_graph
    assert _storage_snapshot(creation_case) == before_storage
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_session_create_schema_requires_revision_with_replacement_id() -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"name": "Replacement", "overwrite_revision": 1})


@pytest.mark.parametrize("transport", ["browser", "api"])
def test_default_creation_uses_actor_personal_workspace_and_manager_attribution(
    creation_case: CreationCase,
    transport: str,
) -> None:
    credential = (
        _browser_credential(creation_case, "owner")
        if transport == "browser"
        else _api_credential(creation_case, "owner")
    )
    response = _create(credential, {"name": f"Default {transport} session"})

    assert response.status_code == 201, response.get_json()
    session_id = response.get_json()["id"]
    with creation_case.database.snapshot_session() as session:
        record = session.get(SessionRecord, session_id)
        assert record is not None
        project = session.get(Project, record.project_id)
        assert project is not None
        assert project.workspace_id == creation_case.personal_workspaces["owner"]
        assert project.workspace_id != creation_case.migration_workspace_id
        assert project.created_by_user_id == creation_case.users["owner"]
        assert (
            session.get(
                ProjectMembership,
                (project.id, creation_case.users["owner"]),
            )
            is None
        )
    assert (
        credential.client.get(
            f"/api/v1/sessions/{session_id}", headers=credential.headers
        ).status_code
        == 200
    )


def test_team_member_creation_inherits_shared_access_without_project_grants(
    creation_case: CreationCase,
) -> None:
    manager_project = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Manager-associated team project",
        members={"manager": "manager"},
    )
    creator = _api_credential(creation_case, "member", include_team=True)
    response = _create(
        creator,
        {"name": "Member created", "workspace_id": creation_case.team_workspace_id},
    )

    assert response.status_code == 201, response.get_json()
    created_id = response.get_json()["id"]
    with creation_case.database.snapshot_session() as session:
        created = session.get(SessionRecord, created_id)
        assert created is not None
        project = session.get(Project, created.project_id)
        assert project is not None
        assert project.workspace_id == creation_case.team_workspace_id
        assert project.created_by_user_id == creation_case.users["member"]
        assert (
            session.scalars(
                select(ProjectMembership).where(ProjectMembership.project_id == project.id)
            ).all()
            == []
        )

    creator_read = creator.client.get(f"/api/v1/sessions/{created_id}", headers=creator.headers)
    assert creator_read.status_code == 200, creator_read.get_json()
    assert (
        creator.client.get(
            f"/api/v1/sessions/{manager_project.id}", headers=creator.headers
        ).status_code
        == 200
    )

    manager = _api_credential(creation_case, "manager", include_team=True)
    manager_read = manager.client.get(f"/api/v1/sessions/{created_id}", headers=manager.headers)
    assert manager_read.status_code == 200, manager_read.get_json()
    manager_edit = manager.client.patch(
        f"/api/v1/sessions/{created_id}",
        json={"interface_mode": "simple"},
        headers={**manager.headers, "If-Match": manager_read.headers["ETag"]},
    )
    assert manager_edit.status_code == 200, manager_edit.get_json()
    assert manager_edit.get_json()["interface_mode"] == "simple"

    with creation_case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == creation_case.team_workspace_id,
                WorkspaceMembership.user_id == creation_case.users["member"],
            )
        )

    departed_creator_read = creator.client.get(
        f"/api/v1/sessions/{created_id}", headers=creator.headers
    )
    assert _error_signature(departed_creator_read) == (
        404,
        "not_found",
        "Session not found.",
    )
    remaining_manager_read = manager.client.get(
        f"/api/v1/sessions/{created_id}", headers=manager.headers
    )
    assert remaining_manager_read.status_code == 200, remaining_manager_read.get_json()
    remaining_manager_edit = manager.client.patch(
        f"/api/v1/sessions/{created_id}",
        json={"interface_mode": "advanced"},
        headers={**manager.headers, "If-Match": remaining_manager_read.headers["ETag"]},
    )
    assert remaining_manager_edit.status_code == 200, remaining_manager_edit.get_json()
    assert remaining_manager_edit.get_json()["interface_mode"] == "advanced"

    with creation_case.database.snapshot_session() as session:
        created = session.get(SessionRecord, created_id)
        assert created is not None
        project = session.get(Project, created.project_id)
        assert project is not None
        assert project.created_by_user_id == creation_case.users["member"]
        assert (
            session.scalars(
                select(ProjectMembership).where(ProjectMembership.project_id == project.id)
            ).all()
            == []
        )


@pytest.mark.parametrize(
    ("actor", "workspace_kind"),
    [
        ("guest", "other_personal"),
        ("guest", "other_team"),
        ("guest", "missing"),
        ("guest", "archived"),
        ("instance_admin", "other_team"),
    ],
)
def test_unavailable_workspace_is_fixed_404_before_receipt_or_creation(
    creation_case: CreationCase,
    actor: str,
    workspace_kind: str,
) -> None:
    if workspace_kind == "other_personal":
        workspace_id = creation_case.personal_workspaces["other_owner"]
    elif workspace_kind == "other_team":
        workspace_id = _new_team_workspace(creation_case, f"Unrelated {actor} team")
    elif workspace_kind == "archived":
        workspace_id = _new_team_workspace(
            creation_case,
            "Archived team",
            state="archived",
        )
    else:
        workspace_id = "missing-team-workspace"

    credential = _api_credential(creation_case, actor)
    key = f"denied-{workspace_kind}-{actor}"
    before = _catalog(creation_case)
    response = _create(
        credential,
        {"name": "Denied workspace create", "workspace_id": workspace_id},
        idempotency_key=key,
    )

    assert _error_signature(response) == (404, "not_found", "Workspace not found.")
    assert _catalog(creation_case) == before
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_visible_unicode_duplicate_returns_its_projection(
    creation_case: CreationCase,
) -> None:
    existing = _seed_session(
        creation_case,
        workspace_id=creation_case.personal_workspaces["owner"],
        name="Straße",
    )
    credential = _api_credential(creation_case, "owner")
    key = "visible-unicode-duplicate-001"
    response = _create(
        credential,
        {"name": "  STRASSE  "},
        idempotency_key=key,
    )

    assert response.status_code == 409
    body = response.get_json()["error"]
    assert body["code"] == "duplicate_session"
    assert body["details"]["existing_session"]["id"] == existing.id
    assert body["details"]["existing_session"]["name"] == existing.name
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_shared_and_other_workspace_names_are_distinguished(
    creation_case: CreationCase,
) -> None:
    existing = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Shared title",
        members={"manager": "manager"},
    )
    other_team = _new_team_workspace(
        creation_case,
        "Other team",
        members={"member": "member"},
    )
    _seed_session(
        creation_case,
        workspace_id=other_team,
        name="Other workspace title",
        members={"member": "manager"},
    )
    _seed_session(
        creation_case,
        workspace_id=creation_case.personal_workspaces["other_owner"],
        name="Shared title",
    )
    credential = _api_credential(creation_case, "member", include_team=True)

    hidden_name = _create(
        credential,
        {"name": "Shared title", "workspace_id": creation_case.team_workspace_id},
    )
    other_workspace_name = _create(
        credential,
        {"name": "Other workspace title", "workspace_id": creation_case.team_workspace_id},
    )

    assert hidden_name.status_code == 409, hidden_name.get_json()
    duplicate = hidden_name.get_json()["error"]
    assert duplicate["code"] == "duplicate_session"
    assert duplicate["details"]["existing_session"]["id"] == existing.id
    assert duplicate["details"]["existing_session"]["name"] == existing.name
    assert other_workspace_name.status_code == 201, other_workspace_name.get_json()
    assert other_workspace_name.get_json()["name"] == "Other workspace title"


@pytest.mark.parametrize("with_idempotency", [False, True])
def test_manager_replacement_is_recoverable_and_retains_old_files(
    creation_case: CreationCase,
    with_idempotency: bool,
) -> None:
    old = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Recoverable replacement",
        members={"manager": "manager"},
        file_contents="original project data",
    )
    credential = _api_credential(creation_case, "manager", include_team=True)
    key = "replace-team-session-001" if with_idempotency else None
    payload = {
        "name": old.name,
        "workspace_id": creation_case.team_workspace_id,
        "overwrite_session_id": old.id,
        "overwrite_revision": old.revision,
    }
    response = _create(
        credential,
        payload,
        idempotency_key=key,
    )

    assert response.status_code == 201, response.get_json()
    created_id = response.get_json()["id"]
    assert created_id != old.id
    assert response.headers["ETag"] == f'"{response.get_json()["revision"]}"'
    assert (creation_case.paths.sessions / old.storage_key / "retained.txt").read_text(
        encoding="utf-8"
    ) == "original project data"
    with creation_case.database.snapshot_session() as session:
        old_after = session.get(SessionRecord, old.id)
        created = session.get(SessionRecord, created_id)
        assert old_after is not None and old_after.trashed_at is not None
        assert old_after.revision == old.revision + 1
        assert created is not None and created.project_id != old.project_id
        assert created.storage_key != old.storage_key
        project = session.get(Project, created.project_id)
        assert project is not None
        assert project.workspace_id == creation_case.team_workspace_id
        assert project.created_by_user_id == creation_case.users["manager"]
        assert (
            session.get(
                ProjectMembership,
                (project.id, creation_case.users["manager"]),
            )
            is None
        )
    assert (creation_case.paths.sessions / created.storage_key).is_dir()
    assert not any((creation_case.paths.sessions / created.storage_key).iterdir())
    if key is not None:
        receipt = _receipt(creation_case, subject=credential.subject, key=key)
        assert receipt is not None
        assert receipt["state"] == "completed"
        assert receipt["resource_kind"] == "session"
        assert receipt["resource_id"] == created_id
        receipt_before_replay = receipt
        before_replay = _catalog(creation_case)
        _add_active_job(creation_case, old)
        replay = _create(credential, payload, idempotency_key=key)
        assert replay.status_code == 201, replay.get_json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.get_json() == response.get_json()
        assert _catalog(creation_case) == before_replay
        assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before_replay
    else:
        assert _catalog(creation_case)[1]


def test_missing_overwrite_revision_returns_fixed_428_before_resource_lookup(
    creation_case: CreationCase,
) -> None:
    credential = _api_credential(creation_case, "manager", include_team=True)
    response = _create(
        credential,
        {
            "name": "No revision supplied",
            "workspace_id": creation_case.team_workspace_id,
            "overwrite_session_id": "unknown-session-id",
        },
    )
    assert _error_signature(response) == (
        428,
        "precondition_required",
        "The current overwrite_revision is required to replace a session.",
    )


def test_workspace_member_replacement_checks_revision_before_busy_or_idempotency_state(
    creation_case: CreationCase,
) -> None:
    target = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Editor cannot replace",
        members={"editor": "editor", "manager": "manager"},
    )
    _add_active_job(creation_case, target)
    credential = _api_credential(creation_case, "editor", include_team=True)
    key = "editor-replacement-denied-001"
    before = _session_snapshot(creation_case, target.id)
    response = _create(
        credential,
        {
            "name": target.name,
            "workspace_id": creation_case.team_workspace_id,
            "overwrite_session_id": target.id,
            "overwrite_revision": 999,
        },
        idempotency_key=key,
    )

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "revision_conflict"
    assert error["details"] == {"current_revision": target.revision}
    assert _session_snapshot(creation_case, target.id) == before
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_missing_invisible_and_cross_workspace_replacements_share_session_404(
    creation_case: CreationCase,
) -> None:
    invisible_workspace = _new_team_workspace(creation_case, "Invisible team")
    invisible = _seed_session(
        creation_case,
        workspace_id=invisible_workspace,
        name="Invisible target",
    )
    other_workspace = _seed_session(
        creation_case,
        workspace_id=creation_case.personal_workspaces["other_owner"],
        name="Other workspace target",
        members={"manager": "manager"},
    )
    assert other_workspace.project_id is not None
    credential = _api_credential(
        creation_case,
        "manager",
        include_team=True,
        project_ids=(other_workspace.project_id,),
    )
    visible_elsewhere = credential.client.get(
        f"/api/v1/sessions/{other_workspace.id}", headers=credential.headers
    )
    assert visible_elsewhere.status_code == 200, visible_elsewhere.get_json()
    targets = (
        "missing-session-id",
        invisible.id,
        other_workspace.id,
    )
    for index, target_id in enumerate(targets):
        before = _catalog(creation_case)
        key = f"invisible-target-{index}-0001"
        response = _create(
            credential,
            {
                "name": f"Replacement {index}",
                "workspace_id": creation_case.team_workspace_id,
                "overwrite_session_id": target_id,
                "overwrite_revision": 1,
            },
            idempotency_key=key,
        )
        assert _error_signature(response) == (404, "not_found", "Session not found.")
        assert _catalog(creation_case) == before
        assert _receipt(creation_case, subject=credential.subject, key=key) is None


@pytest.mark.parametrize("with_idempotency", [False, True])
@pytest.mark.parametrize("failure", ["revision", "busy"])
def test_stale_revision_and_busy_replacement_preserve_state(
    creation_case: CreationCase,
    with_idempotency: bool,
    failure: str,
) -> None:
    target = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name=f"{failure} replacement",
        members={"manager": "manager"},
        file_contents="leave this file alone",
    )
    if failure == "busy":
        _add_active_job(creation_case, target)
    credential = _api_credential(creation_case, "manager", include_team=True)
    key = f"{failure}-replacement-failure-001" if with_idempotency else None
    before = _catalog(creation_case)
    before_storage = _storage_snapshot(creation_case)
    submitted_revision = target.revision if failure == "busy" else target.revision + 1
    response = _create(
        credential,
        {
            "name": target.name,
            "workspace_id": creation_case.team_workspace_id,
            "overwrite_session_id": target.id,
            "overwrite_revision": submitted_revision,
        },
        idempotency_key=key,
    )

    if failure == "revision":
        assert response.status_code == 409
        error = response.get_json()["error"]
        assert error["code"] == "revision_conflict"
        assert error["details"] == {"current_revision": target.revision}
    else:
        assert _error_signature(response) == (
            409,
            "session_busy",
            "Stop or cancel active work before replacing this session.",
        )
    assert _catalog(creation_case) == before
    assert _storage_snapshot(creation_case) == before_storage
    if key is not None:
        assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_instance_admin_without_workspace_membership_cannot_create_replacement(
    creation_case: CreationCase,
) -> None:
    target = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Private manager project",
        members={"manager": "manager"},
    )
    credential = _api_credential(creation_case, "instance_admin")
    assert credential.token_id is not None
    with creation_case.database.immediate_session() as session:
        # A retained overbroad credential ceiling is not workspace membership.
        session.add(
            ApiTokenWorkspaceGrant(
                token_id=credential.token_id, workspace_id=creation_case.team_workspace_id
            )
        )
    key = "instance-admin-no-content-001"
    before = _catalog(creation_case)
    before_storage = _storage_snapshot(creation_case)
    response = _create(
        credential,
        {
            "name": target.name,
            "workspace_id": creation_case.team_workspace_id,
            "overwrite_session_id": target.id,
            "overwrite_revision": target.revision,
        },
        idempotency_key=key,
    )

    assert _error_signature(response) == (404, "not_found", "Workspace not found.")
    assert _catalog(creation_case) == before
    assert _storage_snapshot(creation_case) == before_storage
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_replacement_requires_target_to_remain_newest_visible_name(
    creation_case: CreationCase,
) -> None:
    older = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Repeated title",
        members={"manager": "manager"},
    )
    newer = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Repeated title",
        members={"manager": "manager"},
    )
    with creation_case.database.immediate_session() as session:
        session.get(SessionRecord, older.id).updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        session.get(SessionRecord, newer.id).updated_at = datetime(2026, 10, 24, tzinfo=UTC)
    credential = _api_credential(creation_case, "manager", include_team=True)
    key = "newest-target-conflict-001"
    before = _catalog(creation_case)
    response = _create(
        credential,
        {
            "name": older.name,
            "workspace_id": creation_case.team_workspace_id,
            "overwrite_session_id": older.id,
            "overwrite_revision": older.revision,
        },
        idempotency_key=key,
    )

    assert _error_signature(response) == (
        409,
        "overwrite_conflict",
        "The session selected for replacement no longer matches this name.",
    )
    assert _catalog(creation_case) == before
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_allowed_idempotent_create_replays_original_response_unchanged(
    creation_case: CreationCase,
) -> None:
    credential = _api_credential(creation_case, "member", include_team=True)
    payload = {
        "name": "Idempotent team session",
        "workspace_id": creation_case.team_workspace_id,
    }
    key = "allowed-create-replay-001"
    first = _create(credential, payload, idempotency_key=key)
    assert first.status_code == 201, first.get_json()
    created_id = first.get_json()["id"]
    before_catalog = _catalog(creation_case)
    receipt_before = _receipt(creation_case, subject=credential.subject, key=key)
    replay = _create(credential, payload, idempotency_key=key)

    assert replay.status_code == 201, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first.get_json()
    assert replay.headers["ETag"] == first.headers["ETag"]
    assert _catalog(creation_case) == before_catalog
    assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before
    assert _session_snapshot(creation_case, created_id) is not None


@pytest.mark.parametrize("corruption", ["resource_kind", "resource_id", "empty_resource_id"])
def test_replay_rejects_mismatched_resource_without_changing_receipt(
    creation_case: CreationCase,
    corruption: str,
) -> None:
    credential = _api_credential(creation_case, "member", include_team=True)
    payload = {
        "name": f"Mismatched receipt {corruption}",
        "workspace_id": creation_case.team_workspace_id,
    }
    key = f"mismatched-resource-{corruption}"
    first = _create(credential, payload, idempotency_key=key)
    assert first.status_code == 201, first.get_json()
    before_catalog = _catalog(creation_case)
    with creation_case.database.immediate_session() as session:
        receipt = session.scalar(
            select(ApiIdempotency).where(
                ApiIdempotency.principal_subject == credential.subject,
                ApiIdempotency.operation_id == "createSession",
                ApiIdempotency.idempotency_key == key,
            )
        )
        assert receipt is not None
        if corruption == "resource_kind":
            receipt.resource_kind = "project"
        elif corruption == "resource_id":
            receipt.resource_id = new_id()
        else:
            receipt.resource_id = ""
    receipt_before = _receipt(creation_case, subject=credential.subject, key=key)

    replay = _create(credential, payload, idempotency_key=key)

    assert _error_signature(replay) == (404, "not_found", "Session not found.")
    assert _catalog(creation_case) == before_catalog
    assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before


@pytest.mark.parametrize(
    "revocation",
    ["workspace_membership", "created_project_grant", "account", "token"],
)
def test_replay_rechecks_creation_and_resource_access_without_changing_receipt(
    creation_case: CreationCase,
    revocation: str,
) -> None:
    credential = _api_credential(creation_case, "member", include_team=True)
    payload = {
        "name": f"Replay after {revocation}",
        "workspace_id": creation_case.team_workspace_id,
    }
    key = f"revocation-replay-{revocation}-001"
    first = _create(credential, payload, idempotency_key=key)
    assert first.status_code == 201, first.get_json()
    created_id = first.get_json()["id"]
    created = _session_snapshot(creation_case, created_id)
    assert created is not None
    project_id = created["project_id"]
    receipt_before = _receipt(creation_case, subject=credential.subject, key=key)
    assert receipt_before is not None

    with creation_case.database.immediate_session() as session:
        if revocation == "workspace_membership":
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == creation_case.team_workspace_id,
                    WorkspaceMembership.user_id == creation_case.users["member"],
                )
            )
        elif revocation == "created_project_grant":
            session.add(
                ProjectMembership(
                    project_id=project_id,
                    user_id=creation_case.users["member"],
                    role="manager",
                    granted_by_user_id=creation_case.users["member"],
                )
            )
            session.flush()
            session.execute(
                delete(ProjectMembership).where(
                    ProjectMembership.project_id == project_id,
                    ProjectMembership.user_id == creation_case.users["member"],
                )
            )
            retained = session.get(
                WorkspaceMembership,
                (creation_case.team_workspace_id, creation_case.users["member"]),
            )
            assert retained is not None and retained.role == "member"
        elif revocation == "account":
            session.execute(
                update(User).where(User.id == creation_case.users["member"]).values(is_active=False)
            )
        else:
            assert credential.token_id is not None
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == credential.token_id)
                .values(revoked_at=utcnow())
            )

    replay = _create(credential, payload, idempotency_key=key)
    if revocation == "created_project_grant":
        assert replay.status_code == 201, replay.get_json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.get_json() == first.get_json()
    else:
        expected = (
            (404, "not_found", "Workspace not found.")
            if revocation == "workspace_membership"
            else (401, "authentication_required", "Authentication is required.")
        )
        assert _error_signature(replay) == expected
        assert "Idempotency-Replayed" not in replay.headers
    assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before


def test_old_canonical_create_receipt_replays_after_optional_schema_expansion(
    creation_case: CreationCase,
) -> None:
    credential = _api_credential(creation_case, "owner")
    existing = creation_case.services.sessions.create(
        "Legacy canonical replay",
        workspace_id=creation_case.personal_workspaces["owner"],
    )
    saved_response = credential.client.get(
        f"/api/v1/sessions/{existing.id}", headers=credential.headers
    )
    assert saved_response.status_code == 200
    legacy_payload = {
        "name": "Legacy canonical replay",
        "workflow_kind": "audiobook",
        "interface_mode": "simple",
        "source_language": "auto",
        "target_language": None,
        "workflow_preset": "custom",
        "included_stages": [],
        "overwrite_session_id": None,
        "multilingual_setup": None,
    }
    key = "old-canonical-create-receipt-001"
    receipt = ApiIdempotency(
        principal_subject=credential.subject,
        operation_id="createSession",
        idempotency_key=key,
        request_digest=creation_case.services.idempotency.request_digest(
            "createSession",
            legacy_payload,
        ),
        state="completed",
        status_code=201,
        response_json=saved_response.get_json(),
        resource_kind="session",
        resource_id=existing.id,
        expires_at=utcnow() + timedelta(hours=1),
    )
    with creation_case.database.immediate_session() as session:
        session.add(receipt)
    receipt_before = _receipt(creation_case, subject=credential.subject, key=key)
    before = _catalog(creation_case)

    replay = _create(credential, {"name": "Legacy canonical replay"}, idempotency_key=key)

    assert replay.status_code == 201, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == saved_response.get_json()
    assert _catalog(creation_case) == before
    assert _receipt(creation_case, subject=credential.subject, key=key) == receipt_before


def test_failure_after_trash_rolls_back_and_removes_only_new_empty_directory(
    creation_case: CreationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Fail after trash",
        members={"manager": "manager"},
        file_contents="preserve old bytes",
    )
    credential = _api_credential(creation_case, "manager", include_team=True)
    key = "injected-post-trash-failure-001"
    before_catalog = _catalog(creation_case)
    before_storage = _storage_snapshot(creation_case)

    def fail_completion(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected failure after trash and before receipt completion")

    monkeypatch.setattr(creation_case.services.idempotency, "complete", fail_completion)
    with pytest.raises(RuntimeError, match="after trash"):
        _create(
            credential,
            {
                "name": target.name,
                "workspace_id": creation_case.team_workspace_id,
                "overwrite_session_id": target.id,
                "overwrite_revision": target.revision,
            },
            idempotency_key=key,
        )

    assert _catalog(creation_case) == before_catalog
    assert _storage_snapshot(creation_case) == before_storage
    assert _session_snapshot(creation_case, target.id)["trashed_at"] is None
    assert _receipt(creation_case, subject=credential.subject, key=key) is None


@pytest.mark.parametrize("with_idempotency", [False, True])
def test_directory_collision_rolls_back_without_removing_existing_empty_directory(
    creation_case: CreationCase,
    monkeypatch: pytest.MonkeyPatch,
    with_idempotency: bool,
) -> None:
    target = _seed_session(
        creation_case,
        workspace_id=creation_case.team_workspace_id,
        name="Keep existing directories",
        members={"manager": "manager"},
        file_contents="original project bytes",
    )
    collision_key = new_id()
    collision = creation_case.paths.sessions / collision_key
    collision.mkdir()
    before_catalog = _catalog(creation_case)
    before_storage = _storage_snapshot(creation_case)
    credential = _api_credential(creation_case, "manager", include_team=True)
    generated = iter((new_id(), collision_key))
    monkeypatch.setattr("pandrator.web.session_routes.new_id", lambda: next(generated))
    key = "existing-empty-directory-collision" if with_idempotency else None

    with pytest.raises(FileExistsError):
        _create(
            credential,
            {
                "name": target.name,
                "workspace_id": creation_case.team_workspace_id,
                "overwrite_session_id": target.id,
                "overwrite_revision": target.revision,
            },
            idempotency_key=key,
        )

    assert collision.is_dir()
    assert _catalog(creation_case) == before_catalog
    assert _storage_snapshot(creation_case) == before_storage
    if key is not None:
        assert _receipt(creation_case, subject=credential.subject, key=key) is None


def test_writer_wait_rechecks_team_membership_before_creating_or_reserving(
    creation_case: CreationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _api_credential(creation_case, "member", include_team=True)
    request_thread_name = "session-create-writer-revocation-request"
    expected_path = "/api/v1/sessions"
    writer_entry_reached = Event()
    release_request_writer = Event()
    route_begin_expected = Event()
    request_begin_attempted = Event()
    response_future: Future[TestResponse] = Future()
    key = "writer-wait-membership-revocation-001"
    payload = {
        "name": "Must not create after revocation",
        "workspace_id": creation_case.team_workspace_id,
    }
    before_catalog = _catalog(creation_case)
    before_storage = _storage_snapshot(creation_case)
    original_immediate_session = creation_case.database.immediate_session

    def before_cursor_execute(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            current_thread().name == request_thread_name
            and route_begin_expected.is_set()
            and statement.lstrip().upper() == "BEGIN IMMEDIATE"
        ):
            request_begin_attempted.set()

    def make_request() -> None:
        try:
            with creation_case.app.test_client() as client:
                response = client.post(
                    "/api/v1/sessions",
                    json=payload,
                    headers={
                        **credential.headers,
                        "Idempotency-Key": key,
                    },
                )
            response_future.set_result(response)
        except BaseException as error:
            response_future.set_exception(error)

    thread = Thread(target=make_request, name=request_thread_name)

    with monkeypatch.context() as scoped_patch:

        @contextmanager
        def controlled_immediate_session(
            *,
            busy_timeout_ms: int | None = None,
        ) -> Iterator[Session]:
            if (
                current_thread().name == request_thread_name
                and has_request_context()
                and request.method == "POST"
                and request.path == expected_path
                and getattr(g, "principal", None) is not None
            ):
                assert request.endpoint is not None
                assert request.endpoint.rsplit(".", 1)[-1] == "session_create"
                writer_entry_reached.set()
                if not release_request_writer.wait(timeout=15):
                    raise TimeoutError("The bounded route-writer gate was not released.")
                route_begin_expected.set()
                if busy_timeout_ms is None:
                    busy_timeout_ms = 15_000
            with original_immediate_session(busy_timeout_ms=busy_timeout_ms) as session:
                yield session

        scoped_patch.setattr(
            creation_case.database,
            "immediate_session",
            controlled_immediate_session,
        )
        listener_registered = False
        try:
            event.listen(
                creation_case.database.engine,
                "before_cursor_execute",
                before_cursor_execute,
            )
            listener_registered = True
            thread.start()
            assert writer_entry_reached.wait(timeout=15), (
                "The request did not reach its post-authentication writer entry."
            )
            with original_immediate_session() as writer:
                writer.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == creation_case.team_workspace_id,
                        WorkspaceMembership.user_id == creation_case.users["member"],
                    )
                )
                release_request_writer.set()
                assert request_begin_attempted.wait(timeout=15), (
                    "The route did not attempt BEGIN IMMEDIATE while revocation held the writer lock."
                )
            response = response_future.result(timeout=15)
        finally:
            release_request_writer.set()
            if listener_registered:
                event.remove(
                    creation_case.database.engine,
                    "before_cursor_execute",
                    before_cursor_execute,
                )
            if thread.is_alive():
                thread.join(timeout=15)
        assert not thread.is_alive(), "The bounded request thread did not finish."

    assert _error_signature(response) == (404, "not_found", "Workspace not found.")
    assert _catalog(creation_case) == before_catalog
    assert _storage_snapshot(creation_case) == before_storage
    assert _receipt(creation_case, subject=credential.subject, key=key) is None
