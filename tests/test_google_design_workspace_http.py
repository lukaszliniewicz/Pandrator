"""Native design admission, retry and receipt visibility use the proved workspace."""

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from pandrator.web import google_voice_design_routes as routes
from pandrator.web import models as m
from pandrator.web import service_routes
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import DesignGoogleVoiceInput
from pandrator_mcp.tools.voice_lifecycle import design_google_voice
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token
from tests.test_settings_workspace_access import put
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401
from tests.test_voice_provider_job_effects import receipt as effect_receipt

SECRET = "synthetic-google-workspace-key"
MODEL = "gemini-3.8-flash-tts"


def configure(client, csrf, workspace=None, *, service="gemini", credential=SECRET, enabled=True):
    record = {"id": service, "enabled": enabled, "models": [MODEL]}
    if credential:
        record.update(api_key=credential, credential_backend="database")
    return body(put(client, csrf, "services.tts", {"provider_configs": [record]}, workspace=workspace))


def rows(case):
    with case.database.snapshot_session() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (m.Voice, m.Job, m.ApiIdempotency)}


@pytest.fixture
def design(case, monkeypatch):
    users = team(case, monkeypatch)
    configure(users.owner, users.oh, users.workspace_id)
    return SimpleNamespace(case=case, users=users, services=case.app.extensions["pandrator"]["services"])


def submit(f, *, client=None, csrf=None, workspace=None, key="google-workspace-create", name="Workshop Google voice", service="gemini", query=None):
    hints = {} if workspace is None else {"workspace_id": workspace}
    if query:
        hints.update(query)
    return (client or f.users.alice).post("/api/v1/voices/design/google", query_string=hints,
        json={"service_id": service, "model": MODEL, "name": name, "prompt": "Warm, unhurried and clear.",
            "language": "en", "voice_category": "unspecified"}, headers=headers(csrf or f.users.ah, key))


def test_member_team_and_actual_caller_home_are_separate_with_scoped_retry_keys(design):
    f = design
    personal = configure(f.users.alice, f.users.ah)
    shared = body(submit(f, workspace=f.users.workspace_id), 202)
    own = body(submit(f), 202)
    peer = body(submit(f, client=f.users.bob, csrf=f.users.bh,
        workspace=f.users.workspace_id, name="Another workshop voice"), 202)
    assert len({shared["id"], own["id"], peer["id"]}) == 3
    assert shared["workspace_id"] == peer["workspace_id"] == f.users.workspace_id
    assert own["workspace_id"] == personal["workspace_id"] != f.users.workspace_id
    with f.case.database.snapshot_session() as session:
        for result, uid in ((shared, f.users.alice_id), (own, f.users.alice_id), (peer, f.users.bob_id)):
            voice = session.get_one(m.Voice, result["voice_id"])
            job = session.get_one(m.Job, result["id"])
            actor = session.get_one(m.JobActorRecord, job.id)
            assert voice.workspace_id == job.workspace_id == result["workspace_id"]
            assert actor.user_id == uid and job.kind == "voice.google_design" and job.max_attempts == 1
            assert job.project_id is None and job.session_id is None and job.workflow_run_id is None
            assert job.payload_json["google_credential_sha256"] == hashlib.sha256(SECRET.encode()).hexdigest()
            assert job.payload_json["tts_settings_revisions"] == {"services.tts": 1, "defaults.tts": 0}
            assert job.payload_json["job_id"] == job.id
            assert voice.metadata_json["google_voice_design"]["job_id"] == job.id
            assert SECRET not in json.dumps(job.payload_json)
    assert SECRET not in json.dumps(shared)


def test_current_authority_precedes_configuration_resolution_and_no_scope_can_borrow_a_private_home(design, monkeypatch):
    f = design
    private = configure(f.users.bob, f.users.bh)
    before = rows(f.case)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied design request touched credentials or jobs")

    monkeypatch.setattr(routes, "configured_google_design_service", forbidden)
    monkeypatch.setattr(f.services.jobs, "enqueue_in_session", forbidden)
    for client, csrf in ((f.users.alice, f.users.ah), (f.users.owner, f.users.oh)):
        assert submit(f, client=client, csrf=csrf, workspace=private["workspace_id"]).status_code == 404
    for scopes in ({"app.read"}, {"app.read", "app.write"}, {"app.read", "app.run"}):
        _token, bearer = scoped_token(f.case, f.users.alice_id, scopes, workspace=f.users.workspace_id)
        assert submit(f, csrf=bearer, workspace=f.users.workspace_id).status_code == 403
    for query in ({"workspace_id": " "}, {"workspace_id": "w" * 81}, {"session_id": "project-root"}):
        assert submit(f, query=query).status_code == 422
    assert rows(f.case) == before


def test_project_viewing_does_not_grant_workspace_voice_creation(design, monkeypatch):
    f = design
    project = body(f.users.owner.post("/api/v1/sessions", json={"name": "View-only source",
        "workflow_kind": "subtitles"}, headers=f.users.oh), 201)
    viewers = body(f.users.owner.get(f"/api/v1/projects/{project['project_id']}/viewers"))
    body(f.users.owner.put(f"/api/v1/projects/{project['project_id']}/viewers/{f.users.alice_id}",
        json={"expected_revision": viewers["revision"]}, headers=f.users.oh))
    before = rows(f.case)
    monkeypatch.setattr(routes, "configured_google_design_service",
        lambda *_a, **_kw: pytest.fail("Project viewer reached workspace credentials"))
    with f.case.database.snapshot_session() as session:
        owner_workspace = session.scalar(select(m.Project.workspace_id).where(m.Project.id == project["project_id"]))
    assert submit(f, workspace=owner_workspace).status_code == 404
    assert rows(f.case) == before


