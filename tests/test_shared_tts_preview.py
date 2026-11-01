"""Actual workspace admission and saved Member execution for ordinary auditions."""

from __future__ import annotations

import base64
import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import requests
from sqlalchemy import func, select

from pandrator.logic import tts_handler
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.credentials import tts_service_credential_key, upsert_credential
from pandrator.web.jobs import Worker
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import AuditionVoiceInput
from pandrator_mcp.tools.voice_lifecycle import audition_voice
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import (
    MODEL,
    SECRET,
    configure,
)
from tests.test_google_design_workspace_http import (
    case as case,  # noqa: F401
)
from tests.test_google_design_workspace_http import (
    design as design,  # noqa: F401
)
from tests.test_google_design_workspace_http import (
    no_network as no_network,  # noqa: F401
)
from tests.test_google_tts_audio import audio_payload
from tests.test_provider_workspace_access import scoped_token
from tests.test_settings_workspace_access import put
from tests.test_voice_lifecycle_idempotency import silent_wav
from tests.test_voice_sample_execution_access import api_actor

PATH = "/api/v1/services/tts/gemini/preview"
REQUEST = {"text": "A shared workshop audition.", "model": MODEL, "voice": "voice_workshop"}


def submit(f, *, client=None, csrf=None, workspace=None, key="workspace-preview-1", value=None):
    query = {} if workspace is None else {"workspace_id": workspace}
    return (client or f.users.alice).post(
        PATH, query_string=query, json=value or REQUEST,
        headers=headers(csrf or f.users.ah, key),
    )


def counts(f):
    with f.case.database.snapshot_session() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.Artifact, m.UsageEvent, m.ApiIdempotency))


def worker(f):
    handlers = WorkflowHandlers(f.case.database, f.services.paths, jobs=f.services.jobs)
    return handlers, Worker(f.services.jobs, "workspace-preview-worker", handlers.handlers())


def fake_post(monkeypatch, *, after_send=None):
    calls = []

    def post(url, **options):
        assert url == "https://generativelanguage.googleapis.com/v1beta/interactions"
        assert options["headers"]["x-goog-api-key"] == SECRET
        assert options["allow_redirects"] is False
        calls.append((url, options))
        if after_send:
            after_send()
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(audio_payload(
            True, base64.b64encode(silent_wav()).decode(), mime_type="audio/wav"
        )).encode()
        return response

    monkeypatch.setattr("pandrator.logic.native_speech_http.requests.post", post)
    monkeypatch.setattr(tts_handler, "_get_litellm_speech_client",
        lambda: pytest.fail("Scoped preview reached an opaque SDK"))
    return calls


def test_member_registered_worker_saves_shared_audio_and_usage_without_connections(design, monkeypatch):
    f = design
    monkeypatch.setenv("GEMINI_API_KEY", "forbidden-host-google-key")
    queued = body(submit(f, workspace=f.users.workspace_id), 202)
    assert queued["workspace_id"] == f.users.workspace_id
    assert queued["payload_json"]["settings"]["model"] == MODEL
    assert "provider_configs" not in queued["payload_json"]["settings"]
    calls = fake_post(monkeypatch)
    _handlers, run = worker(f)
    assert run.run_once()
    with f.case.database.snapshot_session() as session:
        job = session.get_one(m.Job, queued["id"])
        assert job.status == "succeeded", job.error_message
        assert job.max_attempts == 1
        assert session.get_one(m.JobActorRecord, job.id).user_id == f.users.alice_id
        artifact = session.get_one(m.Artifact, job.result_json["artifact_id"])
        usage = session.scalars(select(m.UsageEvent).where(m.UsageEvent.job_id == job.id)).one()
        assert job.workspace_id == artifact.workspace_id == usage.workspace_id == f.users.workspace_id
        assert job.project_id is artifact.project_id is usage.project_id is None
        assert f.services.paths.managed_path(artifact.relative_path).read_bytes() == silent_wav()
        metadata = json.dumps(artifact.metadata_json)
        assert SECRET not in metadata and "api_base" not in metadata and "secret_ref" not in metadata
    assert len(calls) == 1
    read = body(f.users.bob.get(f"/api/v1/jobs/{queued['id']}"))
    assert "provider_configs" not in read["payload_json"]["settings"]
    assert "api_base" not in json.dumps(read) and SECRET not in json.dumps(read)
    work = body(f.users.bob.get(f"/api/v1/work/{queued['id']}"))
    assert "payload_json" not in work and SECRET not in json.dumps(work)


