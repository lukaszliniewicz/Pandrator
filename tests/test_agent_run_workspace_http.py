"""Agent history and state changes use current project and credential rights."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.openapi import build_openapi_document
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_source_cleaning_workspace_http import book, source
from tests.test_source_cleaning_workspace_http import cleaning as cleaning  # noqa: F401


def _seed_history(f, record, artifact):
    with f.case.database.immediate_session() as unit:
        run = m.AgentRun(
            id=m.new_id(),
            kind="source_cleaning",
            session_id=record["id"],
            source_artifact_id=artifact.id,
            source_content_hash=artifact.content_hash,
            status="failed",
            settings_json={"agentic": True},
        )
        unit.add(run)
        unit.flush()
        unit.add(
            m.AgentStep(
                agent_run_id=run.id,
                ordinal=0,
                phase="metadata",
                status="completed",
                summary="A saved cleaning phase",
                input_json={"text": "Protected source text"},
                output_json={"text": "Protected result text"},
            )
        )
        return run.id


def _counts(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model))
            for model in (m.AgentRun, m.AgentStep, m.Job)
        )


@contextmanager
def _forbid_protected_body_loads():
    def denied(_record, _context):
        raise AssertionError("A denied request must not load protected bodies")

    models = (m.SessionRecord, m.AgentRun, m.AgentStep, m.Artifact)
    for model in models:
        event.listen(model, "load", denied)
    try:
        yield
    finally:
        for model in models:
            event.remove(model, "load", denied)


def _complete(f, record, run_id):
    path = f.services.paths.uploads / f"{uuid4()}-cleaned.txt"
    path.write_text("Accepted shared cleaning result.")
    with execution_actor_scope(HostActor("pandrator-cli")):
        result = f.services.artifacts.register(
            path, kind="text", role="clean_text", session_id=record["id"]
        )
    with f.case.database.immediate_session() as unit:
        run = unit.get_one(m.AgentRun, run_id)
        run.status = "completed"
        run.result_artifact_id = result.id
    return result.id


@pytest.mark.parametrize("caller", ["outsider", "administrator", "narrowed_credential"])
def test_inaccessible_agent_management_denies_before_body_or_json(cleaning, monkeypatch, caller):
    f, u = cleaning, cleaning.u
    workspace = u.workspace_id if caller == "narrowed_credential" else None
    record = book(f, u.alice, u.ah, workspace)
    artifact, path = source(f, record)
    run_id = _seed_history(f, record, artifact)
    before = _counts(f)
    if caller == "administrator":
        client, auth = u.owner, u.oh
    elif caller == "narrowed_credential":
        other = book(f, u.bob, u.bh, u.workspace_id)
        _, auth = scoped_token(
            f.case, u.bob_id, {"app.read", "app.write", "app.run"}, project=other["project_id"]
        )
        client = f.case.app.test_client()
    else:
        client, auth = u.bob, u.bh

    def denied_json(*_args, **_kwargs):
        raise AssertionError("Project authority must precede request JSON")

    monkeypatch.setattr(Request, "get_json", denied_json)
    with _forbid_protected_body_loads():
        for url in (
            f"/api/v1/sessions/{record['id']}/agent-runs",
            f"/api/v1/agent-runs/{run_id}/steps",
        ):
            body(client.get(url, headers=auth), 404)
        body(
            client.post(
                f"/api/v1/sessions/{record['id']}/agent-runs",
                data="{",
                content_type="application/json",
                headers=auth,
            ),
            404,
        )
        for action in ("resume", "accept"):
            body(client.post(f"/api/v1/agent-runs/{run_id}/{action}", headers=auth), 404)
    assert _counts(f) == before
    assert path.read_text() == "Shared narration."


def test_private_viewer_can_read_history_but_cannot_process_or_accept(cleaning):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah)
    artifact, _path = source(f, record)
    run_id = _seed_history(f, record, artifact)
    viewers = f"/api/v1/projects/{record['project_id']}/viewers"
    current = body(u.alice.get(viewers))
    body(
        u.alice.put(
            f"{viewers}/{u.bob_id}",
            json={"expected_revision": current["revision"]},
            headers=u.ah,
        )
    )
    items = body(u.bob.get(f"/api/v1/sessions/{record['id']}/agent-runs"))["items"]
    assert [(item["id"], item["status"]) for item in items] == [(run_id, "failed")]
    steps = body(u.bob.get(f"/api/v1/agent-runs/{run_id}/steps"))["items"]
    assert steps[0]["input_json"] == {"text": "Protected source text"}
    before = _counts(f)
    body(
        u.bob.post(
            f"/api/v1/sessions/{record['id']}/agent-runs",
            json={"source_artifact_id": artifact.id, "settings": {}},
            headers=u.bh,
        ),
        403,
    )
    for action in ("resume", "accept"):
        body(u.bob.post(f"/api/v1/agent-runs/{run_id}/{action}", headers=u.bh), 403)
    assert _counts(f) == before


def test_member_resumes_after_creator_leaves_and_accepts_without_run_scope(cleaning):
    f, u = cleaning, cleaning.u
    record = book(f, u.alice, u.ah, u.workspace_id)
    artifact, path = source(f, record)
    receipt = body(
        u.alice.post(
            f"/api/v1/sessions/{record['id']}/agent-runs",
            json={"source_artifact_id": artifact.id, "settings": {}},
            headers=u.ah,
        ),
        202,
    )
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.Job, receipt["job_id"]).status = "failed"
        unit.get_one(m.AgentRun, receipt["id"]).status = "failed"
        unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
    assert u.alice.get(f"/api/v1/sessions/{record['id']}/agent-runs").status_code == 404
    _, run_auth = scoped_token(f.case, u.bob_id, {"app.read", "app.run"}, workspace=u.workspace_id)
    api = f.case.app.test_client()
    assert body(api.get(f"/api/v1/sessions/{record['id']}/agent-runs", headers=run_auth))["items"]
    resumed = body(api.post(f"/api/v1/agent-runs/{receipt['id']}/resume", headers=run_auth), 202)
    assert resumed["job_id"] != receipt["job_id"]
    with f.case.database.snapshot_session() as unit:
        actor = unit.get_one(m.JobActorRecord, resumed["job_id"])
        job = unit.get_one(m.Job, resumed["job_id"])
        assert actor.user_id == u.bob_id
        assert job.session_id == record["id"]
        assert job.payload_json["source_artifact_id"] == artifact.id
    result_id = _complete(f, record, receipt["id"])
    _, edit_auth = scoped_token(
        f.case, u.bob_id, {"app.read", "app.write"}, workspace=u.workspace_id
    )
    body(api.post(f"/api/v1/agent-runs/{receipt['id']}/accept", headers=run_auth), 403)
    body(api.post(f"/api/v1/agent-runs/{receipt['id']}/resume", headers=edit_auth), 403)
    accepted = body(api.post(f"/api/v1/agent-runs/{receipt['id']}/accept", headers=edit_auth))
    assert accepted["status"] == "accepted" and accepted["result_artifact_id"] == result_id
    assert path.read_text() == "Shared narration."


def test_default_single_user_can_admit_and_read_agent_history(case):
    services = case.app.extensions["pandrator"]["services"]
    # No named-user activation or workspace selector is needed for this path.
    f = SimpleNamespace(services=services, case=case)
    auth = {"X-CSRF-Token": case.csrf}
    record = book(f, case.client, auth)
    artifact, path = source(f, record)
    receipt = body(
        case.client.post(
            f"/api/v1/sessions/{record['id']}/agent-runs",
            json={"source_artifact_id": artifact.id, "settings": {}},
            headers=auth,
        ),
        202,
    )
    items = body(case.client.get(f"/api/v1/sessions/{record['id']}/agent-runs"))["items"]
    assert items[0]["id"] == receipt["id"]
    assert body(case.client.get(f"/api/v1/agent-runs/{receipt['id']}/steps"))["items"] == []
    with case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(m.User)) == 1
        assert unit.get_one(m.JobActorRecord, receipt["job_id"]).user_id == case.actor.user_id
    assert path.read_text() == "Shared narration."


def test_agent_run_openapi_scopes_match_read_run_and_edit_actions():
    paths = build_openapi_document()["paths"]
    for path, method, scopes in (
        ("/api/v1/sessions/{sessionId}/agent-runs", "get", ["app.read"]),
        ("/api/v1/sessions/{sessionId}/agent-runs", "post", ["app.read", "app.run"]),
        ("/api/v1/agent-runs/{runId}/steps", "get", ["app.read"]),
        ("/api/v1/agent-runs/{runId}/resume", "post", ["app.read", "app.run"]),
        ("/api/v1/agent-runs/{runId}/accept", "post", ["app.read", "app.write"]),
    ):
        assert paths[path][method]["x-required-scopes"] == scopes
        assert {"nativeOAuth": scopes} in paths[path][method]["security"]
