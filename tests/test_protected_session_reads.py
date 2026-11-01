from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import requests.sessions
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, object_session

from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.access.session_queries import (
    get_session_record,
    list_session_records,
)
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import (
    CredentialOwner,
    CredentialResources,
    record_resources,
)
from pandrator.web.accounts.execution_actor import HostActor, UserActor
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.api import create_app
from pandrator.web.auth import AuthService, BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.model_base import new_id, utcnow
from pandrator.web.models import (
    ApiToken,
    Artifact,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.models import ProjectMembership
from tests.web_test_support import prepare_web_test_data_root

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
READ_SCOPES = frozenset({"app.read"})


@pytest.fixture(scope="module", autouse=True)
def block_network_attempts() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked_request(*_args: object, **_kwargs: object) -> None:
        attempts.append("requests.sessions.Session.request")
        raise AssertionError("Protected-session tests must not use the network.")

    def blocked_connect(*_args: object, **_kwargs: object) -> None:
        attempts.append("socket.socket.connect")
        raise AssertionError("Protected-session tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked_request)
    patcher.setattr(socket.socket, "connect", blocked_connect)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts, f"Unexpected network attempts: {attempts!r}"


@dataclass(frozen=True, slots=True)
class SeededSessions:
    ids: dict[str, str]
    shared_project_id: str
    private_project_id: str


@dataclass(frozen=True, slots=True)
class ProtectedSessionCase:
    database: Database
    context: AccessContext
    reader_id: str
    token_id: str
    sessions: SeededSessions


@dataclass(frozen=True, slots=True)
class HttpSessionCase:
    app: Flask
    client: FlaskClient
    bootstrap: BootstrapTokenStore
    auth: AuthService
    database: Database
    reader_id: str
    owner_session_id: str
    sessions: SeededSessions


def _insert_user(
    session: Session,
    *,
    display_name: str,
    is_instance_admin: bool = False,
) -> tuple[str, str]:
    user_id = new_id()
    session.add(
        User(
            id=user_id,
            display_name=display_name,
            is_active=True,
            is_instance_admin=is_instance_admin,
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
    workspace = Workspace(
        id=new_id(),
        kind="personal",
        name=f"{display_name}'s workspace",
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


def _insert_actor(
    session: Session,
    user_id: str,
    scopes: frozenset[str] = READ_SCOPES,
    *,
    resource_grants: CredentialResources | None = None,
) -> tuple[AccessContext, str]:
    credential = session.get(UserCredential, user_id)
    assert credential is not None
    token_id = new_id()
    subject = f"test-user:{user_id}"
    session.add(
        ApiToken(
            id=token_id,
            label="Synthetic protected-session read token",
            token_hash=hashlib.sha256(token_id.encode("ascii")).hexdigest(),
            token_prefix=token_id[:12],
            subject=subject,
            scopes_json=sorted(scopes),
            principal_kind="api_token",
            user_id=user_id,
            user_auth_revision=credential.auth_revision,
            created_by="protected-session-read-test",
        )
    )
    session.flush()
    record_resources(
        session,
        CredentialOwner.TOKEN,
        token_id,
        resource_grants if resource_grants is not None else CredentialResources(),
    )
    return (
        AccessContext(
            UserActor(
                user_id=user_id,
                auth_revision=credential.auth_revision,
                subject=subject,
                principal_kind="api_token",
                scopes=scopes,
                token_id=token_id,
            )
        ),
        token_id,
    )


def _session_record(
    record_id: str,
    name: str,
    *,
    updated_at: datetime,
    source_language: str = "en",
    target_language: str | None = None,
    trashed_at: datetime | None = None,
) -> SessionRecord:
    return SessionRecord(
        id=record_id,
        name=name,
        workflow_kind="subtitles",
        source_language=source_language,
        target_language=target_language,
        included_stages_json=[],
        status="idle",
        revision=1,
        created_at=updated_at,
        updated_at=updated_at,
        trashed_at=trashed_at,
    )


def _seed_sessions(
    session: Session,
    *,
    reader_id: str,
    reader_workspace_id: str,
    owner_workspace_id: str,
) -> SeededSessions:
    ids = {
        "private": "private-hit",
        "private_trashed": "private-trash",
        "shared_source": "shared-source",
        "shared_branch": "shared-language",
        "own": "own-source",
        "tie_a": "own-tie-a",
        "tie_z": "own-tie-z",
        "id_search": "only-id-marker",
        "source_search": "source-only",
        "target_search": "target-only",
        "own_trashed": "own-trash",
    }
    private_project = create_session_project(
        session,
        _session_record(
            ids["private"],
            "A needle private project",
            updated_at=NOW + timedelta(days=4),
            target_language="pl",
        ),
        workspace_id=owner_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["private_trashed"],
            "A needle private trashed project",
            updated_at=NOW + timedelta(days=5),
            trashed_at=NOW,
        ),
        workspace_id=owner_workspace_id,
    )
    shared_project = create_session_project(
        session,
        _session_record(
            ids["shared_source"],
            "B needle shared source",
            updated_at=NOW + timedelta(days=3),
            target_language="pl",
        ),
        workspace_id=owner_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["shared_branch"],
            "Shared language branch",
            updated_at=NOW - timedelta(days=2),
            target_language="de",
        ),
        project_id=shared_project.id,
    )
    create_session_project(
        session,
        _session_record(ids["own"], "Reader's own source", updated_at=NOW - timedelta(days=1)),
        workspace_id=reader_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["tie_a"],
            "C needle reader project A",
            updated_at=NOW + timedelta(days=1),
        ),
        workspace_id=reader_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["tie_z"],
            "D needle reader project Z",
            updated_at=NOW + timedelta(days=1),
        ),
        workspace_id=reader_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["id_search"],
            "Identifier search row",
            updated_at=NOW - timedelta(days=3),
        ),
        workspace_id=reader_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["source_search"],
            "Source language search row",
            updated_at=NOW - timedelta(days=4),
            source_language="source-marker",
        ),
        workspace_id=reader_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["target_search"],
            "Target language search row",
            updated_at=NOW - timedelta(days=5),
            target_language="target-marker",
        ),
        workspace_id=reader_workspace_id,
    )
    create_session_project(
        session,
        _session_record(
            ids["own_trashed"],
            "E needle reader trashed project",
            updated_at=NOW + timedelta(days=6),
            trashed_at=NOW,
        ),
        workspace_id=reader_workspace_id,
    )

    shared_hash = "a" * 64
    private_hash = "b" * 64
    shared_checkpoint = Artifact(
        id="shared-checkpoint",
        workspace_id=owner_workspace_id,
        project_id=shared_project.id,
        session_id=ids["shared_source"],
        kind="srt",
        role="correction",
        relative_path="artifacts/protected-session-shared.srt",
        content_hash=shared_hash,
        state="current",
        metadata_json={},
        created_at=NOW,
        updated_at=NOW,
    )
    private_checkpoint = Artifact(
        id="private-checkpoint",
        workspace_id=owner_workspace_id,
        project_id=private_project.id,
        session_id=ids["private"],
        kind="srt",
        role="correction",
        relative_path="artifacts/protected-session-private.srt",
        content_hash=private_hash,
        state="current",
        metadata_json={},
        created_at=NOW,
        updated_at=NOW,
    )
    session.add_all([shared_checkpoint, private_checkpoint])
    session.flush()
    session.add_all(
        [
            TranslationProject(
                id="shared-translations",
                project_root_id=shared_project.id,
                name="Shared translation navigation",
                source_session_id=ids["shared_source"],
                checkpoint_artifact_id=shared_checkpoint.id,
                source_content_hash=shared_hash,
                source_language="en",
                revision=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            TranslationProject(
                id="private-translations",
                project_root_id=private_project.id,
                name="Private navigation sentinel",
                source_session_id=ids["private"],
                checkpoint_artifact_id=private_checkpoint.id,
                source_content_hash=private_hash,
                source_language="en",
                revision=1,
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
    )
    session.flush()
    session.add(
        TranslationProjectBranch(
            id="shared-translation-branch",
            project_id="shared-translations",
            session_id=ids["shared_branch"],
            target_language="de",
            source_checkpoint_artifact_id=shared_checkpoint.id,
            source_content_hash=shared_hash,
            created_at=NOW,
        )
    )
    session.add(
        ProjectMembership(
            project_id=shared_project.id,
            user_id=reader_id,
            role="viewer",
        )
    )
    return SeededSessions(ids, shared_project.id, private_project.id)


@pytest.fixture
def protected_case(tmp_path: Path) -> Iterator[ProtectedSessionCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        with database.immediate_session() as session:
            owner_workspace = initial_personal_workspace(session)
            reader_id, reader_workspace_id = _insert_user(
                session, display_name="Protected session reader"
            )
            seeded = _seed_sessions(
                session,
                reader_id=reader_id,
                reader_workspace_id=reader_workspace_id,
                owner_workspace_id=owner_workspace.id,
            )
            context, token_id = _insert_actor(
                session,
                reader_id,
                resource_grants=CredentialResources(
                    workspace_ids=frozenset({reader_workspace_id}),
                    project_ids=frozenset({seeded.shared_project_id}),
                ),
            )
        yield ProtectedSessionCase(database, context, reader_id, token_id, seeded)
    finally:
        database.dispose()


@pytest.fixture
def http_case(tmp_path: Path) -> Iterator[HttpSessionCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    application_services = app.extensions["pandrator"]["services"]
    try:
        client = app.test_client()
        launch_token = bootstrap.issue()
        authenticated = client.post("/api/v1/auth/bootstrap", json={"token": launch_token})
        assert authenticated.status_code == 200, authenticated.get_json()
        services = app.extensions["pandrator"]
        database = services["database"]
        owner_session = services["sessions"].create("Initial owner session")
        with database.immediate_session() as session:
            owner_workspace = initial_personal_workspace(session)
            reader_id, reader_workspace_id = _insert_user(
                session, display_name="HTTP protected session reader"
            )
            seeded = _seed_sessions(
                session,
                reader_id=reader_id,
                reader_workspace_id=reader_workspace_id,
                owner_workspace_id=owner_workspace.id,
            )
        yield HttpSessionCase(
            app=app,
            client=client,
            bootstrap=bootstrap,
            auth=services["auth"],
            database=database,
            reader_id=reader_id,
            owner_session_id=owner_session.id,
            sessions=seeded,
        )
    finally:
        application_services.close()


def _ids(records: list[SessionRecord]) -> list[str]:
    return [record.id for record in records]


def test_list_filters_visible_rows_before_stable_search_pagination(
    protected_case: ProtectedSessionCase,
) -> None:
    with protected_case.database.snapshot_session() as session:
        first_page = list_session_records(
            session, protected_case.context, query="  NEEDLE  ", limit=2
        )
        assert _ids(first_page) == [
            protected_case.sessions.ids["shared_source"],
            protected_case.sessions.ids["tie_a"],
        ]
        offset_page = list_session_records(
            session,
            protected_case.context,
            query="needle",
            limit=2,
            offset=1,
        )
        assert _ids(offset_page) == [
            protected_case.sessions.ids["tie_a"],
            protected_case.sessions.ids["tie_z"],
        ]
        assert (
            list_session_records(session, protected_case.context, query="only-id-marker")[0].id
            == protected_case.sessions.ids["id_search"]
        )
        assert (
            list_session_records(session, protected_case.context, query="source-marker")[0].id
            == protected_case.sessions.ids["source_search"]
        )
        assert (
            list_session_records(session, protected_case.context, query="target-marker")[0].id
            == protected_case.sessions.ids["target_search"]
        )
        blank_query = list_session_records(session, protected_case.context, query="   ")
        assert protected_case.sessions.ids["shared_branch"] in _ids(blank_query)
        assert protected_case.sessions.ids["own_trashed"] not in _ids(blank_query)
        assert protected_case.sessions.ids["private"] not in _ids(blank_query)
        assert protected_case.sessions.ids["private_trashed"] not in _ids(blank_query)

        including_trash = list_session_records(
            session,
            protected_case.context,
            query="needle",
            include_trashed=True,
        )
        assert protected_case.sessions.ids["own_trashed"] in _ids(including_trash)
        assert protected_case.sessions.ids["private_trashed"] not in _ids(including_trash)
        assert protected_case.sessions.ids["private"] not in _ids(including_trash)
        assert list_session_records(session, protected_case.context, limit=0) == []
        assert len(list_session_records(session, protected_case.context, limit=10_000)) == len(
            blank_query
        )


@pytest.mark.parametrize(
    ("limit", "offset", "error"),
    [
        (True, 0, TypeError),
        (1.0, 0, TypeError),
        (-1, 0, ValueError),
        (None, True, TypeError),
        (None, 1.0, TypeError),
        (None, -1, ValueError),
    ],
)
def test_pagination_rejects_non_exact_or_negative_integers(
    protected_case: ProtectedSessionCase,
    limit: int | float | bool | None,
    offset: int | float | bool,
    error: type[Exception],
) -> None:
    with protected_case.database.snapshot_session() as session:
        with pytest.raises(error):
            list_session_records(
                session,
                protected_case.context,
                limit=cast(int | None, limit),
                offset=cast(int, offset),
            )


def test_direct_reads_return_attached_granted_rows_and_hide_absence(
    protected_case: ProtectedSessionCase,
) -> None:
    with protected_case.database.snapshot_session() as session:
        for key in ("shared_source", "shared_branch", "own"):
            record = get_session_record(
                session, protected_case.context, protected_case.sessions.ids[key]
            )
            assert isinstance(record, SessionRecord)
            assert object_session(record) is session

        outcomes = []
        for resource_id in (
            protected_case.sessions.ids["private"],
            "missing-session-id",
        ):
            with pytest.raises(SessionNotFound) as error:
                get_session_record(session, protected_case.context, resource_id)
            outcomes.append(error.value.args)
        assert outcomes == [("Session not found.",), ("Session not found.",)]


def test_revoked_project_membership_denies_old_context_in_next_unit(
    protected_case: ProtectedSessionCase,
) -> None:
    with protected_case.database.immediate_session() as session:
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == protected_case.sessions.shared_project_id,
                ProjectMembership.user_id == protected_case.reader_id,
            )
        )

    with protected_case.database.snapshot_session() as session:
        visible = list_session_records(session, protected_case.context)
        assert protected_case.sessions.ids["shared_source"] not in _ids(visible)
        assert protected_case.sessions.ids["shared_branch"] not in _ids(visible)
        with pytest.raises(SessionNotFound):
            get_session_record(
                session,
                protected_case.context,
                protected_case.sessions.ids["shared_source"],
            )


@pytest.mark.parametrize("revocation", ["token", "account_epoch"])
def test_revoked_token_or_account_epoch_denies_old_context(
    protected_case: ProtectedSessionCase,
    revocation: str,
) -> None:
    with protected_case.database.immediate_session() as session:
        if revocation == "token":
            session.execute(
                update(ApiToken)
                .where(ApiToken.id == protected_case.token_id)
                .values(revoked_at=utcnow())
            )
        else:
            session.execute(
                update(UserCredential)
                .where(UserCredential.user_id == protected_case.reader_id)
                .values(auth_revision=2)
            )

    with protected_case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            list_session_records(session, protected_case.context)
        with pytest.raises(AccessDenied):
            get_session_record(
                session,
                protected_case.context,
                protected_case.sessions.ids["own"],
            )


def test_administrator_without_content_membership_cannot_read_private_project(
    protected_case: ProtectedSessionCase,
) -> None:
    admin_scopes = frozenset({"app.read", "app.admin"})
    with protected_case.database.immediate_session() as session:
        admin_id, _admin_workspace_id = _insert_user(
            session,
            display_name="Contentless installation administrator",
            is_instance_admin=True,
        )
        admin_context, _token_id = _insert_actor(
            session,
            admin_id,
            admin_scopes,
            resource_grants=CredentialResources(
                project_ids=frozenset({protected_case.sessions.private_project_id})
            ),
        )

    with protected_case.database.snapshot_session() as session:
        records = list_session_records(session, admin_context)
        assert protected_case.sessions.ids["private"] not in _ids(records)
        with pytest.raises(SessionNotFound):
            get_session_record(
                session,
                admin_context,
                protected_case.sessions.ids["private"],
            )


def test_missing_bare_and_host_contexts_are_rejected(
    protected_case: ProtectedSessionCase,
) -> None:
    invalid_contexts = (
        None,
        protected_case.context.actor,
        HostActor("pandrator-cli"),
        object(),
    )
    with protected_case.database.snapshot_session() as session:
        for invalid in invalid_contexts:
            with pytest.raises(TypeError, match="access context"):
                list_session_records(session, cast(AccessContext, invalid))
            with pytest.raises(TypeError, match="access context"):
                get_session_record(
                    session,
                    cast(AccessContext, invalid),
                    protected_case.sessions.ids["own"],
                )


def test_helpers_preserve_pending_state_and_attached_rows_on_caller_rollback(
    protected_case: ProtectedSessionCase,
) -> None:
    own_id = protected_case.sessions.ids["own"]
    original_name = ""
    with pytest.raises(RuntimeError, match="outer transaction failure"):
        with protected_case.database.session() as session:
            own = session.get(SessionRecord, own_id)
            assert own is not None
            original_name = own.name
            own.name = "Pending caller edit"
            pending_user = User(
                display_name="Pending caller user",
                is_active=True,
                is_instance_admin=False,
            )
            session.add(pending_user)

            records = list_session_records(session, protected_case.context)
            fetched = get_session_record(session, protected_case.context, own_id)

            assert pending_user.id is None
            assert pending_user in session.new
            assert own.name == "Pending caller edit"
            assert fetched is own
            assert object_session(fetched) is session
            assert all(object_session(record) is session for record in records)
            raise RuntimeError("outer transaction failure")

    with protected_case.database.snapshot_session() as session:
        own = session.get(SessionRecord, own_id)
        assert own is not None and own.name == original_name
        assert (
            session.scalar(select(User.id).where(User.display_name == "Pending caller user"))
            is None
        )


def test_session_http_routes_use_actual_reader_policy_and_keep_safe_projections(
    http_case: HttpSessionCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_response = http_case.client.get("/api/v1/sessions")
    assert owner_response.status_code == 200
    assert http_case.owner_session_id in {item["id"] for item in owner_response.get_json()["items"]}

    assert http_case.auth.solo_access_allowed(http_case.reader_id, 1) is False
    reader_client = http_case.app.test_client()
    rejected_grant = http_case.bootstrap.issue(
        user_id=http_case.reader_id,
        auth_revision=1,
        subject=f"test-user:{http_case.reader_id}",
        scopes=READ_SCOPES,
    )
    rejected = reader_client.post("/api/v1/auth/bootstrap", json={"token": rejected_grant})
    assert rejected.status_code == 401

    real_solo_access_allowed = http_case.auth.solo_access_allowed

    def allow_only_test_reader(user_id: str | None, auth_revision: int | None) -> bool:
        if user_id == http_case.reader_id and auth_revision == 1:
            return True
        return real_solo_access_allowed(user_id, auth_revision)

    monkeypatch.setattr(http_case.auth, "solo_access_allowed", allow_only_test_reader)
    reader_grant = http_case.bootstrap.issue(
        user_id=http_case.reader_id,
        auth_revision=1,
        subject=f"test-user:{http_case.reader_id}",
        scopes=READ_SCOPES,
    )
    authenticated = reader_client.post("/api/v1/auth/bootstrap", json={"token": reader_grant})
    assert authenticated.status_code == 200, authenticated.get_json()
    with reader_client.session_transaction() as browser_state:
        browser_token = browser_state["auth_session"]
    browser_actor = http_case.auth.browser_sessions.resolve(browser_token)
    assert browser_actor is not None
    assert browser_actor.user_id == http_case.reader_id

    listing = reader_client.get("/api/v1/sessions")
    assert listing.status_code == 200
    list_items = listing.get_json()["items"]
    listed_ids = {item["id"] for item in list_items}
    assert http_case.sessions.ids["own"] in listed_ids
    assert http_case.sessions.ids["shared_source"] in listed_ids
    assert http_case.sessions.ids["shared_branch"] in listed_ids
    assert http_case.sessions.ids["private"] not in listed_ids
    assert http_case.sessions.ids["private_trashed"] not in listed_ids
    serialized = json.dumps(listing.get_json(), sort_keys=True)
    assert "Private navigation sentinel" not in serialized

    navigation = {item["id"]: item.get("translation_project") for item in list_items}
    assert navigation[http_case.sessions.ids["shared_source"]]["role"] == "source"
    assert navigation[http_case.sessions.ids["shared_branch"]]["role"] == "branch"
    assert navigation[http_case.sessions.ids["shared_branch"]]["target_language"] == "de"

    matching = reader_client.get("/api/v1/sessions?q=needle")
    assert matching.status_code == 200
    matching_ids = {item["id"] for item in matching.get_json()["items"]}
    assert matching_ids == {
        http_case.sessions.ids["shared_source"],
        http_case.sessions.ids["tie_a"],
        http_case.sessions.ids["tie_z"],
    }

    source = reader_client.get(f"/api/v1/sessions/{http_case.sessions.ids['shared_source']}")
    branch = reader_client.get(f"/api/v1/sessions/{http_case.sessions.ids['shared_branch']}")
    assert source.status_code == branch.status_code == 200
    source_body = source.get_json()
    branch_body = branch.get_json()
    assert source_body["translation_project"]["role"] == "source"
    assert branch_body["translation_project"]["role"] == "branch"
    assert source.headers["ETag"] == f'"{source_body["revision"]}"'

    hidden = reader_client.get(f"/api/v1/sessions/{http_case.sessions.ids['private']}")
    missing = reader_client.get("/api/v1/sessions/missing-session-id")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.get_json()["error"]["code"] == missing.get_json()["error"]["code"]
    assert hidden.get_json()["error"]["message"] == missing.get_json()["error"]["message"]

    limited_client = http_case.app.test_client()
    limited_grant = http_case.bootstrap.issue(
        user_id=http_case.reader_id,
        auth_revision=1,
        subject=f"test-user:{http_case.reader_id}",
        scopes={"app.write"},
    )
    limited_auth = limited_client.post("/api/v1/auth/bootstrap", json={"token": limited_grant})
    assert limited_auth.status_code == 200
    scope_denied = limited_client.get("/api/v1/sessions")
    assert scope_denied.status_code == 403
    assert scope_denied.get_json()["error"]["code"] == "scope_denied"

    with http_case.database.immediate_session() as session:
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == http_case.sessions.shared_project_id,
                ProjectMembership.user_id == http_case.reader_id,
            )
        )
    after_revocation = reader_client.get("/api/v1/sessions")
    assert after_revocation.status_code == 200
    after_revocation_ids = {item["id"] for item in after_revocation.get_json()["items"]}
    assert http_case.sessions.ids["shared_source"] not in after_revocation_ids
    assert http_case.sessions.ids["shared_branch"] not in after_revocation_ids
    revoked_read = reader_client.get(f"/api/v1/sessions/{http_case.sessions.ids['shared_source']}")
    assert revoked_read.status_code == 404

    with http_case.database.immediate_session() as session:
        session.execute(
            update(UserCredential)
            .where(UserCredential.user_id == http_case.reader_id)
            .values(auth_revision=2)
        )
    revoked_credential = reader_client.get("/api/v1/sessions")
    assert revoked_credential.status_code == 401
