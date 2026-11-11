"""Approved job commands, shared bundle retries and writer-time authority."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, event, func, select, update

from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.jobs import Worker
from pandrator.web.models import ApiIdempotency, Job, JobActorRecord
from pandrator.web.openapi import build_openapi_document
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential, grant_workspace_content_access
from tests.test_job_access_routes import no_network as no_network

ADMIN = frozenset({"app.admin", "app.read", "app.run", "app.write"})


def counts(case):
    with case.database.snapshot_session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Job, JobActorRecord, ApiIdempotency)
        )


def bundle_path(case):
    return f"/api/v1/sessions/{case.a.id}/bundle"


def test_raw_diagnostic_selects_actual_admin_personal_root_and_executes(case):
    caller = credential(case, "outsider", scopes=ADMIN, grant_team=False)
    response = caller.client.post(
        "/api/v1/jobs",
        headers=caller.headers,
        json={
            "kind": "noop",
            "payload": {
                "duration": 0,
                "echo": "scoped diagnostic",
                "actor_user_id": "forged",
                "actor_source": "host",
            },
        },
    )
    assert response.status_code == 202, response.get_json()
    identifier = response.get_json()["id"]
    with case.database.snapshot_session() as session:
        record, actor = session.get(Job, identifier), session.get(JobActorRecord, identifier)
        assert record.workspace_id == case.personal[case.users["outsider"]]
        assert record.project_id is record.session_id is None
        assert actor.user_id == case.users["outsider"]
        assert (
            "actor_user_id" not in record.payload_json and "actor_source" not in record.payload_json
        )
    worker = Worker(
        case.services.jobs, "approved-diagnostic", case.services.workflow_handlers.handlers()
    )
    assert worker.run_once()
    completed = case.services.jobs.get(identifier)
    assert completed.status == "succeeded", completed.error_message
    assert completed.result_json == {"echo": "scoped diagnostic", "worker": "ready"}


@pytest.mark.parametrize(
    "kind",
    [
        "future.kind",
        "tts.preview",
        "rvc.model.upload",
        "training.xtts",
        "source.download_url",
        "maintenance.session_purge",
    ],
)
def test_unapproved_worker_payload_cannot_bypass_domain_admission(case, monkeypatch, kind):
    caller = credential(case, "outsider", scopes=ADMIN)
    resolve = Mock(side_effect=AssertionError("Unapproved submission resolved an artifact"))
    enqueue = Mock(side_effect=AssertionError("Unapproved submission reached queue"))
    monkeypatch.setattr(case.services.artifacts, "resolve", resolve)
    monkeypatch.setattr(case.services.jobs, "enqueue_in_session", enqueue)
    before = counts(case)
    response = caller.client.post(
        "/api/v1/jobs",
        headers=caller.headers,
        json={
            "kind": kind,
            "session_id": case.a.id,
            "payload": {
                "pth_artifact_id": "foreign",
                "source_artifact_id": "foreign",
                "path": "/untrusted/input",
            },
        },
    )
    assert response.status_code == 422, response.get_json()
    assert response.get_json()["error"]["code"] == "job_operation_required"
    assert counts(case) == before
    resolve.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"duration": 31},
        {"duration": float("nan")},
        {"source_artifact_id": "foreign"},
        {"workspace_id": "foreign"},
    ],
)
def test_diagnostic_schema_rejects_unapproved_controls(case, payload):
    caller = credential(case, "owner", scopes=ADMIN)
    before = counts(case)
    response = caller.client.post(
        "/api/v1/jobs", headers=caller.headers, json={"kind": "noop", "payload": payload}
    )
    assert response.status_code == 422, response.get_json()
    assert counts(case) == before


def test_instance_admin_is_not_authority_for_a_project_diagnostic(case):
    caller = credential(case, "outsider", scopes=ADMIN)
    response = caller.client.post(
        "/api/v1/jobs", headers=caller.headers, json={"kind": "noop", "session_id": case.a.id}
    )
    assert response.status_code == 404, response.get_json()
    assert counts(case) == (0, 0, 0)


def test_domain_bundle_accepts_ordinary_editor_and_rejects_viewer_and_outside_admin(case):
    before = counts(case)
    for name, status in (("viewer", 403), ("outsider", 404)):
        caller = credential(
            case, name, scopes=ADMIN if name == "outsider" else frozenset({"app.read", "app.run"})
        )
        denied = caller.client.post(bundle_path(case), headers=caller.headers, json={})
        assert denied.status_code == status, denied.get_json()
        assert counts(case) == before
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor")
    response = editor.client.post(
        bundle_path(case), headers=editor.headers, json={"include_sources": False}
    )
    assert response.status_code == 202, response.get_json()
    with case.database.snapshot_session() as session:
        record, actor = (
            session.get(Job, response.get_json()["id"]),
            session.get(JobActorRecord, response.get_json()["id"]),
        )
        assert record.kind == "session.bundle.export"
        assert (record.workspace_id, record.project_id, record.session_id) == (
            case.team,
            case.a.project_id,
            case.a.id,
        )
        assert record.payload_json == {"session_id": case.a.id, "include_sources": False}
        assert actor.user_id == case.users["editor"]


def test_bundle_routes_share_canonical_retry_without_duplicate_work(case):
    caller = credential(case, "owner", scopes=ADMIN)
    headers = {**caller.headers, "Idempotency-Key": "shared-bundle-command"}
    first = caller.client.post(bundle_path(case), headers=headers, json={"include_sources": False})
    assert first.status_code == 202, first.get_json()
    second = caller.client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "session.bundle.export",
            "session_id": case.a.id,
            "payload": {"include_sources": False},
        },
    )
    assert second.status_code == 202 and second.get_json() == first.get_json()
    assert second.headers["Idempotency-Replayed"] == "true"
    assert counts(case) == (1, 1, 1)
    conflict = caller.client.post(
        bundle_path(case), headers=headers, json={"include_sources": True}
    )
    assert conflict.status_code == 409
    assert counts(case) == (1, 1, 1)


@pytest.mark.parametrize("loss,status", [("membership", 404), ("grant", 404)])
def test_bundle_replay_rechecks_current_authority_before_receipt(case, monkeypatch, loss, status):
    grant_workspace_content_access(case, "editor")
    caller = credential(case, "editor")
    headers = {**caller.headers, "Idempotency-Key": "revoked-bundle-command"}
    assert caller.client.post(bundle_path(case), headers=headers, json={}).status_code == 202
    with case.database.immediate_session() as session:
        predicate = (
            ProjectMembership.project_id == case.a.project_id,
            ProjectMembership.user_id == case.users["editor"],
        )
        if loss == "membership":
            session.execute(delete(ProjectMembership).where(*predicate))
            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == case.team,
                    WorkspaceMembership.user_id == case.users["editor"],
                )
            )
        else:
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == caller.token.id
                )
            )
    begin = Mock(side_effect=AssertionError("Revoked caller reached receipt"))
    monkeypatch.setattr(case.services.idempotency, "begin", begin)
    before = counts(case)
    response = caller.client.post(bundle_path(case), headers=headers, json={})
    assert response.status_code == status, response.get_json()
    assert counts(case) == before
    begin.assert_not_called()


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_project_only_role_cannot_establish_bundle_run_authority(case, monkeypatch, role):
    """Current policy: project roles are viewing exceptions, not run authority."""
    if role != "editor":
        with case.database.immediate_session() as session:
            session.execute(
                update(ProjectMembership)
                .where(
                    ProjectMembership.project_id == case.a.project_id,
                    ProjectMembership.user_id == case.users["editor"],
                )
                .values(role=role)
            )
    # The explicit token grant covers the workspace; the caller deliberately has
    # no WorkspaceMembership, so only the project role could supply run authority.
    caller = credential(case, "editor")
    begin = Mock(side_effect=AssertionError("Project-only role reached receipt"))
    monkeypatch.setattr(case.services.idempotency, "begin", begin)
    before = counts(case)
    response = caller.client.post(
        bundle_path(case),
        headers={**caller.headers, "Idempotency-Key": f"project-only-{role}"},
        json={},
    )
    assert response.status_code == 403, response.get_json()
    assert counts(case) == before
    begin.assert_not_called()


def test_bundle_same_subject_different_user_cannot_probe_foreign_receipt(case):
    grant_workspace_content_access(case, "editor")
    first = credential(case, "editor", subject="shared-bundle-subject")
    headers = {**first.headers, "Idempotency-Key": "private-bundle-receipt"}
    assert first.client.post(bundle_path(case), headers=headers, json={}).status_code == 202
    outside = credential(case, "outsider", subject="shared-bundle-subject")
    own = case.services.sessions.create(
        "Other caller personal project", workspace_id=case.personal[case.users["outsider"]]
    )
    before = counts(case)
    response = outside.client.post(
        f"/api/v1/sessions/{own.id}/bundle",
        headers={**outside.headers, "Idempotency-Key": "private-bundle-receipt"},
        json={"include_sources": False},
    )
    assert response.status_code == 404, response.get_json()
    assert counts(case) == before


def test_bundle_root_mismatch_and_failed_enqueue_do_not_leave_receipts(case, monkeypatch):
    caller = credential(case, "owner", scopes=ADMIN)
    response = caller.client.post(
        "/api/v1/jobs",
        headers=caller.headers,
        json={
            "kind": "session.bundle.export",
            "session_id": case.a.id,
            "payload": {"session_id": case.b.id},
        },
    )
    assert response.status_code == 422
    monkeypatch.setattr(
        case.services.jobs,
        "enqueue_in_session",
        Mock(side_effect=ValueError("Synthetic enqueue conflict")),
    )
    response = caller.client.post(
        bundle_path(case),
        headers={**caller.headers, "Idempotency-Key": "bundle-rollback-fixture"},
        json={},
    )
    assert response.status_code == 409
    assert counts(case) == (0, 0, 0)


def test_bundle_waiting_for_writer_rechecks_membership(case, monkeypatch):
    grant_workspace_content_access(case, "editor")
    caller = credential(case, "editor")
    ready, release, waiting = threading.Event(), threading.Event(), threading.Event()
    future = Future()
    thread_name = "bundle-writer-role-race"
    original = ApiGuards.access_context

    def capture(guards):
        context = original(guards)
        if threading.current_thread().name == thread_name:
            ready.set()
            assert release.wait(10)
        return context

    def before_sql(_conn, _cursor, statement, _params, _context, _many):
        if (
            threading.current_thread().name == thread_name
            and statement.strip().upper() == "BEGIN IMMEDIATE"
        ):
            waiting.set()

    def submit():
        try:
            future.set_result(
                case.app.test_client().post(
                    bundle_path(case),
                    headers={**caller.headers, "Idempotency-Key": "bundle-writer-revoked"},
                    json={},
                )
            )
        except BaseException as error:
            future.set_exception(error)

    monkeypatch.setattr(ApiGuards, "access_context", capture)
    event.listen(case.database.engine, "before_cursor_execute", before_sql)
    thread = threading.Thread(target=submit, name=thread_name)
    try:
        thread.start()
        assert ready.wait(10)
        with case.database.immediate_session() as session:
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
            release.set()
            assert waiting.wait(5)
        thread.join(10)
        assert not thread.is_alive()
        response = future.result(1)
        assert response.status_code == 404, response.get_json()
        assert counts(case) == (0, 0, 0)
    finally:
        release.set()
        thread.join(10)
        event.remove(case.database.engine, "before_cursor_execute", before_sql)
        assert not thread.is_alive()


def test_openapi_exposes_approved_kinds_and_bundle_retry_contract():
    paths = build_openapi_document()["paths"]
    command = paths["/api/v1/jobs"]["post"]
    schema = command["requestBody"]["content"]["application/json"]["schema"]
    assert schema["allOf"][1]["properties"]["kind"]["enum"] == ["noop", "session.bundle.export"]
    assert "422" in command["responses"]
    assert command["x-required-scopes"] == ["app.admin", "app.run"]
    assert (
        paths["/api/v1/sessions/{sessionId}/bundle"]["post"]["parameters"][0]["name"]
        == "Idempotency-Key"
    )
