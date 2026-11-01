from __future__ import annotations

import socket
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest
import requests.sessions
from flask import Flask, g, has_request_context, request
from flask.testing import FlaskClient
from sqlalchemy import Connection, delete, event, select, update
from sqlalchemy.orm import Session, object_session
from werkzeug.test import TestResponse

from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import (
    CredentialResources,
    default_personal_resources,
)
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenProjectGrant
from pandrator.web.api import create_app
from pandrator.web.auth import AuthService, BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    Artifact,
    Job,
    OutcomePlan,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import Project, ProjectMembership
from tests.web_test_support import prepare_web_test_data_root

WRITE_SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def block_network_attempts() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked_request(*_args: object, **_kwargs: object) -> None:
        attempts.append("requests.sessions.Session.request")
        raise AssertionError("Protected-session mutation tests must not use the network.")

    def blocked_connect(*_args: object, **_kwargs: object) -> None:
        attempts.append("socket.socket.connect")
        raise AssertionError("Protected-session mutation tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked_request)
    patcher.setattr(socket.socket, "connect", blocked_connect)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts, f"Unexpected network attempts: {attempts!r}"


@dataclass(frozen=True, slots=True)
class MutationSeed:
    users: dict[str, str]
    project_id: str
    shared_workspace_id: str
    source_session_id: str
    language_session_id: str
    trashed_session_id: str
    private_session_id: str
    private_project_id: str
    private_workspace_id: str


@dataclass(frozen=True, slots=True)
class Credential:
    client: FlaskClient
    headers: dict[str, str]
    subject: str
    token_id: str | None = None


@dataclass(frozen=True, slots=True)
class MutationCase:
    app: Flask
    bootstrap: BootstrapTokenStore
    auth: AuthService
    database: Database
    seed: MutationSeed
    sessions: Any


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
            login_name=f"mutation-{user_id[:12]}",
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


def _session_record(
    name: str,
    *,
    session_id: str | None = None,
    status: str = "idle",
    revision: int = 1,
    trashed_at: datetime | None = None,
) -> SessionRecord:
    return SessionRecord(
        id=session_id or new_id(),
        name=name,
        workflow_kind="subtitles",
        interface_mode="advanced",
        source_language="en",
        target_language="pl",
        included_stages_json=[],
        status=status,
        revision=revision,
        created_at=_NOW,
        updated_at=_NOW,
        trashed_at=trashed_at,
    )


@pytest.fixture
def mutation_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MutationCase]:
    data_root = tmp_path / "data"
    prepare_web_test_data_root(data_root)
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
            primary_workspace = initial_personal_workspace(session)
            users: dict[str, str] = {}
            for role in ("viewer", "reviewer", "editor", "manager"):
                users[role], _workspace_id = _insert_user(
                    session,
                    name=f"Session mutation {role}",
                )
            users["instance_admin"], _admin_workspace_id = _insert_user(
                session,
                name="Session mutation instance admin",
                is_instance_admin=True,
            )
            users["other_workspace_owner"], other_workspace_id = _insert_user(
                session,
                name="Session mutation second workspace owner",
            )
            shared_workspace = Workspace(
                id=new_id(),
                kind="team",
                name="Session mutation shared workspace",
                state="active",
                revision=1,
            )
            session.add(shared_workspace)
            session.flush()
            initial_owner_id = primary_workspace.personal_owner_user_id
            assert initial_owner_id is not None
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=shared_workspace.id,
                        user_id=initial_owner_id,
                        role="owner",
                    ),
                    WorkspaceMembership(
                        workspace_id=shared_workspace.id,
                        user_id=users["manager"],
                        role="admin",
                    ),
                    WorkspaceMembership(
                        workspace_id=shared_workspace.id,
                        user_id=users["editor"],
                        role="member",
                    ),
                ]
            )
            session.flush()

            source = _session_record("Mutation source session")
            project = create_session_project(
                session,
                source,
                workspace_id=shared_workspace.id,
            )
            language = _session_record("Mutation language session")
            create_session_project(session, language, project_id=project.id)
            checkpoint_hash = "c" * 64
            checkpoint = Artifact(
                id=new_id(),
                workspace_id=shared_workspace.id,
                project_id=project.id,
                session_id=source.id,
                kind="srt",
                role="correction",
                relative_path="artifacts/protected-session-mutation.srt",
                content_hash=checkpoint_hash,
                state="current",
                metadata_json={},
                created_at=_NOW,
                updated_at=_NOW,
            )
            session.add(checkpoint)
            session.flush()
            translations = TranslationProject(
                id=new_id(),
                project_root_id=project.id,
                name="Session mutation translation fixture",
                source_session_id=source.id,
                checkpoint_artifact_id=checkpoint.id,
                source_content_hash=checkpoint_hash,
                source_language="en",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
            session.add(translations)
            session.flush()
            session.add(
                TranslationProjectBranch(
                    id=new_id(),
                    project_id=translations.id,
                    session_id=language.id,
                    target_language="pl",
                    source_checkpoint_artifact_id=checkpoint.id,
                    source_content_hash=checkpoint_hash,
                    created_at=_NOW,
                )
            )
            trashed = _session_record(
                "Mutation trashed session",
                status="trashed",
                revision=2,
                trashed_at=_NOW,
            )
            create_session_project(session, trashed, project_id=project.id)

            shared_active_job = Job(
                id=new_id(),
                kind="protected-session-mutation-fixture",
                workspace_id=shared_workspace.id,
                project_id=project.id,
                session_id=source.id,
                status="queued",
            )
            session.add(shared_active_job)

            private = _session_record("Private session with active job")
            private_project = create_session_project(
                session,
                private,
                workspace_id=other_workspace_id,
            )
            active_job = Job(
                id=new_id(),
                kind="protected-session-mutation-fixture",
                workspace_id=other_workspace_id,
                project_id=private_project.id,
                session_id=private.id,
                status="queued",
            )
            session.add(active_job)
            # Keep the historical keys: editor is now a workspace member and
            # manager is a workspace administrator; viewer/reviewer stay external.
            for role in ("viewer", "reviewer", "editor", "manager"):
                session.add(
                    ProjectMembership(
                        project_id=project.id,
                        user_id=users[role],
                        role=role,
                    )
                )

        allowed_users = frozenset(users.values())
        real_solo_access_allowed = services.auth.solo_access_allowed

        def allow_only_synthetic_users(
            user_id: str | None,
            auth_revision: int | None,
        ) -> bool:
            if user_id in allowed_users and auth_revision == 1:
                return True
            return real_solo_access_allowed(user_id, auth_revision)

        monkeypatch.setattr(services.auth, "solo_access_allowed", allow_only_synthetic_users)
        yield MutationCase(
            app=app,
            bootstrap=bootstrap,
            auth=services.auth,
            database=database,
            seed=MutationSeed(
                users=users,
                project_id=project.id,
                shared_workspace_id=shared_workspace.id,
                source_session_id=source.id,
                language_session_id=language.id,
                trashed_session_id=trashed.id,
                private_session_id=private.id,
                private_project_id=private_project.id,
                private_workspace_id=other_workspace_id,
            ),
            sessions=services.sessions,
        )
    finally:
        services.close()


def _browser_credential(
    case: MutationCase,
    user_key: str,
    *,
    scopes: frozenset[str] = WRITE_SCOPES,
) -> Credential:
    user_id = case.seed.users[user_key]
    client = case.app.test_client()
    grant = case.bootstrap.issue(
        user_id=user_id,
        auth_revision=1,
        subject=f"session-mutation-test:{user_id}",
        scopes=scopes,
    )
    response = client.post("/api/v1/auth/bootstrap", json={"token": grant})
    assert response.status_code == 200, response.get_json()
    return Credential(
        client=client,
        headers={"X-CSRF-Token": response.get_json()["csrf_token"]},
        subject=f"session-mutation-test:{user_id}",
    )


def _api_credential(
    case: MutationCase,
    user_key: str,
    *,
    scopes: frozenset[str] = WRITE_SCOPES,
) -> Credential:
    user_id = case.seed.users[user_key]
    resource_grants = None
    if user_key in {"viewer", "reviewer", "editor", "manager"}:
        with case.database.session() as session:
            personal_resources = default_personal_resources(session, user_id)
        resource_grants = CredentialResources(
            workspace_ids=personal_resources.workspace_ids,
            project_ids=frozenset({case.seed.project_id}),
        )
    token, raw_token = case.auth.create_api_token(
        "Synthetic protected-session mutation token",
        scopes=scopes,
        user_id=user_id,
        auth_revision=1,
        resource_grants=resource_grants,
    )
    assert token.subject is not None
    return Credential(
        client=case.app.test_client(),
        headers={"Authorization": f"Bearer {raw_token}"},
        subject=token.subject,
        token_id=token.id,
    )


def _patch(
    credential: Credential,
    session_id: str,
    *,
    revision: int,
    changes: dict[str, Any],
    idempotency_key: str | None = None,
) -> TestResponse:
    headers = {**credential.headers, "If-Match": f'"{revision}"'}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return credential.client.patch(
        f"/api/v1/sessions/{session_id}",
        json=changes,
        headers=headers,
    )


def _delete(credential: Credential, session_id: str, *, revision: int) -> TestResponse:
    return credential.client.delete(
        f"/api/v1/sessions/{session_id}",
        headers={**credential.headers, "If-Match": f'"{revision}"'},
    )


def _restore(credential: Credential, session_id: str, *, revision: int) -> TestResponse:
    return credential.client.post(
        f"/api/v1/sessions/{session_id}/restore",
        headers={**credential.headers, "If-Match": f'"{revision}"'},
    )


def _error_without_request_id(response: TestResponse) -> dict[str, Any]:
    payload = response.get_json()
    request_id = payload["error"].pop("request_id")
    assert isinstance(request_id, str) and request_id
    return payload


def _resource_snapshot(case: MutationCase, session_id: str) -> tuple[Any, ...]:
    with case.database.snapshot_session() as session:
        record = (
            session.execute(select(SessionRecord.__table__).where(SessionRecord.id == session_id))
            .mappings()
            .one_or_none()
        )
        if record is None:
            return None, None, ()
        project_id = record["project_id"]
        project = (
            session.execute(select(Project.__table__).where(Project.id == project_id))
            .mappings()
            .one_or_none()
        )
        outcomes = tuple(
            dict(row)
            for row in session.execute(
                select(OutcomePlan.__table__).where(OutcomePlan.session_id == session_id)
            ).mappings()
        )
        return dict(record), dict(project) if project is not None else None, outcomes


def _receipt(
    case: MutationCase,
    *,
    subject: str,
    key: str,
) -> dict[str, Any] | None:
    with case.database.snapshot_session() as session:
        row = (
            session.execute(
                select(ApiIdempotency.__table__).where(
                    ApiIdempotency.principal_subject == subject,
                    ApiIdempotency.operation_id == "updateSession",
                    ApiIdempotency.idempotency_key == key,
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


@pytest.mark.parametrize(
    ("user_key", "transport"),
    [("editor", "browser"), ("editor", "api")],
)
def test_workspace_member_can_edit_source_and_language_sessions_with_real_http_credentials(
    mutation_case: MutationCase,
    user_key: str,
    transport: str,
) -> None:
    with mutation_case.database.snapshot_session() as session:
        shared_project = session.get(Project, mutation_case.seed.project_id)
        private_project = session.get(Project, mutation_case.seed.private_project_id)
        assert shared_project is not None and private_project is not None
        assert shared_project.workspace_id == mutation_case.seed.shared_workspace_id
        assert private_project.workspace_id == mutation_case.seed.private_workspace_id
        assert mutation_case.seed.shared_workspace_id != mutation_case.seed.private_workspace_id
        legacy_membership = session.get(
            ProjectMembership,
            (mutation_case.seed.project_id, mutation_case.seed.users["editor"]),
        )
        workspace_membership = session.get(
            WorkspaceMembership,
            (
                mutation_case.seed.shared_workspace_id,
                mutation_case.seed.users["editor"],
            ),
        )
        assert legacy_membership is not None and legacy_membership.role == "editor"
        assert workspace_membership is not None and workspace_membership.role == "member"
    credential = (
        _browser_credential(mutation_case, user_key)
        if transport == "browser"
        else _api_credential(mutation_case, user_key)
    )
    for session_id in (
        mutation_case.seed.source_session_id,
        mutation_case.seed.language_session_id,
    ):
        current = credential.client.get(
            f"/api/v1/sessions/{session_id}", headers=credential.headers
        )
        assert current.status_code == 200
        expected_role = "source" if session_id == mutation_case.seed.source_session_id else "branch"
        assert current.get_json()["translation_project"]["role"] == expected_role
        response = _patch(
            credential,
            session_id,
            revision=current.get_json()["revision"],
            changes={"interface_mode": "simple"},
        )
        assert response.status_code == 200, response.get_json()
        assert response.headers["ETag"] == f'"{response.get_json()["revision"]}"'
        assert response.get_json()["interface_mode"] == "simple"


@pytest.mark.parametrize("user_key", ["viewer", "reviewer"])
def test_read_only_roles_are_denied_before_revision_busy_or_idempotency_state(
    mutation_case: MutationCase,
    user_key: str,
) -> None:
    credential = _api_credential(mutation_case, user_key)
    with mutation_case.database.snapshot_session() as session:
        legacy_membership = session.get(
            ProjectMembership,
            (mutation_case.seed.project_id, mutation_case.seed.users[user_key]),
        )
        assert legacy_membership is not None and legacy_membership.role == user_key
        assert (
            session.get(
                WorkspaceMembership,
                (
                    mutation_case.seed.shared_workspace_id,
                    mutation_case.seed.users[user_key],
                ),
            )
            is None
        )
    session_id = mutation_case.seed.source_session_id
    key = f"denied-{user_key}-mutation"
    before = _resource_snapshot(mutation_case, session_id)
    trashed_before = _resource_snapshot(
        mutation_case,
        mutation_case.seed.trashed_session_id,
    )

    response = _patch(
        credential,
        session_id,
        revision=999,
        changes={"interface_mode": "simple"},
        idempotency_key=key,
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "scope_denied"
    serialized_error = str(response.get_json())
    assert "revision_conflict" not in serialized_error
    assert "current_revision" not in serialized_error

    trash = _delete(credential, session_id, revision=999)
    assert trash.status_code == 403
    assert trash.get_json()["error"]["code"] == "scope_denied"
    assert "session_busy" not in str(trash.get_json())

    restore = _restore(
        credential,
        mutation_case.seed.trashed_session_id,
        revision=999,
    )
    assert restore.status_code == 403
    assert restore.get_json()["error"]["code"] == "scope_denied"

    assert _receipt(mutation_case, subject=credential.subject, key=key) is None
    assert _resource_snapshot(mutation_case, session_id) == before
    assert (
        _resource_snapshot(mutation_case, mutation_case.seed.trashed_session_id) == trashed_before
    )
    assert (
        _resource_snapshot(mutation_case, mutation_case.seed.trashed_session_id)[0]["status"]
        == "trashed"
    )


def test_workspace_manager_can_trash_and_restore_a_project_session(
    mutation_case: MutationCase,
) -> None:
    credential = _api_credential(mutation_case, "manager")
    session_id = mutation_case.seed.source_session_id
    with mutation_case.database.snapshot_session() as session:
        initial_owner_id = initial_personal_workspace(session).personal_owner_user_id
        assert initial_owner_id is not None
        initial_owner_membership = session.get(
            WorkspaceMembership,
            (mutation_case.seed.shared_workspace_id, initial_owner_id),
        )
        manager_membership = session.get(
            WorkspaceMembership,
            (
                mutation_case.seed.shared_workspace_id,
                mutation_case.seed.users["manager"],
            ),
        )
        assert initial_owner_membership is not None and initial_owner_membership.role == "owner"
        assert manager_membership is not None and manager_membership.role == "admin"
    with mutation_case.database.immediate_session() as session:
        session.execute(delete(Job).where(Job.session_id == session_id))

    trashed = _delete(credential, session_id, revision=1)
    assert trashed.status_code == 200, trashed.get_json()
    assert trashed.headers["ETag"] == '"2"'
    assert trashed.get_json()["status"] == "trashed"

    restored = _restore(credential, session_id, revision=2)
    assert restored.status_code == 200, restored.get_json()
    assert restored.headers["ETag"] == '"3"'
    assert restored.get_json()["status"] == "idle"
    with mutation_case.database.snapshot_session() as session:
        record = session.get(SessionRecord, session_id)
        assert record is not None
        assert record.trashed_at is None
        assert record.purge_after is None


def test_project_only_reader_cannot_trash_or_restore_and_invisible_sessions_match_missing(
    mutation_case: MutationCase,
) -> None:
    credential = _api_credential(mutation_case, "viewer")
    source_id = mutation_case.seed.source_session_id
    trashed_id = mutation_case.seed.trashed_session_id
    private_id = mutation_case.seed.private_session_id
    source_before = _resource_snapshot(mutation_case, source_id)
    trashed_before = _resource_snapshot(mutation_case, trashed_id)
    private_before = _resource_snapshot(mutation_case, private_id)

    trash = _delete(credential, source_id, revision=1)
    assert trash.status_code == 403
    assert trash.get_json()["error"]["code"] == "scope_denied"
    assert "session_busy" not in str(trash.get_json())

    restore = _restore(credential, trashed_id, revision=2)
    assert restore.status_code == 403
    assert restore.get_json()["error"]["code"] == "scope_denied"
    assert _resource_snapshot(mutation_case, source_id) == source_before
    assert _resource_snapshot(mutation_case, trashed_id) == trashed_before

    hidden_edit = _patch(
        credential,
        private_id,
        revision=999,
        changes={"interface_mode": "simple"},
        idempotency_key="hidden-session-no-receipt",
    )
    missing_edit = _patch(
        credential,
        "missing-session-mutation",
        revision=999,
        changes={"interface_mode": "simple"},
    )
    hidden_trash = _delete(
        credential,
        private_id,
        revision=999,
    )
    missing_trash = _delete(credential, "missing-session-mutation", revision=999)
    hidden_restore = _restore(
        credential,
        private_id,
        revision=999,
    )
    missing_restore = _restore(credential, "missing-session-mutation", revision=999)
    assert hidden_edit.status_code == missing_edit.status_code == 404
    assert _error_without_request_id(hidden_edit) == _error_without_request_id(missing_edit)
    assert hidden_trash.status_code == missing_trash.status_code == 404
    assert _error_without_request_id(hidden_trash) == _error_without_request_id(missing_trash)
    assert hidden_restore.status_code == missing_restore.status_code == 404
    assert _error_without_request_id(hidden_restore) == _error_without_request_id(missing_restore)
    assert "999" not in str(hidden_edit.get_json())
    assert "busy" not in str(hidden_trash.get_json()).lower()
    assert (
        _receipt(
            mutation_case,
            subject=credential.subject,
            key="hidden-session-no-receipt",
        )
        is None
    )
    assert _resource_snapshot(mutation_case, private_id) == private_before


def test_instance_administrator_without_project_grant_has_no_session_access(
    mutation_case: MutationCase,
) -> None:
    credential = _api_credential(
        mutation_case, "instance_admin", scopes=WRITE_SCOPES | frozenset({"app.admin"})
    )
    with mutation_case.database.immediate_session() as session:
        # Keep the User-role denial observable even with an overbroad synthetic
        # credential ceiling; app.admin still supplies no project membership.
        session.add(
            ApiTokenProjectGrant(
                token_id=credential.token_id,
                project_id=mutation_case.seed.project_id,
            )
        )
    hidden = _patch(
        credential,
        mutation_case.seed.source_session_id,
        revision=999,
        changes={"interface_mode": "simple"},
    )
    missing = _patch(
        credential,
        "missing-session-mutation",
        revision=999,
        changes={"interface_mode": "simple"},
    )
    assert hidden.status_code == missing.status_code == 404
    assert _error_without_request_id(hidden) == _error_without_request_id(missing)


@pytest.mark.parametrize("idempotency_key", [None, "stale-revision-receipt"])
def test_authorized_stale_revision_preserves_session_and_has_no_receipt(
    mutation_case: MutationCase,
    idempotency_key: str | None,
) -> None:
    credential = _api_credential(mutation_case, "editor")
    session_id = mutation_case.seed.source_session_id
    before = _resource_snapshot(mutation_case, session_id)
    response = _patch(
        credential,
        session_id,
        revision=0,
        changes={"interface_mode": "simple"},
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "revision_conflict"
    assert "Expected revision 0" in response.get_json()["error"]["message"]
    assert _resource_snapshot(mutation_case, session_id) == before
    if idempotency_key is not None:
        assert _receipt(mutation_case, subject=credential.subject, key=idempotency_key) is None


def test_allowed_idempotent_update_replays_without_applying_twice(
    mutation_case: MutationCase,
) -> None:
    credential = _api_credential(mutation_case, "editor")
    session_id = mutation_case.seed.source_session_id
    key = "allowed-session-replay-01"
    first = _patch(
        credential,
        session_id,
        revision=1,
        changes={"interface_mode": "simple"},
        idempotency_key=key,
    )
    replay = _patch(
        credential,
        session_id,
        revision=1,
        changes={"interface_mode": "simple"},
        idempotency_key=key,
    )
    assert first.status_code == replay.status_code == 200
    assert first.get_json() == replay.get_json()
    assert first.headers["ETag"] == replay.headers["ETag"] == '"2"'
    assert "Idempotency-Replayed" not in first.headers
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert _resource_snapshot(mutation_case, session_id)[0]["revision"] == 2


@pytest.mark.parametrize(
    ("membership_change", "expected_status"),
    [("legacy_project_role", 200), ("workspace_revocation", 404)],
)
def test_workspace_membership_authority_precedes_saved_idempotent_replay(
    mutation_case: MutationCase,
    membership_change: str,
    expected_status: int,
) -> None:
    credential = _api_credential(mutation_case, "editor")
    session_id = mutation_case.seed.source_session_id
    key = "membership-revocation-replay"
    successful = _patch(
        credential,
        session_id,
        revision=1,
        changes={"interface_mode": "simple"},
        idempotency_key=key,
    )
    assert successful.status_code == 200
    receipt_before = _receipt(mutation_case, subject=credential.subject, key=key)
    assert receipt_before is not None
    resource_before = _resource_snapshot(mutation_case, session_id)

    with mutation_case.database.immediate_session() as session:
        membership = session.get(
            ProjectMembership,
            (mutation_case.seed.project_id, mutation_case.seed.users["editor"]),
        )
        assert membership is not None
        if membership_change == "workspace_revocation":
            workspace_membership = session.get(
                WorkspaceMembership,
                (
                    mutation_case.seed.shared_workspace_id,
                    mutation_case.seed.users["editor"],
                ),
            )
            assert workspace_membership is not None
            assert workspace_membership.role == "member"
            session.delete(workspace_membership)
            session.delete(membership)
        else:
            membership.role = "viewer"

    retry = _patch(
        credential,
        session_id,
        revision=1,
        changes={"interface_mode": "simple"},
        idempotency_key=key,
    )
    assert retry.status_code == expected_status
    if membership_change == "legacy_project_role":
        assert retry.get_json() == successful.get_json()
        assert retry.headers["Idempotency-Replayed"] == "true"
        with mutation_case.database.snapshot_session() as session:
            legacy_membership = session.get(
                ProjectMembership,
                (mutation_case.seed.project_id, mutation_case.seed.users["editor"]),
            )
            workspace_membership = session.get(
                WorkspaceMembership,
                (
                    mutation_case.seed.shared_workspace_id,
                    mutation_case.seed.users["editor"],
                ),
            )
            assert legacy_membership is not None and legacy_membership.role == "viewer"
            assert workspace_membership is not None and workspace_membership.role == "member"
    else:
        assert "Idempotency-Replayed" not in retry.headers
        assert retry.get_json()["error"]["code"] == "not_found"
    assert _receipt(mutation_case, subject=credential.subject, key=key) == receipt_before
    assert _resource_snapshot(mutation_case, session_id) == resource_before


def test_restore_caller_owned_transaction_rolls_back_and_standalone_returns_detached(
    mutation_case: MutationCase,
) -> None:
    session_id = mutation_case.seed.trashed_session_id
    with pytest.raises(RuntimeError, match="outer transaction rollback"):
        with mutation_case.database.immediate_session() as session:
            restored = mutation_case.sessions.restore(
                session_id,
                2,
                db_session=session,
            )
            assert object_session(restored) is session
            assert session.in_transaction()
            assert restored.status == "idle"
            restored.name = "Uncommitted caller edit"
            raise RuntimeError("outer transaction rollback")

    with mutation_case.database.snapshot_session() as session:
        record = session.get(SessionRecord, session_id)
        assert record is not None
        assert record.status == "trashed"
        assert record.trashed_at is not None
        assert record.name == "Mutation trashed session"

    detached = mutation_case.sessions.restore(session_id, 2)
    assert object_session(detached) is None
    assert detached.status == "idle"
    assert detached.revision == 3


@pytest.mark.parametrize("revocation", ["workspace_membership", "token"])
def test_writer_wait_rechecks_workspace_membership_or_token_revocation(
    mutation_case: MutationCase,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    credential = _api_credential(mutation_case, "editor")
    session_id = mutation_case.seed.source_session_id
    request_thread_name = f"session-mutation-{revocation}-request"
    expected_path = f"/api/v1/sessions/{session_id}"
    writer_entry_reached = Event()
    release_request_writer = Event()
    route_begin_expected = Event()
    request_begin_attempted = Event()
    response_future: Future[TestResponse] = Future()

    original_immediate_session = mutation_case.database.immediate_session

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
            with mutation_case.app.test_client() as client:
                response = client.patch(
                    f"/api/v1/sessions/{session_id}",
                    json={"interface_mode": "simple"},
                    headers={
                        **credential.headers,
                        "If-Match": '"1"',
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
                and request.method == "PATCH"
                and request.path == expected_path
                and getattr(g, "principal", None) is not None
            ):
                assert request.endpoint is not None
                assert request.endpoint.rsplit(".", 1)[-1] == "session_update"
                if not writer_entry_reached.is_set():
                    writer_entry_reached.set()
                    if not release_request_writer.wait(timeout=15):
                        raise TimeoutError("The bounded route-writer gate was not released.")
                    route_begin_expected.set()
                    if busy_timeout_ms is None:
                        busy_timeout_ms = 15_000
            with original_immediate_session(busy_timeout_ms=busy_timeout_ms) as session:
                yield session

        scoped_patch.setattr(
            mutation_case.database,
            "immediate_session",
            controlled_immediate_session,
        )
        listener_registered = False
        try:
            event.listen(
                mutation_case.database.engine,
                "before_cursor_execute",
                before_cursor_execute,
            )
            listener_registered = True
            thread.start()
            assert writer_entry_reached.wait(timeout=15), (
                "The request did not reach its post-authentication writer entry."
            )
            with mutation_case.database.immediate_session() as writer:
                if revocation == "workspace_membership":
                    membership = writer.get(
                        WorkspaceMembership,
                        (
                            mutation_case.seed.shared_workspace_id,
                            mutation_case.seed.users["editor"],
                        ),
                    )
                    assert membership is not None and membership.role == "member"
                    writer.delete(membership)
                    writer.execute(
                        delete(ProjectMembership).where(
                            ProjectMembership.project_id == mutation_case.seed.project_id,
                            ProjectMembership.user_id == mutation_case.seed.users["editor"],
                        )
                    )
                else:
                    assert credential.token_id is not None
                    writer.execute(
                        update(ApiToken)
                        .where(ApiToken.id == credential.token_id)
                        .values(revoked_at=utcnow())
                    )
                release_request_writer.set()
                assert request_begin_attempted.wait(timeout=15), (
                    "The route did not attempt BEGIN IMMEDIATE while the revocation writer held its lock."
                )
            response = response_future.result(timeout=15)
        finally:
            release_request_writer.set()
            if listener_registered:
                event.remove(
                    mutation_case.database.engine,
                    "before_cursor_execute",
                    before_cursor_execute,
                )
            if thread.is_alive():
                thread.join(timeout=15)
        assert not thread.is_alive(), "The bounded request thread did not finish."

    assert response.status_code == (404 if revocation == "workspace_membership" else 403)
    assert response.get_json()["error"]["code"] == (
        "not_found" if revocation == "workspace_membership" else "scope_denied"
    )
    assert "revision_conflict" not in str(response.get_json())
    assert "current_revision" not in str(response.get_json())
    assert _resource_snapshot(mutation_case, session_id)[0]["revision"] == 1
