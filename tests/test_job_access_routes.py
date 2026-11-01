"""Actual HTTP job boundaries, cancellation hints, blockers and label privacy."""

from __future__ import annotations

import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests.sessions
from sqlalchemy import delete, func, select

from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.api import create_app
from pandrator.web.models import ApiIdempotency, Job, ResourceClaim, new_id, utcnow
from pandrator.web.projects.models import ProjectMembership
from tests.web_test_support import prepare_web_test_data_root

SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})


@pytest.fixture(scope="module", autouse=True)
def no_network():
    attempts = []
    patcher = pytest.MonkeyPatch()

    def blocked(*_args, **_kwargs):
        attempts.append("network")
        raise AssertionError("Job route tests must not use the network.")

    patcher.setattr(requests.sessions.Session, "request", blocked)
    patcher.setattr(socket.socket, "connect", blocked)
    try:
        yield
    finally:
        patcher.undo()
        assert not attempts


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "job-routes"
    prepare_web_test_data_root(root)
    app = create_app(data_root=root, testing=True, background_maintenance=False)
    services = app.extensions["pandrator"]["services"]
    database, auth = services.database, services.auth
    try:
        owner_id = auth.initial_account().user_id
        with database.immediate_session() as session:
            users = {"owner": owner_id}
            personal = {owner_id: initial_personal_workspace(session).id}
            for name in ("editor", "viewer", "member", "outsider"):
                user_id = new_id()
                users[name] = user_id
                session.add(
                    User(
                        id=user_id,
                        display_name=name,
                        is_active=True,
                        is_instance_admin=name == "outsider",
                    )
                )
                session.flush()
                session.add(UserCredential(user_id=user_id, login_name=user_id, auth_revision=1))
                workspace = Workspace(
                    id=new_id(),
                    kind="personal",
                    name=name,
                    personal_owner_user_id=user_id,
                    state="active",
                )
                session.add(workspace)
                session.flush()
                session.add(
                    WorkspaceMembership(workspace_id=workspace.id, user_id=user_id, role="owner")
                )
                personal[user_id] = workspace.id
            team = Workspace(id=new_id(), kind="team", name="Job routes team", state="active")
            session.add(team)
            session.flush()
            # Project-only editor/viewer fixtures retain the legacy grant-specific cases.
            # The separate member has inherited content rights without project rows.
            for name, role in (("owner", "owner"), ("member", "member")):
                session.add(
                    WorkspaceMembership(workspace_id=team.id, user_id=users[name], role=role)
                )
        a = services.sessions.create("Allowed project label", workspace_id=team.id)
        b = services.sessions.create("Private foreign project label sentinel", workspace_id=team.id)
        with database.immediate_session() as session:
            for name, role in (("editor", "editor"), ("viewer", "viewer")):
                session.add(
                    ProjectMembership(project_id=a.project_id, user_id=users[name], role=role)
                )
        real_gate = auth.solo_access_allowed
        known = frozenset(users.values())
        monkeypatch.setattr(
            auth,
            "solo_access_allowed",
            lambda user_id, revision: (
                (user_id in known and revision == 1) or real_gate(user_id, revision)
            ),
        )
        yield SimpleNamespace(
            app=app,
            services=services,
            database=database,
            auth=auth,
            users=users,
            personal=personal,
            team=team.id,
            a=a,
            b=b,
        )
    finally:
        services.close()


def credential(case, name, *, scopes=SCOPES, subject=None, grant_team=True):
    token, raw = case.auth.create_api_token(
        "Synthetic job route credential",
        user_id=case.users[name],
        auth_revision=1,
        scopes=scopes,
        subject=subject,
    )
    if grant_team:
        with case.database.immediate_session() as session:
            # Explicit fixture ceiling, including the outsider's overbroad negative.
            session.add(ApiTokenWorkspaceGrant(token_id=token.id, workspace_id=case.team))
    principal = case.auth.resolve_api_token(raw, network_zone="loopback", target_instance_id=None)
    assert principal is not None
    return SimpleNamespace(
        client=case.app.test_client(),
        headers={"Authorization": f"Bearer {raw}"},
        token=token,
        actor=actor_from_principal(principal),
    )


