"""Actual-user access boundaries for private, sessionless transcriptions."""

from __future__ import annotations

import hashlib
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests.sessions
from flask.testing import FlaskClient
from sqlalchemy import delete, func, select, update

from pandrator.web.access.contracts import AccessContext
from pandrator.web.access.private_transcription_policy import (
    PrivateTranscriptionNotFound,
    get_private_transcription,
)
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import (
    UserActor,
    actor_from_principal,
    actor_from_storage,
)
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.private_work_models import QuickTranscriptionOwner
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenWorkspaceGrant,
    AutomationClientWorkspaceGrant,
)
from pandrator.web.api import create_app
from pandrator.web.auth import AuthService, BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    AutomationClient,
    Job,
    JobEvent,
    Project,
    QuickTranscription,
    WorkspaceSetting,
    new_id,
    utcnow,
)
from tests.web_test_support import prepare_web_test_data_root

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PRIVATE_SCOPES = frozenset({"app.read", "app.run", "app.cancel"})


@pytest.fixture(scope="module", autouse=True)
def block_network_attempts() -> Iterator[None]:
    attempts: list[str] = []
    patcher = pytest.MonkeyPatch()

    def blocked_request(*_args: object, **_kwargs: object) -> None:
        attempts.append("requests.sessions.Session.request")
        raise AssertionError("Private-transcription access tests must not use the network.")

    def blocked_connect(*_args: object, **_kwargs: object) -> None:
        attempts.append("socket.socket.connect")
        raise AssertionError("Private-transcription access tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked_request)
    patcher.setattr(socket.socket, "connect", blocked_connect)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts, f"Unexpected network attempts: {attempts!r}"


def _browser_actor(case_auth: AuthService, client: FlaskClient) -> UserActor:
    with client.session_transaction() as browser_state:
        raw_session = browser_state["auth_session"]
    principal = case_auth.browser_sessions.resolve(raw_session)
    assert principal is not None
    return UserActor(
        user_id=principal.user_id,
        auth_revision=principal.auth_revision,
        subject=principal.subject,
        principal_kind=principal.kind,
        scopes=principal.scopes,
        browser_session_id=principal.session_id,
        client_id=principal.client_id,
    )


@pytest.fixture
def private_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    data_root = tmp_path / "private-transcription-data"
    paths = prepare_web_test_data_root(data_root)
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=data_root,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]
    database: Database = services["database"]
    auth: AuthService = services["auth"]
    try:
        initial = auth.initial_account()
        with database.immediate_session() as session:
            owner = session.get(User, initial.user_id)
            assert owner is not None
            owner.is_instance_admin = True
            owner_workspace = initial_personal_workspace(session)

            second_id = new_id()
            session.add(
                User(
                    id=second_id,
                    display_name="Private transcription second user",
                    is_active=True,
                    is_instance_admin=True,
                )
            )
            session.flush()
            session.add(
                UserCredential(
                    user_id=second_id,
                    login_name=f"private-transcription-{second_id}",
                    password_hash=None,
                    auth_revision=1,
                )
            )
            second_workspace = Workspace(
                id=new_id(),
                kind="personal",
                name="Private transcription second workspace",
                personal_owner_user_id=second_id,
                state="active",
                revision=1,
            )
            session.add(second_workspace)
            session.flush()
            session.add(
                WorkspaceMembership(
                    workspace_id=second_workspace.id,
                    user_id=second_id,
                    role="owner",
                )
            )
            session.flush()
            owner_id = owner.id
            owner_workspace_id = owner_workspace.id
            second_workspace_id = second_workspace.id

        real_solo_access_allowed = auth.solo_access_allowed

        monkeypatch.setattr(
            auth,
            "solo_access_allowed",
            lambda user_id, auth_revision: (
                (user_id == second_id and auth_revision == 1)
                or real_solo_access_allowed(user_id, auth_revision)
            ),
        )

        owner_client = app.test_client()
        owner_bootstrap = owner_client.post(
            "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
        )
        assert owner_bootstrap.status_code == 200, owner_bootstrap.get_json()
        owner_headers = {"X-CSRF-Token": owner_bootstrap.get_json()["csrf_token"]}
        owner_actor = _browser_actor(auth, owner_client)
        assert owner_actor.user_id == owner_id

        second_client = app.test_client()
        second_bootstrap = bootstrap.issue(
            subject=owner_actor.subject,
            kind="owner_session",
            user_id=second_id,
            auth_revision=1,
            scopes=ALL_SCOPES,
        )
        authenticated_second = second_client.post(
            "/api/v1/auth/bootstrap", json={"token": second_bootstrap}
        )
        assert authenticated_second.status_code == 200, authenticated_second.get_json()
        second_headers = {"X-CSRF-Token": authenticated_second.get_json()["csrf_token"]}
        second_actor = _browser_actor(auth, second_client)
        assert second_actor.user_id == second_id
        assert second_actor.subject == owner_actor.subject

        yield SimpleNamespace(
            app=app,
            bootstrap=bootstrap,
            auth=auth,
            database=database,
            services=services,
            paths=paths,
            owner_id=owner_id,
            owner_workspace_id=owner_workspace_id,
            owner_client=owner_client,
            owner_headers=owner_headers,
            owner_actor=owner_actor,
            second_id=second_id,
            second_workspace_id=second_workspace_id,
            second_client=second_client,
            second_headers=second_headers,
            second_actor=second_actor,
        )
    finally:
        services["quick_transcriptions"].stop_maintenance()
        database.dispose()