def test_retry_survives_voice_removal_and_configuration_change_but_not_membership_loss(design, monkeypatch):
    f = design
    first = body(submit(f, workspace=f.users.workspace_id), 202)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Job, first["id"]).status = "failed"
        session.delete(session.get_one(m.Voice, first["voice_id"]))
        session.get_one(m.WorkspaceSetting, (f.users.workspace_id, "services.tts")).revision += 1
    monkeypatch.setattr(routes, "configured_google_design_service",
        lambda *_a, **_kw: pytest.fail("Exact retry resolved mutable credentials"))
    replay = submit(f, workspace=f.users.workspace_id)
    assert body(replay, 202) == first and replay.headers["Idempotency-Replayed"] == "true"
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
    assert submit(f, workspace=f.users.workspace_id).status_code == 404


@pytest.mark.parametrize("loss", ["membership", "settings"])
def test_configuration_preflight_cannot_admit_after_authority_or_revision_changes(design, monkeypatch, loss):
    f = design
    before = rows(f.case)
    resolve = routes.configured_google_design_service

    def resolve_then_change(*args, **kwargs):
        result = resolve(*args, **kwargs)
        with f.case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
            else:
                session.get_one(m.WorkspaceSetting, (f.users.workspace_id, "services.tts")).revision += 1
        return result

    monkeypatch.setattr(routes, "configured_google_design_service", resolve_then_change)
    assert submit(f, workspace=f.users.workspace_id).status_code == (404 if loss == "membership" else 409)
    assert rows(f.case) == before


def test_named_vertex_without_explicit_credential_never_discovers_host_adc(design, monkeypatch):
    f = design
    own = configure(f.users.alice, f.users.ah, service="vertex_ai", credential="")
    before = rows(f.case)
    monkeypatch.setattr("google.auth.default", lambda **_kw: pytest.fail("Named design discovered host ADC"))
    result = submit(f, service="vertex_ai", workspace=own["workspace_id"])
    assert result.status_code == 422
    assert rows(f.case) == before


def test_named_vertex_explicit_service_account_can_queue_without_authentication_io(design, monkeypatch):
    f = design
    credential = json.dumps({"type": "service_account", "project_id": "workspace-project",
        "client_email": "synthetic@example.invalid", "private_key": "synthetic-not-a-key",
        "token_uri": "https://oauth2.example.invalid/token"})
    own = configure(f.users.alice, f.users.ah, service="vertex_ai", credential=credential)
    monkeypatch.setattr("google.auth.default", lambda **_kw: pytest.fail("Explicit workspace JSON reached ADC"))
    queued = body(submit(f, service="vertex_ai", workspace=own["workspace_id"]), 202)
    with f.case.database.snapshot_session() as session:
        job = session.get_one(m.Job, queued["id"])
        assert job.payload_json["project"] == "workspace-project"
        assert job.payload_json["google_credential_sha256"] == hashlib.sha256(credential.encode()).hexdigest()


def test_real_mcp_builder_reaches_scoped_flask_admission_and_safe_job_receipt(design):
    f = design

    class LocalClient(ApplicationVoiceMethods):
        def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
            return body(f.users.alice.open(path, method=kwargs["method"], json=kwargs["body"],
                query_string=kwargs.get("parameters"), headers=headers(f.users.ah, kwargs["idempotency_key"])), 202)

        def _request_binary_json(self, path: str, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"Unexpected binary design request: {path}")

    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: LocalClient()))
    outcome = design_google_voice(runtime, DesignGoogleVoiceInput(service_id="gemini", model=MODEL,
        name="MCP workshop", prompt="Calm and clear.", idempotency_key="mcp-workspace-design",
        workspace_id=f.users.workspace_id))
    assert outcome.result["workspace_id"] == f.users.workspace_id and outcome.work is not None
    job_id = outcome.work.id
    with f.case.database.immediate_session() as session:
        job = session.get_one(m.Job, job_id)
        job.status = "failed"
        job.external_effect_json = {**effect_receipt(), "operation": "voice.google_design",
            "service_id": "gemini", "job_id": job_id, "workspace_id": f.users.workspace_id,
            "voice_id": job.payload_json["voice_id"], "provider_voice_id": "workspace-created-voice"}
    for root in ("jobs", "work"):
        result = body(f.users.alice.get(f"/api/v1/{root}/{job_id}"))
        effect = result["external_effect"]
        assert effect == {"operation": "voice.google_design", "service_id": "gemini",
            "state": "completed", "binding_state": "not_applied", "provider_voice_id": "workspace-created-voice"}
        assert "token" not in effect and "handler_result" not in effect


def test_scoped_preview_uses_the_team_connection_without_a_personal_fallback(design, monkeypatch):
    f = design
    before = rows(f.case)
    monkeypatch.setattr(service_routes, "require_preview_replay",
        lambda *_a, **_kw: pytest.fail("Wrong-root preview reached old receipt"))
    original = f.services.tts_catalogue.preview_settings

    def settings(*args, **kwargs):
        assert kwargs["workspace_id"] == f.users.workspace_id
        return original(*args, **kwargs)

    monkeypatch.setattr(f.services.tts_catalogue, "preview_settings", settings)
    response = f.users.alice.post("/api/v1/services/tts/gemini/preview",
        query_string={"workspace_id": f.users.workspace_id}, json={"text": "Sample"}, headers=f.users.ah)
    result = body(response, 202)
    assert result["workspace_id"] == f.users.workspace_id
    assert rows(f.case)["jobs"] == before["jobs"] + 1
    assert rows(f.case)["voices"] == before["voices"]