def job(case, *, project="a", author="editor", created_at=None, **values):
    identifier = new_id()
    root = getattr(case, project) if project else None
    with case.database.immediate_session() as session:
        row = Job(
            id=identifier,
            kind="future.synthetic",
            workspace_id=case.team if root else case.personal[case.users[author]],
            project_id=root.project_id if root else None,
            session_id=root.id if root else None,
            created_at=created_at or utcnow(),
            payload_json=values.pop("payload_json", {}),
            **values,
        )
        session.add(row)
        session.flush()
        session.add(
            JobActorRecord(
                job_id=identifier,
                user_id=case.users[author],
                auth_revision=7,
                context_json={"version": 1, "source": "user", "subject": "old credential subject"},
            )
        )
    return identifier


def error(response):
    data = response.get_json()["error"]
    assert data.pop("request_id")
    return response.status_code, data


@pytest.mark.parametrize("suffix", ["jobs/{id}", "jobs/{id}/logs", "work/{id}", "work/{id}/events"])
def test_generic_http_detail_and_events_match_hidden_with_missing(case, suffix):
    editor = credential(case, "editor")
    own, hidden = job(case), job(case, project="b")
    assert (
        editor.client.get("/api/v1/" + suffix.format(id=own), headers=editor.headers).status_code
        == 200
    )
    denied = editor.client.get("/api/v1/" + suffix.format(id=hidden), headers=editor.headers)
    missing = editor.client.get("/api/v1/" + suffix.format(id=new_id()), headers=editor.headers)
    assert error(denied) == error(missing)
    assert denied.status_code == 404


def test_lists_and_snapshot_scope_before_limit_even_with_broader_workspace_grant(case):
    editor = credential(case, "editor")
    own = {job(case, created_at=utcnow() - timedelta(days=2)) for _ in range(2)}
    hidden = {job(case, project="b") for _ in range(55)}
    for path in (
        "/api/v1/jobs?limit=2",
        "/api/v1/work?limit=2",
        "/api/v1/events/snapshot?view=compact",
    ):
        response = editor.client.get(path, headers=editor.headers)
        assert response.status_code == 200, response.get_json()
        body = response.get_data(as_text=True)
        assert all(identifier in body for identifier in own)
        assert all(identifier not in body for identifier in hidden)
        assert case.b.name not in body


def grant_workspace_content_access(case, name):
    with case.database.immediate_session() as session:
        session.add(
            WorkspaceMembership(workspace_id=case.team, user_id=case.users[name], role="member")
        )