def test_same_retry_key_is_isolated_by_workspace_and_user_and_requires_current_access(design, monkeypatch):
    f = design
    own = configure(f.users.alice, f.users.ah)
    first = body(submit(f, workspace=f.users.workspace_id), 202)
    home = body(submit(f), 202)
    peer = body(submit(f, client=f.users.bob, csrf=f.users.bh, workspace=f.users.workspace_id), 202)
    assert len({first["id"], home["id"], peer["id"]}) == 3
    assert home["workspace_id"] == own["workspace_id"] != f.users.workspace_id
    monkeypatch.setattr(f.services.tts_catalogue, "preview_settings",
        lambda *_a, **_k: pytest.fail("Exact retry read mutable configuration"))
    assert body(submit(f, workspace=f.users.workspace_id), 202) == first
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
    assert submit(f, workspace=f.users.workspace_id).status_code == 404


def test_scope_and_read_write_run_denials_precede_settings_replay_and_queue(design, monkeypatch):
    f = design
    private = configure(f.users.bob, f.users.bh)
    before = counts(f)
    monkeypatch.setattr(f.services.tts_catalogue, "preview_settings",
        lambda *_a, **_k: pytest.fail("Denied preview read protected settings"))
    for client, csrf in ((f.users.alice, f.users.ah), (f.users.owner, f.users.oh)):
        assert submit(f, client=client, csrf=csrf, workspace=private["workspace_id"]).status_code == 404
    for scopes in ({"app.read"}, {"app.read", "app.write"}, {"app.read", "app.run"}):
        _token, bearer = scoped_token(f.case, f.users.alice_id, scopes, workspace=f.users.workspace_id)
        assert submit(f, csrf=bearer, workspace=f.users.workspace_id).status_code == 403
    for query in ({"workspace_id": " "}, {"workspace_id": "w" * 81}, {"session_id": "project"}):
        response = f.users.alice.post(PATH, query_string=query, json=REQUEST, headers=f.users.ah)
        assert response.status_code == 422
    assert counts(f) == before


@pytest.mark.parametrize("loss", ["membership", "settings", "credential"])
def test_actual_send_denies_late_changes_after_request_options_are_built(design, monkeypatch, loss):
    f = design
    queued = body(submit(f, workspace=f.users.workspace_id), 202)
    calls = fake_post(monkeypatch)
    original = tts_handler._resolve_openai_audio_api_key

    def resolve_then_change(endpoint):
        key = original(endpoint)
        with f.case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
            elif loss == "settings":
                session.get_one(m.WorkspaceSetting, (f.users.workspace_id, "services.tts")).revision += 1
            else:
                upsert_credential(session, tts_service_credential_key("gemini"), "Synthetic rotated",
                    "synthetic-rotated-workspace-key", workspace_id=f.users.workspace_id)
        return key

    monkeypatch.setattr(tts_handler, "_resolve_openai_audio_api_key", resolve_then_change)
    _handlers, run = worker(f)
    assert run.run_once()
    assert not calls
    assert f.services.jobs.get(queued["id"]).status == "failed"
    assert counts(f)[1:3] == (0, 0)


@pytest.mark.parametrize("loss", ["membership", "lease"])
def test_late_response_cannot_publish_audio_or_usage_after_losing_authority(design, monkeypatch, loss):
    f = design
    queued = body(submit(f, workspace=f.users.workspace_id), 202)

    def change():
        with f.case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
            else:
                job = session.get_one(m.Job, queued["id"])
                job.lease_generation += 1
                job.lease_owner = "replacement-worker"
                job.lease_expires_at = m.utcnow() + timedelta(minutes=10)
                job.result_json = {"newer": "canonical"}

    calls = fake_post(monkeypatch, after_send=change)
    _handlers, run = worker(f)
    assert run.run_once()
    assert len(calls) == 1 and counts(f)[1:3] == (0, 0)
    if loss == "lease":
        assert f.services.jobs.get(queued["id"]).result_json == {"newer": "canonical"}


def test_file_preparation_loss_rolls_back_artifact_usage_and_only_cleans_own_attempt(design, monkeypatch):
    f = design
    queued = body(submit(f, workspace=f.users.workspace_id), 202)
    fake_post(monkeypatch)
    handlers, run = worker(f)
    prepare = handlers.artifacts.prepare_registration

    def prepare_then_revoke(*args, **kwargs):
        result = prepare(*args, **kwargs)
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
        return result

    monkeypatch.setattr(handlers.artifacts, "prepare_registration", prepare_then_revoke)
    assert run.run_once() and f.services.jobs.get(queued["id"]).status == "failed"
    assert counts(f)[1:3] == (0, 0)
    assert not list((f.services.paths.artifacts / "tts-previews" / f.users.workspace_id).glob("*.wav"))


