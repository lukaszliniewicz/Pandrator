"""Named-user source changes and speech-plan receipts use current authority."""

from __future__ import annotations

import io
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask.wrappers import Request
from sqlalchemy import delete, func, select

from pandrator.web import models as m
from pandrator.web import source_management
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_purge_workspace_http import project
from tests.test_source_library_workspace_http import library as library  # noqa: F401


def change_payload(client, root, source_id, csrf):
    values = {"role": "primary", "new_source_asset_id": source_id}
    preview = body(client.post(root + "/change-preview", json=values, headers=csrf))
    return {
        **values,
        "expected_revision": preview["session_revision"],
        "impact_token": preview["impact_token"],
    }


@pytest.mark.parametrize("actor", ["private_viewer", "foreign_admin", "read_only_member"])
def test_source_and_plan_mutations_reject_before_json_or_cleanup(library, monkeypatch, actor):
    f, u = library, library.u
    record = project(u.bob, u.bh, u.workspace_id if actor == "read_only_member" else None)
    if actor == "private_viewer":
        with f.case.database.immediate_session() as unit:
            unit.add(
                m.ProjectMembership(
                    project_id=record["project_id"], user_id=u.alice_id, role="viewer"
                )
            )
        client, csrf, status = u.alice, u.ah, 403
    elif actor == "foreign_admin":
        client, csrf, status = u.owner, u.oh, 404
    else:
        _, csrf = scoped_token(f.case, u.alice_id, {"app.read"}, workspace=u.workspace_id)
        client, status = f.case.app.test_client(), 403
    root = f"/api/v1/sessions/{record['id']}"
    assert client.get(root + "/sources/status", headers=csrf).status_code == (
        404 if status == 404 else 200
    )
    assert client.get(root + "/generation-plan/status", headers=csrf).status_code == (
        404 if status == 404 else 200
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("A denied source or plan action parsed JSON")

    cleanup = Mock(side_effect=AssertionError("A denied manual cleanup reached files"))
    monkeypatch.setattr(Request, "get_json", forbidden)
    monkeypatch.setattr("pandrator.web.session_flow_routes.cleanup_reset_files", cleanup)
    for suffix in (
        "sources/change-preview",
        "sources/change",
        "sources/start-new-session",
        "sources/cleanup",
        "sources/confirm-timing",
        "generation-plan/prepare",
        "generation-plan/select",
        "generation-plan/review",
    ):
        response = client.post(root + "/" + suffix, json={}, headers=headers(csrf, str(uuid4())))
        assert response.status_code == status, (suffix, response.get_json())
    cleanup.assert_not_called()


def test_foreign_selected_input_denies_before_impact_or_native_inspection(library, monkeypatch):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    root = f"/api/v1/sessions/{record['id']}/sources"
    impact = Mock(side_effect=AssertionError("Foreign source reached impact/native work"))
    monkeypatch.setattr(source_management, "_impact", impact)
    values = {"role": "primary", "new_source_asset_id": f.private.id}
    assert u.alice.post(root + "/change-preview", json=values, headers=u.ah).status_code == 404
    for suffix in ("change", "start-new-session"):
        response = u.alice.post(
            root + "/" + suffix,
            json={**values, "expected_revision": 1, "impact_token": "a" * 64},
            headers=headers(u.ah, str(uuid4())),
        )
        assert response.status_code == 404, response.get_json()
    impact.assert_not_called()


@pytest.mark.parametrize("operation", ["change", "start-new-session"])
def test_member_source_receipt_rechecks_scope_after_json(library, monkeypatch, operation):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write"}, workspace=u.workspace_id
    )
    client = f.case.app.test_client()
    root = f"/api/v1/sessions/{record['id']}/sources"
    values = change_payload(client, root, f.shared.id, bearer)
    url = root + "/" + operation
    request_headers = headers(bearer, "source-receipt-" + operation)
    accepted = body(client.post(url, json=values, headers=request_headers))
    if operation == "start-new-session":
        with f.case.database.snapshot_session() as unit:
            created = unit.get_one(m.SessionRecord, accepted["session_id"])
            owned = unit.get_one(m.Project, created.project_id)
            assert owned.workspace_id == u.workspace_id and owned.created_by_user_id == u.alice_id
            assert unit.get_one(m.SessionRecord, record["id"]).revision == record["revision"]
    original = Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == url:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    response = client.post(url, json=values, headers=request_headers)
    assert response.status_code == 403, response.get_json()
    assert response.headers.get("Idempotency-Replayed") is None