def test_project_only_viewing_grant_cannot_cancel_even_own_jobs(case):
    editor = credential(case, "editor")
    identifier = job(case)
    response = editor.client.get(f"/api/v1/work/{identifier}", headers=editor.headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["cancellable"] is False
    denied = editor.client.post(
        f"/api/v1/work/{identifier}/cancel",
        headers={**editor.headers, "Idempotency-Key": "view-only-own-job"},
    )
    assert denied.status_code == 403, denied.get_json()
    with case.database.snapshot_session() as session:
        assert session.get(Job, identifier).status == "queued"
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0


def test_work_hints_and_cancel_commands_follow_workspace_authority_and_token_scopes(case):
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor", subject="current editor credential")
    viewer = credential(case, "viewer")
    manager = credential(case, "owner")
    read_only = credential(case, "editor", scopes=frozenset({"app.read"}))
    outsider = credential(case, "outsider", scopes=SCOPES | {"app.admin"})
    own, other = job(case), job(case, author="owner")
    for client, allowed in ((editor, True), (viewer, False), (manager, True), (read_only, False)):
        response = client.client.get(f"/api/v1/work/{own}", headers=client.headers)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["cancellable"] is allowed
        listing = client.client.get("/api/v1/work", headers=client.headers).get_json()["items"]
        assert next(row for row in listing if row["id"] == own)["cancellable"] is allowed
    assert (
        editor.client.get(f"/api/v1/work/{other}", headers=editor.headers).get_json()["cancellable"]
        is True
    )
    assert outsider.client.get(f"/api/v1/work/{own}", headers=outsider.headers).status_code == 404
    with case.database.snapshot_session() as session:
        receipts = session.scalar(select(func.count()).select_from(ApiIdempotency))
    for client, target in ((viewer, own), (viewer, other), (read_only, other)):
        denied = client.client.post(
            f"/api/v1/work/{target}/cancel",
            headers={**client.headers, "Idempotency-Key": "denied-cancel"},
        )
        assert denied.status_code == 403, denied.get_json()
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == receipts
        assert session.get(Job, own).status == session.get(Job, other).status == "queued"
    assert (
        editor.client.post(
            f"/api/v1/work/{own}/cancel",
            headers={**editor.headers, "Idempotency-Key": "own-cancel"},
        ).status_code
        == 200
    )
    assert (
        manager.client.post(f"/api/v1/jobs/{other}/cancel", headers=manager.headers).status_code
        == 200
    )


def test_cancel_replay_rechecks_membership_before_receipt(case):
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor")
    identifier = job(case)
    headers = {**editor.headers, "Idempotency-Key": "revoked-role-replay"}
    first = editor.client.post(f"/api/v1/work/{identifier}/cancel", headers=headers)
    assert first.status_code == 200, first.get_json()
    with case.database.immediate_session() as session:
        before = [
            (row.id, row.request_digest, row.response_json)
            for row in session.scalars(select(ApiIdempotency))
        ]
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["editor"],
            )
        )
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.users["editor"],
            )
        )
    replay = editor.client.post(f"/api/v1/work/{identifier}/cancel", headers=headers)
    assert replay.status_code == 404, replay.get_json()
    with case.database.snapshot_session() as session:
        assert [
            (row.id, row.request_digest, row.response_json)
            for row in session.scalars(select(ApiIdempotency))
        ] == before
        assert session.get(Job, identifier).status == "canceled"


def test_hidden_resource_blocker_does_not_leak_or_suppress_visible_blocker(case):
    editor = credential(case, "editor")
    keys = ["gpu:hidden-blocker", "cpu:visible-blocker"]
    future = utcnow() + timedelta(minutes=5)
    hidden = job(
        case,
        project="b",
        status="running",
        lease_owner="hidden-worker",
        lease_generation=1,
        lease_expires_at=future,
        progress_detail="Foreign private blocker detail",
        created_at=utcnow() - timedelta(minutes=2),
    )
    visible = job(
        case,
        status="running",
        lease_owner="visible-worker",
        lease_generation=1,
        lease_expires_at=future,
        progress_detail="Visible blocker detail",
        created_at=utcnow() - timedelta(minutes=1),
    )
    waiter = job(case, resource_keys_json=keys)
    with case.database.immediate_session() as session:
        session.add_all(
            [
                ResourceClaim(
                    resource_key=keys[0],
                    job_id=hidden,
                    lease_owner="hidden-worker",
                    lease_generation=1,
                    expires_at=future,
                ),
                ResourceClaim(
                    resource_key=keys[1],
                    job_id=visible,
                    lease_owner="visible-worker",
                    lease_generation=1,
                    expires_at=future,
                ),
            ]
        )
    for path in (f"/api/v1/jobs/{waiter}", "/api/v1/jobs", "/api/v1/events/snapshot?view=compact"):
        response = editor.client.get(path, headers=editor.headers)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        item = (
            body
            if path.endswith(waiter)
            else next(
                row
                for row in (body["jobs"]["items"] if "snapshot" in path else body["items"])
                if row["id"] == waiter
            )
        )
        assert item["waiting_for_job"]["id"] == visible
        assert hidden not in response.get_data(as_text=True)
        assert "Foreign private blocker detail" not in response.get_data(as_text=True)


def test_raw_payload_session_cannot_lookup_a_foreign_project_label(case):
    editor = credential(case, "editor")
    identifier = job(case, project=None, payload_json={"session_id": case.b.id})
    response = editor.client.get(f"/api/v1/jobs/{identifier}", headers=editor.headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["project"] is None
    assert case.b.name not in response.get_data(as_text=True)
