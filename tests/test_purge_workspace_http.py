"""Manager deletion, private inspections and installation retention boundaries."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.jobs import Worker
from pandrator.web.session_purge import SessionPurgeService
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token


@pytest.fixture
def people(case, monkeypatch):
    return SimpleNamespace(case=case, u=team(case, monkeypatch),
        services=case.app.extensions["pandrator"]["services"])


def project(client, csrf, workspace=None):
    values = {"name": "Deletion authority fixture", "workflow_kind": "subtitles"}
    if workspace is not None:
        values["workspace_id"] = workspace
    return body(client.post("/api/v1/sessions", json=values,
        headers=headers(csrf, str(uuid4()))), 201)


def promote(f):
    u = f.u
    root = f"/api/v1/workspaces/{u.workspace_id}"
    revision = body(u.owner.get(root))["revision"]
    body(u.owner.patch(root + "/members/" + u.alice_id,
        json={"expected_revision": revision, "role": "manager"}, headers=u.oh))


def trash(client, csrf, record):
    current = body(client.get(f"/api/v1/sessions/{record['id']}"))
    body(client.delete(f"/api/v1/sessions/{record['id']}",
        headers={**csrf, "If-Match": str(current["revision"])}))


def inspection(client, csrf, record, *, key=None):
    return client.post(f"/api/v1/sessions/{record['id']}/purge-inspections", json={},
        headers=headers(csrf, key or str(uuid4())))


def worker(f, receipt):
    assert Worker(f.services.jobs, "named-purge-worker", f.services.workflow_handlers.handlers()).run_once()
    return f.services.jobs.get(receipt["job_id"])


def counts(f):
    with f.case.database.snapshot_session() as unit:
        return {model.__tablename__: unit.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.ApiIdempotency, m.SessionPurge)}


def marker(f, record):
    with f.case.database.snapshot_session() as unit:
        storage_key = unit.get_one(m.SessionRecord, record["id"]).storage_key
    path = f.services.paths.sessions / storage_key / "owned.marker"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Disposable deletion acceptance data")
    return path


def test_foreign_user_and_installation_admin_denial_precedes_json_or_deletion(people, monkeypatch):
    f, u = people, people.u
    record = project(u.bob, u.bh)
    path = marker(f, record)
    trash(u.bob, u.bh, record)
    before = counts(f)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Foreign purge reached request parsing")

    monkeypatch.setattr(Request, "get_json", forbidden)
    root = f"/api/v1/sessions/{record['id']}"
    for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
        assert client.get(root + "/purge-preview").status_code == 404
        for action in ("purge", "purge-inspections", "purge-admissions"):
            assert client.post(root + "/" + action, json={},
                headers=headers(csrf, str(uuid4()))).status_code == 404
    assert path.is_file() and counts(f) == before


def test_member_creator_cannot_inspect_or_permanently_delete_team_project(people, monkeypatch):
    u = people.u
    record = project(u.alice, u.ah, u.workspace_id)
    trash(u.alice, u.ah, record)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Member governance request reached JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    root = f"/api/v1/sessions/{record['id']}"
    assert u.alice.get(root + "/purge-preview").status_code == 403
    for action in ("purge", "purge-inspections", "purge-admissions"):
        assert u.alice.post(root + "/" + action, json={},
            headers=headers(u.ah, str(uuid4()))).status_code == 403


def test_read_only_manager_inspects_under_expired_admitted_token_and_results_are_private(people):
    f, u = people, people.u
    promote(f)
    record = project(u.alice, u.ah, u.workspace_id)
    trash(u.alice, u.ah, record)
    token, bearer = scoped_token(f.case, u.alice_id, {"app.read"}, workspace=u.workspace_id)
    api = f.case.app.test_client()
    queued = body(inspection(api, bearer, record), 202)
    assert api.post(f"/api/v1/sessions/{record['id']}/purge-admissions", json={},
        headers=headers(bearer, "read-only-manager-purge")).status_code == 403
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.ApiToken, token.id).expires_at = m.utcnow() - timedelta(seconds=1)
    completed = worker(f, queued)
    assert completed.status == "succeeded", completed.error_message
    assert body(u.alice.get(f"/api/v1/purge-inspections/{completed.id}"))["inspection"]["can_purge"]
    assert u.alice.get(f"/api/v1/work/{completed.id}/events").status_code == 200
    assert u.bob.get(f"/api/v1/purge-inspections/{completed.id}").status_code == 404
    assert u.bob.get(f"/api/v1/jobs/{completed.id}").status_code == 404
    assert u.bob.get(f"/api/v1/work/{completed.id}/events").status_code == 404


def test_governance_authority_is_rechecked_before_admission_replay(people, monkeypatch):
    f, u = people, people.u
    promote(f)
    record = project(u.alice, u.ah, u.workspace_id)
    queued = body(inspection(u.alice, u.ah, record, key="current-purge-replay"), 202)
    before, original = counts(f), Request.get_json

    def parse_then_demote(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path.endswith("/purge-inspections"):
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)).role = "member"
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_demote)
    assert inspection(u.alice, u.ah, record, key="current-purge-replay").status_code == 403
    assert queued["job_id"] and counts(f) == before


def test_revoked_queued_actor_never_reads_private_inspection(people, monkeypatch):
    f, u = people, people.u
    promote(f)
    record = project(u.alice, u.ah, u.workspace_id)
    token, bearer = scoped_token(f.case, u.alice_id, {"app.read"}, workspace=u.workspace_id)
    queued = body(inspection(f.case.app.test_client(), bearer, record), 202)
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Revoked inspection reached private impact collection")

    monkeypatch.setattr(SessionPurgeService, "_inspect", forbidden)
    completed = worker(f, queued)
    assert completed.status != "succeeded" and not completed.result_json


def test_retention_is_admin_only_and_writer_rechecks_revocation(people, monkeypatch):
    f, u = people, people.u
    promote(f)
    original = Request.get_json

    def forbidden(*_args, **_kwargs):
        pytest.fail("User retention change reached JSON")

    monkeypatch.setattr(Request, "get_json", forbidden)
    assert u.alice.patch("/api/v1/session-trash-policy", json={}, headers=u.ah).status_code == 403
    owner_id = f.case.auth.initial_account().user_id
    _limited, admin_read = scoped_token(f.case, owner_id, {"app.admin"})
    api = f.case.app.test_client()
    assert api.patch("/api/v1/session-trash-policy", json={}, headers=admin_read).status_code == 403
    monkeypatch.setattr(Request, "get_json", original)
    saved = body(u.owner.patch("/api/v1/session-trash-policy",
        json={"expected_revision": 0, "days": 30}, headers=u.oh))
    assert saved == {"revision": 1, "days": 30}
    token, bearer = scoped_token(f.case, owner_id, {"app.admin", "app.write"})

    def parse_then_revoke(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path.endswith("/session-trash-policy"):
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_revoke)
    assert api.patch("/api/v1/session-trash-policy", json={"expected_revision": 1, "days": 2},
        headers=bearer).status_code == 403
    assert body(u.owner.get("/api/v1/session-trash-policy")) == saved


def test_committed_cleanup_finishes_after_role_loss_without_another_actor_gate(people, monkeypatch):
    f, u = people, people.u
    promote(f)
    record = project(u.alice, u.ah, u.workspace_id)
    path = marker(f, record)
    trash(u.alice, u.ah, record)
    impact = body(u.alice.get(f"/api/v1/sessions/{record['id']}/purge-preview"))
    finish = SessionPurgeService._finish_locked

    def demote_after_claim(self, session_id):
        with f.case.database.immediate_session() as unit:
            assert unit.get_one(m.SessionPurge, session_id).state == "purging"
            unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)).role = "member"
        return finish(self, session_id)

    monkeypatch.setattr(SessionPurgeService, "_finish_locked", demote_after_claim)
    result = body(u.alice.post(f"/api/v1/sessions/{record['id']}/purge",
        json={"expected_revision": impact["revision"], "impact_token": impact["impact_token"]},
        headers=u.ah))
    assert result["state"] == "complete" and not path.exists()


@pytest.mark.parametrize("revoked", [False, True])
def test_completed_journal_worker_retry_proves_retained_scope_without_second_unlink(people, monkeypatch, revoked):
    f, u = people, people.u
    promote(f)
    record = project(u.alice, u.ah, u.workspace_id)
    path = marker(f, record)
    trash(u.alice, u.ah, record)
    impact = body(u.alice.get(f"/api/v1/sessions/{record['id']}/purge-preview"))
    token, bearer = scoped_token(f.case, u.alice_id, {"app.read", "app.write"}, project=record["project_id"])
    queued = body(f.case.app.test_client().post(f"/api/v1/sessions/{record['id']}/purge-admissions",
        json={"expected_revision": impact["revision"], "impact_token": impact["impact_token"], "confirm": True},
        headers=headers(bearer, "retained-purge-retry")), 202)
    completed = worker(f, queued)
    assert completed.status == "succeeded", completed.error_message
    assert not path.exists()
    with f.case.database.immediate_session() as unit:
        job = unit.get_one(m.Job, completed.id)
        job.status, job.finished_at, job.lease_owner, job.lease_expires_at = "queued", None, None, None
        if revoked:
            unit.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Complete-journal acknowledgement attempted a second unlink")

    monkeypatch.setattr(SessionPurgeService, "_remove_managed_entry", forbidden)
    retried = worker(f, queued)
    assert (retried.status == "succeeded") is not revoked
    with f.case.database.snapshot_session() as unit:
        assert unit.get(m.SessionRecord, record["id"]) is None
        assert unit.get_one(m.SessionPurge, record["id"]).state == "complete"