def test_start_new_replay_requires_workspace_creation_ceiling(library, monkeypatch):
    f, u = library, library.u
    record = project(u.alice, u.ah, u.workspace_id)
    token, bearer = scoped_token(
        f.case,
        u.alice_id,
        {"app.read", "app.write"},
        workspace=u.workspace_id,
        project=record["project_id"],
    )
    client = f.case.app.test_client()
    root = f"/api/v1/sessions/{record['id']}/sources"
    values = change_payload(client, root, None, bearer)
    request_headers = headers(bearer, "source-new-workspace-ceiling")
    url = root + "/start-new-session"
    accepted = body(client.post(url, json=values, headers=request_headers))
    original = Request.get_json

    def parse_then_remove_workspace(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == url:
            with f.case.database.immediate_session() as unit:
                unit.execute(
                    delete(ApiTokenWorkspaceGrant).where(
                        ApiTokenWorkspaceGrant.token_id == token.id
                    )
                )
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_remove_workspace)
    response = client.post(url, json=values, headers=request_headers)
    assert response.status_code == 404, response.get_json()
    assert response.headers.get("Idempotency-Replayed") is None
    with f.case.database.snapshot_session() as unit:
        assert unit.get_one(m.SessionRecord, accepted["session_id"])
        assert unit.get_one(m.SessionRecord, record["id"]).revision == record["revision"]


def speech_source(f):
    u = f.u
    record = body(
        u.alice.post(
            "/api/v1/sessions",
            json={
                "name": "Shared speech input",
                "workflow_kind": "voiceover",
                "workspace_id": u.workspace_id,
            },
            headers=headers(u.ah, str(uuid4())),
        ),
        201,
    )
    root = f"/api/v1/sessions/{record['id']}"
    body(
        u.alice.post(
            "/api/v1/uploads",
            data={
                "session_id": record["id"],
                "file": (
                    io.BytesIO(b"1\n00:00:01,000 --> 00:00:02,000\nA complete source sentence.\n"),
                    "source.srt",
                ),
            },
            headers=u.ah,
        ),
        201,
    )
    with f.case.database.immediate_session() as unit:
        outcome = unit.get(m.OutcomePlan, record["id"])
        if outcome is None:
            outcome = m.OutcomePlan(session_id=record["id"], value_json={})
            unit.add(outcome)
        outcome.value_json = {**dict(outcome.value_json or {}), "inputs": {"generation": "source"}}
    state = body(u.alice.get(root + "/generation-plan/status"))
    return (
        root,
        record["id"],
        {
            "expected_revision": state["session_revision"],
            "expected_plan_revision_id": state["selected_revision_id"],
            "source_artifact_id": state["current_input"]["artifact_id"],
        },
    )


def test_deterministic_plan_receipt_rechecks_edit_before_replay(library, monkeypatch):
    f, u = library, library.u
    root, sid, values = speech_source(f)
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write"}, workspace=u.workspace_id
    )
    client, url = f.case.app.test_client(), root + "/generation-plan/prepare"
    request_headers = headers(bearer, "speech-plan-current-write")
    accepted = body(client.post(url, json=values, headers=request_headers))
    original = Request.get_json

    def parse_then_reduce(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.path == url:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]
        return value

    monkeypatch.setattr(Request, "get_json", parse_then_reduce)
    assert client.post(url, json=values, headers=request_headers).status_code == 403
    with f.case.database.snapshot_session() as unit:
        plan = unit.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == sid))
        assert plan is not None and plan.active_revision_id == accepted["selected_revision_id"]


def test_llm_plan_receipt_rechecks_run_after_model_resolution(library, monkeypatch):
    f, u = library, library.u
    root, sid, values = speech_source(f)
    with f.case.database.immediate_session() as unit:
        unit.add(
            m.SessionSetting(
                session_id=sid,
                section="text",
                revision=1,
                value_json={"llm_tts_optimization": True},
            )
        )
        outcome = unit.get_one(m.OutcomePlan, sid)
        outcome.value_json = {
            **outcome.value_json,
            "transformations": {"llm_tts_optimization": True},
        }
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.write", "app.run"}, workspace=u.workspace_id
    )
    client, url = f.case.app.test_client(), root + "/generation-plan/prepare"
    request_headers = headers(bearer, "speech-plan-current-run")
    monkeypatch.setattr(
        "pandrator.web.speech_plan_preparation.resolve_speech_model",
        lambda *_a, **_kw: "synthetic/model",
    )
    accepted = body(client.post(url, json=values, headers=request_headers))
    assert accepted["status"] == "queued" and accepted["synthesis_started"] is False

    def resolve_then_reduce(*_args, **_kwargs):
        with f.case.database.immediate_session() as unit:
            unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read", "app.write"]
        return "synthetic/model"

    monkeypatch.setattr(
        "pandrator.web.speech_plan_preparation.resolve_speech_model", resolve_then_reduce
    )
    response = client.post(url, json=values, headers=request_headers)
    assert response.status_code == 403, response.get_json()
    assert response.headers.get("Idempotency-Replayed") is None
    with f.case.database.snapshot_session() as unit:
        assert (
            unit.scalar(select(func.count()).select_from(m.Job).where(m.Job.session_id == sid)) == 1
        )