def _api_credential(
    case: Any,
    user_id: str,
    *,
    subject: str | None = None,
    scopes: frozenset[str] = PRIVATE_SCOPES,
    resource_grants: CredentialResources | None = None,
    principal_kind: str = "api_token",
    client_id: str | None = None,
) -> Any:
    token, raw_token = case.auth.create_api_token(
        "Synthetic private-transcription access credential",
        scopes=scopes,
        subject=subject,
        principal_kind=principal_kind,
        client_id=client_id,
        user_id=user_id,
        auth_revision=1,
        resource_grants=resource_grants,
    )
    principal = case.auth.resolve_api_token(
        raw_token,
        network_zone="loopback",
        target_instance_id=None,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None
    return SimpleNamespace(
        client=case.app.test_client(),
        token=token,
        raw_token=raw_token,
        actor=actor,
        context=AccessContext(actor),
        headers={"Authorization": f"Bearer {raw_token}"},
    )


def _insert_automation_client(
    case: Any,
    user_id: str,
    workspace_id: str,
) -> str:
    client_id = new_id()
    identity = case.services["services"].identity.snapshot(observed_origin="http://localhost/")
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="Private transcription synthetic client",
                subject=f"private-transcription-client:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=sorted(ALL_SCOPES),
                target_instance_id=identity.instance_id,
                canonical_origin=identity.canonical_origin,
                created_by="private-transcription-access-test",
                user_id=user_id,
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(
            AutomationClientWorkspaceGrant(
                client_id=client_id,
                workspace_id=workspace_id,
            )
        )
    return client_id


def _create_transcription(
    client: FlaskClient,
    headers: dict[str, str],
    *,
    key: str,
    content: bytes = b"private transcription source",
    **overrides: Any,
):
    payload: dict[str, Any] = {
        "filename": "private-clip.wav",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "format": "txt",
    }
    payload.update(overrides)
    return client.post(
        "/api/v1/transcriptions",
        json=payload,
        headers={**headers, "Idempotency-Key": key},
    )


def _upload_chunk(
    client: FlaskClient,
    headers: dict[str, str],
    transcription_id: str,
    content: bytes,
    *,
    index: int = 0,
):
    return client.put(
        f"/api/v1/transcriptions/{transcription_id}/chunks/{index}",
        data=content,
        headers=headers,
    )


def _create_uploaded(
    client: FlaskClient,
    headers: dict[str, str],
    *,
    key: str,
    content: bytes = b"private transcription source",
):
    created = _create_transcription(client, headers, key=key, content=content)
    assert created.status_code == 201, created.get_json()
    transcription_id = created.get_json()["id"]
    uploaded = _upload_chunk(client, headers, transcription_id, content)
    assert uploaded.status_code == 200, uploaded.get_json()
    return transcription_id


def _start_transcription(
    client: FlaskClient,
    headers: dict[str, str],
    transcription_id: str,
):
    return client.post(
        f"/api/v1/transcriptions/{transcription_id}/start",
        json={},
        headers=headers,
    )


PRIVATE_ROUTE_MATRIX = (
    ("quick", "GET", "/api/v1/transcriptions/{id}", "none"),
    ("quick", "GET", "/api/v1/transcriptions/{id}/result?format=txt", "none"),
    ("quick", "PUT", "/api/v1/transcriptions/{id}/chunks/0", "chunk"),
    ("quick", "POST", "/api/v1/transcriptions/{id}/start", "json"),
    ("quick", "POST", "/api/v1/transcriptions/{id}/cancel", "none"),
    ("quick", "DELETE", "/api/v1/transcriptions/{id}", "none"),
    ("job", "GET", "/api/v1/jobs/{id}", "none"),
    ("job", "GET", "/api/v1/jobs/{id}/logs", "none"),
    ("job", "POST", "/api/v1/jobs/{id}/cancel", "none"),
    ("work", "GET", "/api/v1/work/{id}", "none"),
    ("work", "GET", "/api/v1/work/{id}/events", "none"),
    ("work", "POST", "/api/v1/work/{id}/cancel", "key"),
)


def _private_route_request(
    client: FlaskClient,
    headers: dict[str, str],
    *,
    method: str,
    path: str,
    body: str,
    identifier: str,
    content: bytes,
    key: str,
):
    request_headers = dict(headers)
    kwargs: dict[str, Any] = {}
    if body == "chunk":
        kwargs["data"] = content
    elif body == "json":
        kwargs["json"] = {}
    elif body == "key":
        request_headers["Idempotency-Key"] = key
    return client.open(
        path.format(id=identifier),
        method=method,
        headers=request_headers,
        **kwargs,
    )


def _error_without_request_id(response: Any) -> tuple[int, dict[str, Any]]:
    payload = response.get_json()
    assert isinstance(payload, dict)
    error = payload.get("error")
    assert isinstance(error, dict)
    request_id = error.get("request_id")
    assert isinstance(request_id, str) and request_id
    return response.status_code, {key: value for key, value in error.items() if key != "request_id"}


def _quick_directory(case: Any, transcription_id: str) -> Path:
    return case.paths.root / "quick-transcriptions" / transcription_id


def _files_snapshot(directory: Path) -> tuple[tuple[str, int, str], ...]:
    if not directory.exists():
        return ()
    return tuple(
        (
            path.relative_to(directory).as_posix(),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


def _mutation_snapshot(
    case: Any,
    transcription_id: str,
    job_id: str | None,
) -> tuple[Any, ...]:
    with case.database.snapshot_session() as session:
        transcription = session.get(QuickTranscription, transcription_id)
        job = session.get(Job, job_id) if job_id is not None else None
        return (
            None
            if transcription is None
            else (
                transcription.state,
                transcription.job_id,
                transcription.uploaded_bytes,
                transcription.next_chunk_index,
            ),
            None if job is None else (job.status, job.updated_at, job.result_json),
            session.scalar(select(func.count()).select_from(QuickTranscription)),
            session.scalar(select(func.count()).select_from(Job)),
            session.scalar(select(func.count()).select_from(ApiIdempotency)),
            _files_snapshot(_quick_directory(case, transcription_id)),
        )


def _seed_private_job(
    case: Any,
    *,
    user_id: str,
    workspace_id: str,
    subject: str,
    created_at: datetime = NOW,
) -> tuple[str, str]:
    job_id = new_id()
    transcription_id = new_id()
    expires_at = created_at + timedelta(days=2)
    checksum = hashlib.sha256(b"seeded private transcription").hexdigest()
    with case.database.immediate_session() as session:
        session.add(
            Job(
                id=job_id,
                kind="transcription.quick",
                workspace_id=workspace_id,
                status="queued",
                payload_json={"transcription_id": transcription_id},
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.flush()
        session.add(
            QuickTranscription(
                id=transcription_id,
                owner_subject=subject,
                job_id=job_id,
                source_suffix=".wav",
                size_bytes=1,
                sha256=checksum,
                format="txt",
                settings_json={},
                state="queued",
                expires_at=expires_at,
            )
        )
        session.flush()
        session.add(
            QuickTranscriptionOwner(
                transcription_id=transcription_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        )
    return job_id, transcription_id


def _add_job_event(
    case: Any,
    job_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> int:
    with case.database.immediate_session() as session:
        event = JobEvent(
            job_id=job_id,
            event_type=event_type,
            payload_json=payload,
            created_at=utcnow(),
        )
        session.add(event)
        session.flush()
        return event.id


def _set_stt_defaults(
    case: Any,
    workspace_id: str,
    values: dict[str, Any],
) -> None:
    with case.database.immediate_session() as session:
        session.add(
            WorkspaceSetting(
                workspace_id=workspace_id,
                key="defaults.stt",
                value_json=dict(values),
                revision=1,
            )
        )


def _receipt_row(
    case: Any,
    *,
    subject: str,
    key: str,
) -> tuple[Any, ...] | None:
    with case.database.snapshot_session() as session:
        row = session.execute(
            select(ApiIdempotency).where(
                ApiIdempotency.principal_subject == subject,
                ApiIdempotency.idempotency_key == key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return (
            row.id,
            row.operation_id,
            row.request_digest,
            row.state,
            row.status_code,
            dict(row.response_json) if row.response_json is not None else None,
            row.resource_kind,
            row.resource_id,
        )


def test_same_user_credentials_share_private_transcription_and_bind_actual_job_root(
    private_case: Any,
) -> None:
    case = private_case
    transcription_id = _create_uploaded(
        case.owner_client,
        case.owner_headers,
        key="private-owner-create",
    )
    api = _api_credential(
        case,
        case.owner_id,
        scopes=PRIVATE_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )
    assert api.actor.subject != case.owner_actor.subject

    with case.database.snapshot_session() as session:
        owner_row = session.get(QuickTranscriptionOwner, transcription_id)
        assert owner_row is not None
        assert (owner_row.user_id, owner_row.workspace_id) == (
            case.owner_id,
            case.owner_workspace_id,
        )

    api_read = api.client.get(f"/api/v1/transcriptions/{transcription_id}", headers=api.headers)
    assert api_read.status_code == 202, api_read.get_json()

    client_id = _insert_automation_client(case, case.owner_id, case.owner_workspace_id)
    automation = _api_credential(
        case,
        case.owner_id,
        scopes=PRIVATE_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
        principal_kind="automation_client",
        client_id=client_id,
    )
    assert automation.actor.principal_kind == "automation_client"
    assert automation.actor.client_id == client_id
    assert automation.actor.subject != api.actor.subject
    client_read = automation.client.get(
        f"/api/v1/transcriptions/{transcription_id}",
        headers=automation.headers,
    )
    assert client_read.status_code == 202, client_read.get_json()

    started = _start_transcription(api.client, api.headers, transcription_id)
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["job_id"]
    with case.database.snapshot_session() as session:
        job = session.get(Job, job_id)
        actor_row = session.get(JobActorRecord, job_id)
        assert job is not None
        assert actor_row is not None
        assert actor_row.user_id == case.owner_id
        admitted_actor = actor_from_storage(
            actor_row.user_id,
            actor_row.auth_revision,
            actor_row.context_json,
        )
        assert admitted_actor is not None
        assert admitted_actor.subject == api.actor.subject
        assert job.workspace_id == case.owner_workspace_id
        assert job.project_id is None
        assert job.session_id is None
        assert job.workflow_run_id is None

    browser_state = case.owner_client.get(
        f"/api/v1/transcriptions/{transcription_id}",
        headers=case.owner_headers,
    )
    assert browser_state.status_code == 202, browser_state.get_json()
    assert browser_state.get_json()["job_id"] == job_id


def test_second_actual_user_with_the_same_subject_cannot_reach_private_work(
    private_case: Any,
) -> None:
    case = private_case
    content = b"owner private source bytes"
    owner_transcription_id = _create_uploaded(
        case.owner_client,
        case.owner_headers,
        key="private-same-subject-owner",
        content=content,
    )
    owner_api = _api_credential(
        case,
        case.owner_id,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )
    started = _start_transcription(owner_api.client, owner_api.headers, owner_transcription_id)
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["job_id"]
    missing_id = new_id()

    second_api = _api_credential(
        case,
        case.second_id,
        subject=case.owner_actor.subject,
        scopes=ALL_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.second_workspace_id})),
    )
    assert second_api.actor.subject == case.owner_actor.subject
    assert second_api.actor.user_id == case.second_id
    assert second_api.actor.principal_kind == "api_token"
    assert "app.admin" in second_api.actor.scopes
    with case.database.snapshot_session() as session:
        second_user = session.get(User, case.second_id)
        assert second_user is not None
        assert second_user.is_instance_admin is True

    own_created = _create_transcription(
        case.second_client,
        case.second_headers,
        key="private-same-subject-second-own",
        content=b"second user's own private source",
    )
    assert own_created.status_code == 201, own_created.get_json()
    own_id = own_created.get_json()["id"]
    assert (
        case.second_client.get(
            f"/api/v1/transcriptions/{own_id}", headers=case.second_headers
        ).status_code
        == 202
    )
    assert (
        second_api.client.get(
            f"/api/v1/transcriptions/{own_id}",
            headers=second_api.headers,
        ).status_code
        == 202
    )

    result_path = _quick_directory(case, owner_transcription_id) / "result.txt"
    result_path.write_text("transcript-private-sentinel", encoding="utf-8")
    assert result_path.is_file()

    for client_index, (client, headers) in enumerate(
        ((case.second_client, case.second_headers), (second_api.client, second_api.headers))
    ):
        for route_index, (family, method, path, body) in enumerate(PRIVATE_ROUTE_MATRIX):
            target_id = owner_transcription_id if family == "quick" else job_id
            key = f"private-hidden-{client_index}-{route_index}"
            before = _mutation_snapshot(case, owner_transcription_id, job_id)
            hidden = _private_route_request(
                client,
                headers,
                method=method,
                path=path,
                body=body,
                identifier=target_id,
                content=content,
                key=key,
            )
            after_hidden = _mutation_snapshot(case, owner_transcription_id, job_id)
            assert hidden.status_code == 404, hidden.get_json()
            assert after_hidden == before
            if family == "quick":
                assert b"transcript-private-sentinel" not in hidden.data

            missing = _private_route_request(
                client,
                headers,
                method=method,
                path=path,
                body=body,
                identifier=missing_id,
                content=content,
                key=key,
            )
            after_missing = _mutation_snapshot(case, owner_transcription_id, job_id)
            assert _error_without_request_id(hidden) == _error_without_request_id(missing)
            assert _error_without_request_id(hidden)[0] == 404
            assert after_missing == before


def test_empty_and_project_only_api_grants_do_not_reach_private_transcriptions(
    private_case: Any,
) -> None:
    case = private_case
    private_id = _create_uploaded(
        case.owner_client,
        case.owner_headers,
        key="private-resource-ceiling",
    )
    root = case.services["sessions"].create(
        "Private transcription project-only grant",
        workspace_id=case.owner_workspace_id,
    )
    with case.database.snapshot_session() as session:
        project = session.get(Project, root.project_id)
        assert project is not None
        assert project.workspace_id == case.owner_workspace_id
        project_id = project.id

    credentials = (
        _api_credential(
            case,
            case.owner_id,
            resource_grants=CredentialResources(),
        ),
        _api_credential(
            case,
            case.owner_id,
            resource_grants=CredentialResources(project_ids=frozenset({project_id})),
        ),
    )
    project_only = credentials[1]
    project_access = project_only.client.get(
        f"/api/v1/sessions/{root.id}",
        headers=project_only.headers,
    )
    assert project_access.status_code == 200, project_access.get_json()

    for index, credential in enumerate(credentials):
        before = _mutation_snapshot(case, private_id, None)
        hidden = credential.client.get(
            f"/api/v1/transcriptions/{private_id}",
            headers=credential.headers,
        )
        assert hidden.status_code == 404, hidden.get_json()
        assert _mutation_snapshot(case, private_id, None) == before

        denied_create = _create_transcription(
            credential.client,
            credential.headers,
            key=f"private-resource-create-denied-{index}",
            content=f"denied source {index}".encode(),
        )
        assert denied_create.status_code == 403, denied_create.get_json()
        assert _mutation_snapshot(case, private_id, None) == before


def test_client_workspace_grant_is_live_and_narrows_an_unchanged_token_snapshot(
    private_case: Any,
) -> None:
    case = private_case
    private_id = _create_transcription(
        case.owner_client,
        case.owner_headers,
        key="private-client-live-grant",
        content=b"client access proof",
    ).get_json()["id"]
    client_id = _insert_automation_client(
        case,
        case.owner_id,
        case.owner_workspace_id,
    )
    credential = _api_credential(
        case,
        case.owner_id,
        scopes=ALL_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
        principal_kind="automation_client",
        client_id=client_id,
    )
    assert (
        credential.client.get(
            f"/api/v1/transcriptions/{private_id}",
            headers=credential.headers,
        ).status_code
        == 202
    )
    with case.database.snapshot_session() as session:
        token_grant_before = session.get(
            ApiTokenWorkspaceGrant,
            (credential.token.id, case.owner_workspace_id),
        )
        assert token_grant_before is not None

    with case.database.immediate_session() as session:
        session.execute(
            delete(AutomationClientWorkspaceGrant).where(
                AutomationClientWorkspaceGrant.client_id == client_id,
                AutomationClientWorkspaceGrant.workspace_id == case.owner_workspace_id,
            )
        )

    before = _mutation_snapshot(case, private_id, None)
    denied = credential.client.get(
        f"/api/v1/transcriptions/{private_id}",
        headers=credential.headers,
    )
    assert denied.status_code == 404, denied.get_json()
    create = _create_transcription(
        credential.client,
        credential.headers,
        key="private-client-create-after-narrowing",
        content=b"narrowed client cannot create",
    )
    assert create.status_code == 403, create.get_json()
    assert _mutation_snapshot(case, private_id, None) == before
    with case.database.snapshot_session() as session:
        assert (
            session.get(
                ApiTokenWorkspaceGrant,
                (credential.token.id, case.owner_workspace_id),
            )
            is not None
        )
        assert (
            session.get(
                AutomationClientWorkspaceGrant,
                (client_id, case.owner_workspace_id),
            )
            is None
        )


def test_second_user_defaults_and_quota_use_actual_workspace_and_stable_owner_rows(
    private_case: Any,
) -> None:
    case = private_case
    _set_stt_defaults(
        case,
        case.owner_workspace_id,
        {
            "stt_engine": "whisper",
            "stt_language": "en",
            "stt_model_quantization": "q8_0",
            "stt_compute_backend": "cpu",
        },
    )
    _set_stt_defaults(
        case,
        case.second_workspace_id,
        {
            "stt_engine": "parakeet",
            "stt_language": "pl",
            "stt_model_quantization": "q8_0",
            "stt_compute_backend": "cpu",
        },
    )

    second_id = _create_uploaded(
        case.second_client,
        case.second_headers,
        key="private-second-user-defaults",
    )
    with case.database.snapshot_session() as session:
        transcription = session.get(QuickTranscription, second_id)
        owner_row = session.get(QuickTranscriptionOwner, second_id)
        assert transcription is not None
        assert owner_row is not None
        assert transcription.settings_json["stt_language"] == "pl"
        assert transcription.settings_json["stt_engine"] == "parakeet"
        assert owner_row.user_id == case.second_id
        assert owner_row.workspace_id == case.second_workspace_id

    started = _start_transcription(
        case.second_client,
        case.second_headers,
        second_id,
    )
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["job_id"]
    with case.database.snapshot_session() as session:
        job = session.get(Job, job_id)
        actor_row = session.get(JobActorRecord, job_id)
        assert job is not None
        assert actor_row is not None
        assert job.workspace_id == case.second_workspace_id
        assert job.project_id is None
        assert job.session_id is None
        assert job.workflow_run_id is None
        assert actor_row.user_id == case.second_id
        admitted_actor = actor_from_storage(
            actor_row.user_id,
            actor_row.auth_revision,
            actor_row.context_json,
        )
        assert admitted_actor is not None
        assert admitted_actor.subject == case.second_actor.subject

    api = _api_credential(
        case,
        case.owner_id,
        scopes=PRIVATE_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )
    created_subjects: set[str] = set()
    for index in range(20):
        client, headers = (
            (case.owner_client, case.owner_headers) if index % 2 == 0 else (api.client, api.headers)
        )
        created = _create_transcription(
            client,
            headers,
            key=f"private-shared-quota-{index}",
            content=f"quota-{index}".encode(),
        )
        assert created.status_code == 201, created.get_json()
        created_subjects.add(case.owner_actor.subject if index % 2 == 0 else api.actor.subject)
    assert case.owner_actor.subject in created_subjects
    assert api.actor.subject in created_subjects
    assert len(created_subjects) == 2
    with case.database.snapshot_session() as session:
        owner_ids = set(
            session.scalars(
                select(QuickTranscriptionOwner.transcription_id).where(
                    QuickTranscriptionOwner.user_id == case.owner_id
                )
            ).all()
        )
        owner_subjects = set(
            session.scalars(
                select(QuickTranscription.owner_subject).where(QuickTranscription.id.in_(owner_ids))
            ).all()
        )
        assert len(owner_ids) == 20
        assert owner_subjects == created_subjects

    before = (
        len(owner_ids),
        _count_rows(case, ApiIdempotency),
        _count_rows(case, Job),
    )
    over_quota = _create_transcription(
        case.owner_client,
        case.owner_headers,
        key="private-shared-quota-21",
        content=b"the twenty-first private transcription",
    )
    assert over_quota.status_code == 429, over_quota.get_json()
    after = (
        _count_private_owners(case, case.owner_id),
        _count_rows(case, ApiIdempotency),
        _count_rows(case, Job),
    )
    assert after == before


def _count_rows(case: Any, model: Any) -> int:
    with case.database.snapshot_session() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _count_private_owners(case: Any, user_id: str) -> int:
    with case.database.snapshot_session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(QuickTranscriptionOwner)
                .where(QuickTranscriptionOwner.user_id == user_id)
            )
            or 0
        )


def _seed_unresolved_private_data(
    case: Any,
    *,
    workspace_id: str,
    subject: str,
) -> tuple[str, str, str]:
    linked_job_id = new_id()
    orphan_id = new_id()
    job_without_link_id = new_id()
    now = utcnow()
    checksum = hashlib.sha256(b"unresolved legacy source").hexdigest()
    with case.database.immediate_session() as session:
        session.add_all(
            [
                Job(
                    id=linked_job_id,
                    # Unassigned legacy links are private even with a malformed kind.
                    kind="dubbing.export",
                    workspace_id=workspace_id,
                    project_id=None,
                    session_id=None,
                    workflow_run_id=None,
                    status="queued",
                    payload_json={"transcription_id": orphan_id},
                    resource_keys_json=["service:stt"],
                    progress=0.0,
                    lease_generation=0,
                    attempts=0,
                    max_attempts=3,
                    created_at=now,
                    updated_at=now,
                ),
                Job(
                    id=job_without_link_id,
                    kind="transcription.quick",
                    workspace_id=workspace_id,
                    project_id=None,
                    session_id=None,
                    workflow_run_id=None,
                    status="queued",
                    payload_json={"transcription_id": "missing-transcription-row"},
                    resource_keys_json=["service:stt"],
                    progress=0.0,
                    lease_generation=0,
                    attempts=0,
                    max_attempts=3,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        session.add(
            QuickTranscription(
                id=orphan_id,
                owner_subject=subject,
                job_id=linked_job_id,
                source_suffix=".wav",
                size_bytes=1,
                sha256=checksum,
                uploaded_bytes=1,
                next_chunk_index=1,
                format="txt",
                settings_json={},
                state="queued",
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
    return orphan_id, linked_job_id, job_without_link_id


def test_unresolved_private_records_and_unlinked_private_jobs_are_hidden_from_admins(
    private_case: Any,
) -> None:
    case = private_case
    orphan_id, linked_job_id, unlinked_job_id = _seed_unresolved_private_data(
        case,
        workspace_id=case.owner_workspace_id,
        subject=case.owner_actor.subject,
    )
    second_api = _api_credential(
        case,
        case.second_id,
        subject=case.owner_actor.subject,
        scopes=ALL_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.second_workspace_id})),
    )
    principals = (
        (case.owner_client, case.owner_headers),
        (second_api.client, second_api.headers),
    )
    for client, headers in principals:
        for identifier in (orphan_id, new_id()):
            hidden = client.get(f"/api/v1/transcriptions/{identifier}", headers=headers)
            assert hidden.status_code == 404, hidden.get_json()
        for job_id in (linked_job_id, unlinked_job_id):
            jobs_hidden = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
            jobs_missing = client.get(f"/api/v1/jobs/{new_id()}", headers=headers)
            work_hidden = client.get(f"/api/v1/work/{job_id}", headers=headers)
            work_missing = client.get(f"/api/v1/work/{new_id()}", headers=headers)
            assert _error_without_request_id(jobs_hidden) == _error_without_request_id(jobs_missing)
            assert _error_without_request_id(work_hidden) == _error_without_request_id(work_missing)
            assert _error_without_request_id(jobs_hidden)[0] == 404
            assert _error_without_request_id(work_hidden)[0] == 404


def test_private_jobs_and_global_events_are_filtered_in_sql_before_limits(
    private_case: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = private_case
    owner_api = _api_credential(
        case,
        case.owner_id,
        scopes=PRIVATE_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )
    owned = (
        _seed_private_job(
            case,
            user_id=case.owner_id,
            workspace_id=case.owner_workspace_id,
            subject=owner_api.actor.subject,
            created_at=NOW - timedelta(days=2),
        ),
        _seed_private_job(
            case,
            user_id=case.owner_id,
            workspace_id=case.owner_workspace_id,
            subject=owner_api.actor.subject,
            created_at=NOW - timedelta(days=1),
        ),
    )
    hidden = tuple(
        _seed_private_job(
            case,
            user_id=case.second_id,
            workspace_id=case.second_workspace_id,
            subject=case.second_actor.subject,
            created_at=NOW + timedelta(seconds=index + 1),
        )
        for index in range(55)
    )
    owned_job_ids = {job_id for job_id, _ in owned}
    hidden_job_ids = {job_id for job_id, _ in hidden}

    hidden_event_payloads = {
        f"hidden-private-event-{job_index}-{event_index}"
        for job_index, (job_id, _transcription_id) in enumerate(hidden)
        for event_index in range(5)
    }
    for job_index, (job_id, _transcription_id) in enumerate(hidden):
        for event_index in range(5):
            _add_job_event(
                case,
                job_id,
                "job.progress",
                {
                    "detail": f"hidden-private-event-{job_index}-{event_index}",
                    "progress": event_index / 5,
                },
            )
    allowed_payloads = {
        "allowed-private-event-first",
        "allowed-private-event-second",
    }
    allowed_event_ids = []
    for job_id, marker in zip(
        sorted(owned_job_ids),
        sorted(allowed_payloads),
        strict=True,
    ):
        allowed_event_ids.append(
            _add_job_event(
                case,
                job_id,
                "job.progress",
                {"detail": marker, "progress": 0.5},
            )
        )
    for job_id in sorted(owned_job_ids | hidden_job_ids):
        _add_job_event(
            case,
            job_id,
            "job.log",
            {"message": f"omitted-job-log-{job_id}"},
        )

    work = case.services["work"]
    with case.database.snapshot_session() as session:
        diagnostic_page = work.diagnostic_list_in_session(
            session,
            owner_api.context,
            limit=2,
            kind="transcription.quick",
        )
        work_page = work.list_in_session(
            session,
            owner_api.context,
            kinds=("transcription.quick",),
            limit=2,
        )
        scoped_events = work.events_after_in_session(
            session,
            owner_api.context,
            0,
            limit=2,
        )
        assert {job.id for job in diagnostic_page} == owned_job_ids
        assert {item.id for item in work_page} == owned_job_ids
        assert len(scoped_events.items) == 2
        assert {event.work_id for event in scoped_events.items} <= owned_job_ids
        assert {event.data["detail"] for event in scoped_events.items} == allowed_payloads
        assert all(event.event_type != "job.log" for event in scoped_events.items)
        allowed_cursor = scoped_events.next_cursor
        assert allowed_cursor == max(allowed_event_ids)

    jobs_response = owner_api.client.get(
        "/api/v1/jobs",
        query_string={"kind": "transcription.quick", "limit": 2},
        headers=owner_api.headers,
    )
    work_response = owner_api.client.get(
        "/api/v1/work",
        query_string={"kind": "transcription.quick", "limit": 2},
        headers=owner_api.headers,
    )
    snapshot_response = owner_api.client.get(
        "/api/v1/events/snapshot",
        query_string={"limit": 40, "view": "compact"},
        headers=owner_api.headers,
    )
    assert jobs_response.status_code == work_response.status_code == 200
    assert snapshot_response.status_code == 200
    for response in (jobs_response, work_response, snapshot_response):
        serialized = response.get_data(as_text=True)
        assert all(identifier in serialized for identifier in owned_job_ids)
        assert all(identifier not in serialized for identifier in hidden_job_ids)

    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)
    stream = case.owner_client.get(
        "/api/v1/events?after=0",
        headers=case.owner_headers,
        buffered=False,
    )
    chunks: list[str] = []
    try:
        for _ in range(4):
            chunk = next(stream.response).decode("utf-8")
            chunks.append(chunk)
            if any(marker in chunk for marker in allowed_payloads):
                break
    finally:
        stream.close()
    serialized_stream = "".join(chunks)
    assert any(marker in serialized_stream for marker in allowed_payloads)
    assert all(marker not in serialized_stream for marker in hidden_event_payloads)
    assert "omitted-job-log-" not in serialized_stream

    tail_job_id = next(iter(hidden_job_ids))
    _add_job_event(
        case,
        tail_job_id,
        "job.progress",
        {"detail": "hidden-only-tail", "progress": 1.0},
    )
    _add_job_event(
        case,
        tail_job_id,
        "job.log",
        {"message": "hidden-log-tail"},
    )
    with case.database.snapshot_session() as session:
        bounds = work.event_bounds_in_session(session)
        hidden_tail = work.events_after_in_session(
            session,
            owner_api.context,
            allowed_cursor,
            limit=2,
        )
        assert hidden_tail.items == []
        assert hidden_tail.next_cursor == bounds.latest
        assert hidden_tail.next_cursor > allowed_cursor


def test_work_cancel_replay_rechecks_the_live_workspace_grant(
    private_case: Any,
) -> None:
    case = private_case
    api = _api_credential(
        case,
        case.owner_id,
        scopes=PRIVATE_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )
    transcription_id = _create_uploaded(
        api.client,
        api.headers,
        key="private-cancel-create",
    )
    started = _start_transcription(api.client, api.headers, transcription_id)
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["job_id"]
    key = "private-work-cancel-replay"
    request_headers = {**api.headers, "Idempotency-Key": key}
    first = api.client.post(
        f"/api/v1/work/{job_id}/cancel",
        headers=request_headers,
    )
    assert first.status_code == 200, first.get_json()
    receipt_after_first = _receipt_row(
        case,
        subject=api.actor.subject,
        key=key,
    )
    assert receipt_after_first is not None
    with case.database.snapshot_session() as session:
        token_grant = session.get(
            ApiTokenWorkspaceGrant,
            (api.token.id, case.owner_workspace_id),
        )
        assert token_grant is not None

    stable_before_replay = _mutation_snapshot(case, transcription_id, job_id)
    with case.database.immediate_session() as session:
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(
                ApiTokenWorkspaceGrant.token_id == api.token.id,
                ApiTokenWorkspaceGrant.workspace_id == case.owner_workspace_id,
            )
        )
    revoked_state = _mutation_snapshot(case, transcription_id, job_id)
    replay = api.client.post(
        f"/api/v1/work/{job_id}/cancel",
        headers=request_headers,
    )
    assert replay.status_code == 404, replay.get_json()
    assert _mutation_snapshot(case, transcription_id, job_id) == revoked_state
    assert _receipt_row(case, subject=api.actor.subject, key=key) == receipt_after_first
    assert stable_before_replay == revoked_state


@pytest.mark.parametrize("revocation", ("token_workspace_grant", "owner_membership"))
def test_stale_orm_rows_do_not_grant_private_transcription_access(
    private_case: Any,
    revocation: str,
) -> None:
    case = private_case
    private_id = _create_transcription(
        case.owner_client,
        case.owner_headers,
        key=f"private-stale-row-{revocation}",
    ).get_json()["id"]
    api = _api_credential(
        case,
        case.owner_id,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )

    with case.database.immediate_session() as session:
        token_grant = session.get(
            ApiTokenWorkspaceGrant,
            (api.token.id, case.owner_workspace_id),
        )
        membership = session.get(
            WorkspaceMembership,
            (case.owner_workspace_id, case.owner_id),
        )
        assert token_grant is not None
        assert membership is not None
        assert get_private_transcription(session, api.context, private_id).id == private_id

        if revocation == "token_workspace_grant":
            session.execute(
                delete(ApiTokenWorkspaceGrant)
                .where(
                    ApiTokenWorkspaceGrant.token_id == api.token.id,
                    ApiTokenWorkspaceGrant.workspace_id == case.owner_workspace_id,
                )
                .execution_options(synchronize_session=False)
            )
        else:
            session.execute(
                delete(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == case.owner_workspace_id,
                    WorkspaceMembership.user_id == case.owner_id,
                )
                .execution_options(synchronize_session=False)
            )
        with pytest.raises(PrivateTranscriptionNotFound):
            get_private_transcription(session, api.context, private_id)


def test_event_stream_stops_after_api_revocation_before_next_private_batch(
    private_case: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = private_case
    api = _api_credential(
        case,
        case.owner_id,
        scopes=PRIVATE_SCOPES,
        resource_grants=CredentialResources(workspace_ids=frozenset({case.owner_workspace_id})),
    )
    transcription_id = _create_uploaded(
        api.client,
        api.headers,
        key="private-stream-revocation-create",
    )
    started = _start_transcription(api.client, api.headers, transcription_id)
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["job_id"]
    first_event_id = _add_job_event(
        case,
        job_id,
        "job.progress",
        {"detail": "first-authorized-private-batch", "progress": 0.25},
    )

    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)
    stream = api.client.get(
        f"/api/v1/events?after={first_event_id - 1}",
        headers=api.headers,
        buffered=False,
    )
    observed: list[str] = []
    try:
        first_chunk = next(stream.response).decode("utf-8")
        observed.append(first_chunk)
        assert "first-authorized-private-batch" in first_chunk

        with case.database.immediate_session() as session:
            session.execute(
                update(ApiToken).where(ApiToken.id == api.token.id).values(revoked_at=utcnow())
            )
        _add_job_event(
            case,
            job_id,
            "job.progress",
            {"detail": "must-not-cross-revocation", "progress": 0.75},
        )

        closed = False
        for _ in range(3):
            try:
                chunk = next(stream.response).decode("utf-8")
            except StopIteration:
                closed = True
                break
            observed.append(chunk)
        assert closed is True
    finally:
        stream.close()

    assert "must-not-cross-revocation" not in "".join(observed)