@pytest.mark.parametrize("named_user", [False, True])
def test_unleased_user_or_missing_actor_fails_before_hydration(design, monkeypatch, named_user):
    f = design
    handlers, _run = worker(f)
    monkeypatch.setattr("pandrator.web.workflow_handlers.hydrate_tts_settings",
        lambda *_a, **_k: pytest.fail("Missing execution authority reached a credential"))
    actor = api_actor(f.case, f.users.alice_id, f.users.workspace_id)[0] if named_user else None
    with execution_actor_scope(actor), pytest.raises(AccessDenied):
        handlers.preview_tts_voice({"text": "No actor", "workspace_id": f.users.workspace_id},
            lambda *_a: None, threading.Event())


def test_real_mcp_request_builder_reaches_workspace_admission_with_safe_outcome(design):
    f = design

    class LocalClient(ApplicationVoiceMethods):
        def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
            return body(f.users.alice.open(path, method=kwargs["method"],
                query_string=kwargs.get("parameters"), json=kwargs["body"],
                headers=headers(f.users.ah, kwargs["idempotency_key"])), 202)

        def _request_binary_json(self, path: str, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"Unexpected binary request: {path}")

    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: LocalClient()))
    result = audition_voice(runtime, AuditionVoiceInput(service_id="gemini", model=MODEL,
        voice="voice_workshop", text="MCP workspace audition.", workspace_id=f.users.workspace_id,
        idempotency_key="workspace-mcp-audition"))
    assert result.work is not None and result.result["workspace_id"] == f.users.workspace_id
    assert "payload_json" not in result.result and SECRET not in json.dumps(result.result)


@pytest.mark.parametrize("change", ["disabled", "model"])
def test_preview_cannot_bypass_the_passive_workspace_approval_policy(design, change):
    f = design
    if change == "disabled":
        body(put(f.users.owner, f.users.oh, "services.tts", {"provider_configs": [
            {"id": "gemini", "enabled": False, "models": [MODEL]}
        ]}, revision=1, workspace=f.users.workspace_id))
    before = counts(f)
    response = submit(f, workspace=f.users.workspace_id,
        value={**REQUEST, "model": "unapproved-model"} if change == "model" else None)
    assert response.status_code == (404 if change == "disabled" else 422)
    assert counts(f) == before


def test_member_google_catalogue_treats_omitted_scope_observation_as_unknown(design, monkeypatch):
    f = design
    with f.case.database.immediate_session() as session:
        voice = m.Voice(workspace_id=f.users.workspace_id, name="Saved workshop design",
            metadata_json={"providers": {"gemini": {"status": "ready",
                "resource_kind": "designed_voice", "voice_id": "voice_workshop"}}})
        session.add(voice)
        session.flush()
        voice_id = voice.id
    monkeypatch.setattr("pandrator.web.tts_catalogue_service.resolve_provider_credentials",
        lambda *_a, **_k: pytest.fail("Member catalogue resolved private credentials"))
    page = body(f.users.alice.get("/api/v1/voice-catalog", query_string={
        "workspace_id": f.users.workspace_id, "kind": "managed", "service_id": "gemini",
        "model": MODEL, "query": voice_id}))
    selected = next(item for item in page["items"] if item["id"] == voice_id)
    binding = selected["compatibility"][0]
    assert binding["status"] == "unknown" and binding["prepared"] and not binding["ready"]


def test_rotated_credential_cannot_escape_through_provider_errors_or_job_logs(design, monkeypatch):
    f = design
    f.services.jobs.secret_redactor.values()
    rotated = "rotated-workspace-value-not-in-the-old-cache"
    with f.case.database.immediate_session() as session:
        upsert_credential(session, tts_service_credential_key("gemini"), "Synthetic rotated",
            rotated, workspace_id=f.users.workspace_id)
    queued = body(submit(f, workspace=f.users.workspace_id), 202)

    def reject(_url, **options):
        assert options["headers"]["x-goog-api-key"] == rotated
        response = requests.Response()
        response.status_code = 400
        response._content = json.dumps({"error": {"message": "Echo: " + rotated}}).encode()
        return response

    monkeypatch.setattr("pandrator.logic.native_speech_http.requests.post", reject)
    _handlers, run = worker(f)
    assert run.run_once()
    for path in (f"/api/v1/jobs/{queued['id']}", f"/api/v1/jobs/{queued['id']}/logs"):
        result = body(f.users.bob.get(path))
        assert rotated not in json.dumps(result)


