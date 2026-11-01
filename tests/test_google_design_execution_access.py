"""Real saved Member execution, credential fences and retained Google creation."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from pandrator.logic import google_tts_catalogue_http, tts_handler
from pandrator.logic.google_voice_scope import google_voice_scope
from pandrator.web import google_voice_design as design
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.voice_sample_execution_policy import (
    require_voice_sample_execution_workspace,
)
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.credentials import database_reference, tts_service_credential_key
from pandrator.web.voice_provider_effects import (
    VoiceProviderEffect,
    VoiceProviderEffectError,
    binding_fingerprint,
    voice_provider_effect_summary,
)
from pandrator.web.voice_provider_settings import capture_voice_provider_settings
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_voice_lifecycle_idempotency import silent_wav
from tests.test_voice_sample_execution_access import claimed, job, run
from tests.test_voice_sample_execution_access import sample_case as sample_case  # noqa: F401

MODEL = "gemini-3.8-flash-tts"
KIND = "voice.google_design"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Google design execution acceptance must remain offline")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(
        "google.auth.default",
        lambda **_kwargs: pytest.fail("Named work must never discover host ADC"),
    )


@pytest.fixture
def google_case(sample_case, monkeypatch):
    f = sample_case
    f.calls.clear()
    f.cancel = threading.Event()
    hook: dict[str, Any] = {"before": None, "during": None, "error": None, "sample": None}
    f.hook = hook
    f.google_service_id = "gemini"
    f.google_credential = "synthetic-member-google-key"
    f.auth_calls = []

    def create(_endpoint, *, before_io, **_options):
        if f.hook["before"]:
            f.hook["before"]()
        before_io()
        f.calls.append("create")
        if f.hook["during"]:
            f.hook["during"]()
        if f.hook["error"]:
            raise f.hook["error"]
        return {
            "id": "voice_google_test",
            "usage": {"total_tokens": 9},
            "sample_audio": f.hook["sample"],
        }

    f.hook["sample"] = {"data": base64.b64encode(silent_wav()).decode(), "mime_type": "audio/wav"}
    monkeypatch.setattr(google_tts_catalogue_http, "create_google_voice", create)
    configure(f)
    return f


def configure(f, *, vertex=False, credential=None, enabled=True):
    f.google_service_id = "vertex_ai" if vertex else "gemini"
    if credential is None:
        credential = (
            json.dumps(
                {
                    "project_id": "same-project",
                    "private_key": "synthetic-only",
                    "client_email": "member@example.invalid",
                }
            )
            if vertex
            else "synthetic-member-google-key"
        )
    f.google_credential = credential
    key = tts_service_credential_key(f.google_service_id)
    with f.case.database.immediate_session() as session:
        setting = session.get(m.WorkspaceSetting, (f.users.workspace_id, "services.tts"))
        if setting is None:
            setting = m.WorkspaceSetting(
                workspace_id=f.users.workspace_id, key="services.tts", revision=1
            )
            session.add(setting)
        else:
            setting.revision += 1
        setting.value_json = {
            "provider_configs": [
                {
                    "id": f.google_service_id,
                    "enabled": enabled,
                    "models": [MODEL],
                    "api_base": "https://aiplatform.googleapis.com"
                    if vertex
                    else tts_handler.GEMINI_AUDIO_BASE_URL,
                    "secret_ref": database_reference(key),
                    "vertex_project": "same-project" if vertex else "",
                    "_google_project": "poison-host-project",
                }
            ]
        }
        stored = session.get(m.StoredCredential, (f.users.workspace_id, key))
        if stored is None:
            session.add(
                m.StoredCredential(
                    workspace_id=f.users.workspace_id,
                    key=key,
                    label="Synthetic Google fixture",
                    secret_value=credential,
                )
            )
        else:
            stored.secret_value = credential
            stored.revision += 1


def queue(f):
    service, credential = design.configured_google_design_service(
        f.case.database, f.services.paths, f.google_service_id, workspace_id=f.users.workspace_id
    )
    scope = google_voice_scope(f.google_service_id, service, credential, allow_host_adc=False)
    with execution_actor_scope(f.actor), f.case.database.immediate_session() as session:
        voice = session.get_one(m.Voice, f.voice["id"])
        voice.description = "Synthetic bounded design prompt"
        data = {
            "workspace_id": f.users.workspace_id,
            "voice_id": voice.id,
            "service_id": f.google_service_id,
            "model": MODEL,
            "name": voice.name,
            "prompt": voice.description,
            "language": "en",
            "project": scope["project"],
            "scope_fingerprint": scope["fingerprint"],
            "google_credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
            **capture_voice_provider_settings(session, f.users.workspace_id),
        }
        queued = f.services.jobs.enqueue_in_session(
            session, KIND, data, workspace_id=f.users.workspace_id
        )
        queued.payload_json = {**data, "job_id": queued.id}
        voice.metadata_json = {
            "keep": "unrelated",
            "google_voice_design": {
                "state": "queued",
                "job_id": queued.id,
                "service_id": f.google_service_id,
                "model": MODEL,
                "scope_fingerprint": scope["fingerprint"],
            },
        }
        session.flush()
        identifier = queued.id
    f.queued_id = identifier
    return job(f, identifier)


def call(f, data, progress=lambda *_args: None):
    return f.services.workflow_handlers.design_google_voice(data, progress, f.cancel)


def voice(f):
    with f.case.database.snapshot_session() as session:
        row = session.get(m.Voice, f.voice["id"])
        return deepcopy(row.metadata_json) if row else None


def test_member_registered_worker_creates_once_and_atomically_saves_same_response_audition(
    google_case,
):
    f = google_case
    queued = queue(f)
    run(f)
    saved = job(f, queued.id)
    assert saved.status == "succeeded", saved.error_message
    assert saved.external_effect_json["handler_result"] == saved.result_json
    assert saved.external_effect_json["binding_state"] == "ready"
    metadata = voice(f)
    assert metadata is not None and metadata["keep"] == "unrelated"
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.JobActorRecord, queued.id).user_id == f.users.alice_id
        artifact = session.get_one(m.Artifact, saved.result_json["artifact_id"])
        assert artifact.workspace_id == f.users.workspace_id
        assert f.services.paths.managed_path(artifact.relative_path).read_bytes() == silent_wav()
    assert f.calls == ["create"]
    summary = voice_provider_effect_summary(saved.external_effect_json)
    assert summary is not None
    assert set(summary) == {
        "operation",
        "state",
        "service_id",
        "provider_voice_id",
        "binding_state",
    }


@pytest.mark.parametrize("loss", ["membership", "lease", "settings"])
def test_current_proof_loss_before_create_post_stops_dispatch(google_case, loss):
    f = google_case
    queued = queue(f)

    def change():
        with f.case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(
                    session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
                )
            elif loss == "lease":
                session.get_one(m.Job, queued.id).lease_expires_at = m.utcnow() - timedelta(
                    seconds=1
                )
            else:
                session.get_one(
                    m.WorkspaceSetting, (f.users.workspace_id, "services.tts")
                ).revision += 1

    f.hook["before"] = change
    with claimed(f, queued) as (data, _lease), pytest.raises((AccessDenied, ValueError)):
        call(f, data)
    assert not f.calls
    assert job(f, queued.id).external_effect_json["state"] == "prepared"


@pytest.mark.parametrize(
    "loss", ["membership", "lease", "settings", "binding", "design", "voice", "archive"]
)
def test_remote_identity_survives_response_loss_without_binding_publication(google_case, loss):
    f = google_case
    queued = queue(f)

    def change():
        with f.case.database.immediate_session() as session:
            current = session.get(m.Voice, f.voice["id"])
            if loss == "membership":
                session.delete(
                    session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
                )
            elif loss == "lease":
                row = session.get_one(m.Job, queued.id)
                row.lease_generation += 1
                row.lease_owner = "replacement-worker"
                row.result_json = {"new": "canonical"}
            elif loss == "settings":
                session.get_one(
                    m.WorkspaceSetting, (f.users.workspace_id, "services.tts")
                ).revision += 1
            elif loss == "binding":
                current.metadata_json = {
                    **current.metadata_json,
                    "providers": {"gemini": {"voice_id": "newer-remote"}},
                }
            elif loss == "design":
                current.metadata_json = {
                    **current.metadata_json,
                    "google_voice_design": {"job_id": "newer-design", "state": "queued"},
                }
            elif loss == "voice":
                session.delete(current)
            else:
                session.get_one(m.Workspace, f.users.workspace_id).state = "archived"

    f.hook["during"] = change
    with (
        claimed(f, queued) as (data, _lease),
        pytest.raises(VoiceProviderEffectError, match="local_not_applied"),
    ):
        call(f, data)
    saved = job(f, queued.id)
    assert saved.external_effect_json["state"] == "completed"
    assert saved.external_effect_json["provider_voice_id"] == "voice_google_test"
    assert saved.external_effect_json["binding_state"] == "not_applied"
    assert saved.status == "running"
    assert saved.result_json == ({"new": "canonical"} if loss == "lease" else None)
    metadata = voice(f)
    if metadata:
        assert (metadata.get("providers") or {}) == (
            {"gemini": {"voice_id": "newer-remote"}} if loss == "binding" else {}
        )
    assert f.calls == ["create"]


@pytest.mark.parametrize("outcome", ["completed", "rejected", "uncertain"])
def test_retained_terminal_retry_precedes_missing_voice_and_never_repeats(google_case, outcome):
    f = google_case
    queued = queue(f)
    if outcome == "rejected":
        f.hook["error"] = google_tts_catalogue_http.GoogleVoiceCreationStatusError(429)
    elif outcome == "uncertain":
        f.hook["error"] = RuntimeError("Synthetic unknown provider failure")
    first = None
    with claimed(f, queued) as (data, _lease):
        if outcome == "completed":
            first = call(f, data)
        else:
            with pytest.raises(RuntimeError):
                call(f, data)
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.Voice, f.voice["id"]))
        if outcome == "completed":
            assert first is not None
            assert call(f, data) == first
        else:
            with pytest.raises(VoiceProviderEffectError, match=outcome):
                call(f, data)
        if outcome == "rejected":
            effect = VoiceProviderEffect.current(
                f.case.database, KIND, f.users.workspace_id, f.voice["id"], "gemini"
            )
            effect.failed(job(f, queued.id).external_effect_json["token"])
            assert job(f, queued.id).external_effect_json["state"] == "rejected"
    assert f.calls == ["create"]


@pytest.mark.parametrize("preview", ["invalid", "cancelled"])
def test_preview_failure_retains_created_identity_and_saved_warning_result(google_case, preview):
    f = google_case
    queued = queue(f)
    if preview == "invalid":
        f.hook["sample"] = {"data": "invalid", "mime_type": "audio/wav"}
    else:
        f.hook["during"] = f.cancel.set
    with claimed(f, queued) as (data, _lease):
        result = call(f, data)
        assert result["warning"] == design._PREVIEW_WARNING and "artifact_id" not in result
        assert call(f, data) == result
    saved = job(f, queued.id)
    assert saved.external_effect_json["handler_result"] == result
    metadata = voice(f)
    assert metadata is not None and metadata["google_voice_design"]["state"] == "created"
    assert f.calls == ["create"]


def test_preview_authority_loss_cleans_only_owned_attempt_and_keeps_identity(
    google_case, monkeypatch
):
    f = google_case
    queued = queue(f)
    retained = f.services.paths.artifacts / "google-voice-design" / "retained.wav"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"Prior unrelated attempt")
    prepare = f.services.workflow_handlers.artifacts.prepare_registration
    attempts = []

    def lose_authority(path, **kwargs):
        result = prepare(path, **kwargs)
        attempts.append(path)
        with f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
            )
        return result

    monkeypatch.setattr(
        f.services.workflow_handlers.artifacts, "prepare_registration", lose_authority
    )
    with claimed(f, queued) as (data, _lease), pytest.raises(AccessDenied):
        call(f, data)
    assert (
        attempts
        and not attempts[0].exists()
        and retained.read_bytes() == b"Prior unrelated attempt"
    )
    metadata = voice(f)
    assert metadata is not None and metadata["google_voice_design"]["state"] == "created"
    assert job(f, queued.id).external_effect_json["binding_state"] == "ready"
    with f.case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.Artifact)
                .where(m.Artifact.role == "tts_voice_preview")
            )
            == 0
        )


def mock_vertex_auth(f, monkeypatch, *, rotate=False):
    class Credentials:
        valid = False
        token = None

        def refresh(self, request):
            assert job(f, f.queued_id).external_effect_json is None
            request("https://synthetic.invalid/token")
            self.valid, self.token = True, "synthetic-access-token"

    def request(*_args, **_kwargs):
        f.auth_calls.append("request")
        if rotate:
            with f.case.database.immediate_session() as session:
                stored = session.get_one(
                    m.StoredCredential,
                    (f.users.workspace_id, tts_service_credential_key("vertex_ai")),
                )
                stored.secret_value = json.dumps(
                    {"project_id": "same-project", "private_key": "rotated-synthetic-only"}
                )

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        lambda *_args, **_kwargs: Credentials(),
    )
    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: request)


@pytest.mark.parametrize("rotate", [False, True])
def test_vertex_explicit_workspace_json_refresh_is_guarded_and_rotation_fences_creation(
    google_case, monkeypatch, rotate
):
    f = google_case
    configure(f, vertex=True)
    queued = queue(f)
    mock_vertex_auth(f, monkeypatch, rotate=rotate)
    run(f)
    saved = job(f, queued.id)
    assert saved.status == ("failed" if rotate else "succeeded"), saved.error_message
    assert f.auth_calls == ["request"]
    assert f.calls == ([] if rotate else ["create"])
    if rotate:
        assert saved.external_effect_json is None


def test_prepared_creating_takeover_rotates_token_and_old_callback_cannot_dispatch(google_case):
    f = google_case
    queued = queue(f)
    with claimed(f, queued) as (data, lease):

        def prove(session):
            return require_voice_sample_execution_workspace(
                session, data, KIND, voice_id=f.voice["id"]
            )

        old = VoiceProviderEffect.current(
            f.case.database, KIND, f.users.workspace_id, f.voice["id"], "gemini"
        )
        with f.case.database.immediate_session() as session:
            current = session.get_one(m.Voice, f.voice["id"])
            token = old.reserve(
                {"model": MODEL}, binding_fingerprint(current, "gemini"), prove, session=session
            )
            current.metadata_json = {
                **current.metadata_json,
                "google_voice_design": {
                    **current.metadata_json["google_voice_design"],
                    "state": "creating",
                },
            }
            session.get_one(m.Job, queued.id).lease_generation += 1
        lease["lease_generation"] += 1
        data["_lease_generation"] += 1

        def old_callback():
            with pytest.raises(VoiceProviderEffectError, match="superseded"):
                old.dispatch(token, prove)

        f.hook["before"] = old_callback
        result = call(f, data)
        assert result["provider_voice_id"] == "voice_google_test"
        saved = job(f, queued.id).external_effect_json
        assert saved["token"] != token and saved["lease_generation"] == lease["lease_generation"]
    assert f.calls == ["create"]


def test_direct_calls_need_explicit_host_and_have_no_job_receipt(google_case):
    f = google_case
    queued = queue(f)
    for actor in (None, f.actor):
        with execution_actor_scope(actor), pytest.raises(AccessDenied):
            call(f, queued.payload_json)
    with execution_actor_scope(HostActor("pandrator-cli")):
        first = call(f, queued.payload_json)
        assert call(f, queued.payload_json) == first
    assert f.calls == ["create"] and job(f, queued.id).external_effect_json is None


def test_direct_host_without_root_infers_team_voice_namespace_only_for_trusted_host(google_case):
    f = google_case
    queued = queue(f)
    data = {key: value for key, value in queued.payload_json.items() if key != "workspace_id"}
    for actor in (None, f.actor):
        with execution_actor_scope(actor), pytest.raises(AccessDenied):
            call(f, data)
    with execution_actor_scope(HostActor("pandrator-cli")):
        first = call(f, data)
        assert call(f, {**data, "workspace_id": None}) == first
        for malformed in ("", {}, False, 0):
            with pytest.raises(ValueError, match="workspace"):
                call(f, {**data, "workspace_id": malformed})
    with f.case.database.snapshot_session() as session:
        artifact = session.get_one(m.Artifact, first["artifact_id"])
        assert artifact.workspace_id == f.users.workspace_id
    assert f.calls == ["create"] and job(f, queued.id).external_effect_json is None


def test_concurrent_direct_hosts_have_one_creation_winner_and_replay_without_second_post(
    google_case,
    monkeypatch,
):
    f = google_case
    queued = queue(f)
    configured = design.configured_google_design_service
    rendezvous = threading.Barrier(2)
    first_resolution = threading.local()
    response_release = threading.Event()
    lock = threading.Lock()
    results = []
    errors = []

    def synchronized_configuration(*args, **kwargs):
        result = configured(*args, **kwargs)
        if not getattr(first_resolution, "seen", False):
            first_resolution.seen = True
            # Both calls already observed queued before either can reserve.
            rendezvous.wait(timeout=10)
        return result

    def create(_endpoint, *, before_io, **_options):
        before_io()
        with lock:
            f.calls.append("create")
            if len(f.calls) > 1:
                response_release.set()
        # Keep the winning Voice in creating until the loser hits the writer.
        # The old two-POST behavior releases this too, then fails the count.
        assert response_release.wait(timeout=10), "Other Host call did not finish its race"
        return {"id": "voice_google_test", "sample_audio": f.hook["sample"]}

    def run_host():
        try:
            with execution_actor_scope(HostActor("pandrator-cli")):
                result = call(f, deepcopy(queued.payload_json))
            with lock:
                results.append(result)
        except Exception as error:
            with lock:
                errors.append(error)
        finally:
            response_release.set()

    monkeypatch.setattr(design, "configured_google_design_service", synchronized_configuration)
    monkeypatch.setattr(google_tts_catalogue_http, "create_google_voice", create)
    threads = [threading.Thread(target=run_host, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads), "Host race exceeded its bounded joins"
    assert f.calls == ["create"] and len(results) == len(errors) == 1
    assert type(errors[0]) is ValueError and str(errors[0]) == design._CREATING_WARNING
    metadata = voice(f)
    assert metadata is not None
    assert metadata["providers"]["gemini"]["status"] == "ready"
    assert metadata["providers"]["gemini"]["voice_id"] == results[0]["provider_voice_id"]
    assert job(f, queued.id).external_effect_json is None
    with execution_actor_scope(HostActor("pandrator-cli")):
        assert call(f, queued.payload_json) == results[0]
    assert f.calls == ["create"]


def test_nonlegacy_service_requires_saved_enabled_record_and_never_host_env(
    google_case, monkeypatch
):
    f = google_case
    configure(f, enabled=False)
    monkeypatch.setenv("GEMINI_API_KEY", "poison-host-key")
    with pytest.raises(ValueError, match="enabled"):
        design.configured_google_design_service(
            f.case.database, f.services.paths, "gemini", workspace_id=f.users.workspace_id
        )
    with f.case.database.immediate_session() as session:
        session.get_one(m.WorkspaceSetting, (f.users.workspace_id, "services.tts")).value_json = {}
    with pytest.raises(ValueError, match="configured"):
        design.configured_google_design_service(
            f.case.database, f.services.paths, "gemini", workspace_id=f.users.workspace_id
        )