@pytest.mark.parametrize("present", [True, False])
def test_qwen_preparation_uses_workspace_key_without_an_implicit_publish(design, monkeypatch, present):
    f = design
    body(put(f.users.owner, f.users.oh, "services.tts", {"provider_configs": [{
        "id": "kobold_qwen", "enabled": True, "models": ["Voice Cloning"],
        "default_model": "Voice Cloning", "api_key": SECRET,
        "credential_backend": "database", "connection_mode": "external", "api_base": "https://qwen.invalid",
    }]}, revision=1, workspace=f.users.workspace_id))
    with f.case.database.immediate_session() as session:
        session.add(m.Voice(workspace_id=f.users.workspace_id, name="Workshop Qwen",
            metadata_json={"providers": {"kobold_qwen": {"status": "ready", "voice_id": "workshop-ref"}}}))
    queued = body(f.users.alice.post("/api/v1/services/tts/kobold_qwen/preview",
        query_string={"workspace_id": f.users.workspace_id}, headers=headers(f.users.ah, "qwen-preparation"),
        json={"text": "Reference audition.", "model": "Voice Cloning", "voice": "workshop-ref", "language": "en"}), 202)
    calls = []
    monkeypatch.setenv("KOBOLD_QWEN_API_KEY", "forbidden-host-qwen-key")

    def get(url, **options):
        assert options["headers"]["Authorization"] == f"Bearer {SECRET}"
        assert options["allow_redirects"] is False
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"voices": [{"id": "workshop-ref", "type": "cloned"}] if present else []}).encode()
        return response

    monkeypatch.setattr("pandrator.logic.kobold_qwen_http.requests.get", get)
    handlers, run = worker(f)
    monkeypatch.setattr(handlers, "publish_voice", lambda *_a, **_k: pytest.fail("Preview implicitly published another provider resource"))
    # Only the preparation boundary is under test; acquisition/inference is a later R3 gate.
    from pydub import AudioSegment
    monkeypatch.setattr(handlers.tts_providers, "synthesize", lambda *_a, **_k: AudioSegment.silent(25))
    assert run.run_once() and calls
    result = f.services.jobs.get(queued["id"])
    assert result.status == ("succeeded" if present else "failed"), result.error_message
    if not present:
        assert "Prepare it in the Voice Library" in (result.error_message or "")


@pytest.mark.parametrize("provider,state", [
    ("kobold_qwen", "installed"), ("kobold_qwen", "missing"),
    ("kobold_qwen", "old_provider"), ("xtts", "installed"), ("xtts", "missing"),
])
def test_member_registered_worker_uses_prepared_models_without_acquisition(design, monkeypatch, provider, state):
    from pandrator.logic.tts_retry import TtsRetryGate

    f = design
    model = "Prebuilt Voices" if provider == "kobold_qwen" else "xtts_v2"
    profile = {"id": provider, "enabled": True, "models": [model], "default_model": model,
        "connection_mode": "external", "api_base": "https://prepared.invalid"}
    if provider == "kobold_qwen":
        profile.update(api_key=SECRET, credential_backend="database")
    body(put(f.users.owner, f.users.oh, "services.tts", {"provider_configs": [profile]},
        revision=1, workspace=f.users.workspace_id))
    queued = body(f.users.alice.post(f"/api/v1/services/tts/{provider}/preview",
        query_string={"workspace_id": f.users.workspace_id}, headers=headers(f.users.ah, "prepared-inference"),
        json={"text": "Installed model audition.", "model": model,
            "voice": "Ryan" if provider == "kobold_qwen" else "reference", "language": "en"}), 202)
    calls = []

    def post(url, **options):
        assert url in {"https://prepared.invalid/v1/audio/speech/prepared",
            "https://prepared.invalid/audio/speech/prepared"}
        assert options["allow_redirects"] is False
        if provider == "kobold_qwen":
            assert options["headers"]["Authorization"] == f"Bearer {SECRET}"
        calls.append(url)
        response = requests.Response()
        response.url = url
        response.status_code = {"installed": 200, "missing": 409, "old_provider": 404}[state]
        response.headers["Content-Type"] = "audio/wav" if state == "installed" else "application/json"
        response._content = silent_wav() if state == "installed" else json.dumps({
            "detail": {"code": "model_acquisition_required" if state == "missing" else "not_found"}
        }).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("Inference probed a catalogue"))
    monkeypatch.setattr(tts_handler, "_TTS_RETRY_GATE", TtsRetryGate())
    monkeypatch.setattr(tts_handler, "wait_for_retry", lambda *_a, **_kw: pytest.fail("Retried permanent model state"))
    _handlers, run = worker(f)
    assert run.run_once()
    result = f.services.jobs.get(queued["id"])
    assert len(calls) == (2 if state == "old_provider" else 1)
    assert result.status == ("succeeded" if state == "installed" else "failed"), result.error_message
    # These local providers have no billed usage event; only audio is published.
    assert counts(f)[1:3] == ((1, 0) if state == "installed" else (0, 0))
    if state == "installed":
        with f.case.database.snapshot_session() as session:
            artifact = session.get_one(m.Artifact, result.result_json["artifact_id"])
            assert artifact.workspace_id == result.workspace_id == f.users.workspace_id
            assert f.services.paths.managed_path(artifact.relative_path).read_bytes() == silent_wav()
    if state == "missing":
        assert "Ask an Administrator" in (result.error_message or "")
