"""Real saved actors/leases and attempt-safe voice sample publication."""

from __future__ import annotations

import json
import shutil
import socket
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web import workflow_voice
from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.voice_sample_execution_policy import (
    require_voice_sample_execution_workspace,
)
from pandrator.web.access.workspace_policy import require_workspace_access
from pandrator.web.accounts.execution_actor import (
    HostActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.auth import Principal
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import Worker
from pandrator.web.voice_sample_transcription import capture_voice_transcription_settings
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token, sql_capture
from tests.test_voice_lifecycle_idempotency import silent_wav


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Voice sample execution is local only")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def api_actor(case, user, wid):
    token, bearer = scoped_token(case, user, {"app.read", "app.write", "app.run"}, workspace=wid)
    services = case.app.extensions["pandrator"]["services"]
    principal = case.auth.resolve_api_token(
        bearer["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=services.identity.snapshot(
            observed_origin="http://localhost"
        ).instance_id,
    )
    assert principal is not None
    return actor_from_principal(principal), token


@pytest.fixture
def sample_case(case, monkeypatch):
    users = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    voice = body(
        users.alice.post(
            "/api/v1/voices",
            query_string={"workspace_id": users.workspace_id},
            json={"name": "Execution reference"},
            headers=users.ah,
        ),
        201,
    )
    path = services.paths.voices / voice["id"] / "original.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(silent_wav())
    source = services.artifacts.register(
        path, kind="audio", role="voice_sample", workspace_id=users.workspace_id
    )
    with case.database.immediate_session() as session:
        sample = m.VoiceSample(
            workspace_id=users.workspace_id, voice_id=voice["id"], artifact_id=source.id
        )
        session.add(sample)
        session.flush()
        sid = sample.id
    actor, token = api_actor(case, users.alice_id, users.workspace_id)
    calls = []
    attempts = []
    hook = {"during": None}

    def ffmpeg(command, **_kwargs):
        calls.append("ffmpeg")
        destination = Path(command[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(command[command.index("-i") + 1]), destination)
        if hook["during"]:
            hook["during"]()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def transcribe(directory, _source, _settings, **_kwargs):
        calls.append("stt")
        directory = Path(directory)
        attempts.append(directory)
        output = directory / "transcript.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic reference\n")
        timing = directory / "words.json"
        timing.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "language": "en",
                    "segments": [
                        {"id": "one", "start_ms": 0, "end_ms": 1000, "text": "Synthetic reference"}
                    ],
                }
            )
        )
        if hook["during"]:
            hook["during"]()
        return SimpleNamespace(
            srt_path=output,
            word_timestamps_path=timing,
            engine="synthetic",
            compute_backend="cpu",
            resolved_language="en",
            routing={},
        )

    monkeypatch.setattr(workflow_voice.subprocess, "run", ffmpeg)
    monkeypatch.setattr(
        "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata", transcribe
    )
    return SimpleNamespace(
        case=case,
        users=users,
        services=services,
        voice=voice,
        path=path,
        source=source,
        sid=sid,
        actor=actor,
        token=token,
        calls=calls,
        attempts=attempts,
        hook=hook,
    )


def payload(fixture, kind):
    data = {"workspace_id": fixture.users.workspace_id, "voice_id": fixture.voice["id"]}
    if kind == "voice.normalize_recording":
        data.update(source_artifact_id=fixture.source.id, expected_voice_revision=1)
    else:
        data.update(sample_id=fixture.sid, sample_artifact_id=fixture.source.id)
        with fixture.case.database.snapshot_session() as session:
            require_workspace_access(
                session, AccessContext(fixture.actor), fixture.users.workspace_id
            )
            data.update(
                capture_voice_transcription_settings(
                    session,
                    fixture.users.workspace_id,
                    {"stt_engine": "whisper", "stt_language": "en"},
                    "en",
                )
            )
    return data


def enqueue(fixture, kind, data=None, actor=None):
    with execution_actor_scope(actor or fixture.actor):
        return fixture.services.jobs.enqueue(
            kind,
            data or payload(fixture, kind),
            workspace_id=fixture.users.workspace_id,
            resource_keys=[f"voice:{fixture.voice['id']}"],
        )


def run(fixture):
    assert Worker(
        fixture.services.jobs, "voice-sample-worker", fixture.services.workflow_handlers.handlers()
    ).run_once()


def job(fixture, identifier):
    with fixture.case.database.snapshot_session() as session:
        row = session.get_one(m.Job, identifier)
        session.expunge(row)
        return row


@contextmanager
def claimed(fixture, queued, *, actor=None):
    row = fixture.services.jobs.claim("voice-policy-worker", lease_seconds=300)
    assert row is not None and row.id == queued.id
    lease: JobLease = {
        "job_id": row.id,
        "owner_id": "voice-policy-worker",
        "lease_generation": row.lease_generation,
    }
    marker = current_job_lease.set(lease)
    try:
        with execution_actor_scope(fixture.actor if actor is None else actor):
            yield (
                {**row.payload_json, "_job_id": row.id, "_lease_generation": row.lease_generation},
                lease,
            )
    finally:
        current_job_lease.reset(marker)


def test_registered_member_worker_normalization_and_transcription_publish_scoped_outputs(
    sample_case,
):
    f = sample_case
    normal = enqueue(f, "voice.normalize_recording")
    run(f)
    assert job(f, normal.id).status == "succeeded"
    transcribe = enqueue(f, "voice.transcribe")
    run(f)
    result = job(f, transcribe.id)
    assert result.status == "succeeded", result.error_message
    with f.case.database.snapshot_session() as session:
        for queued in (normal, transcribe):
            saved = session.get_one(m.JobActorRecord, queued.id)
            assert (
                saved.user_id == f.users.alice_id and saved.auth_revision == f.actor.auth_revision
            )
        assert all(
            row.workspace_id == f.users.workspace_id
            for row in session.scalars(select(m.Artifact)).all()
        )
        assert session.get_one(m.Voice, f.voice["id"]).revision == 2
    assert f.calls == ["ffmpeg", "stt"] and f.attempts[0].is_dir()
    assert f.voice["id"] in str(f.attempts[0]) and f.attempts[0].parent.name == "transcriptions"


def test_policy_denies_wrong_header_payload_lease_and_resource_ceiling_before_private_payload(
    sample_case,
):
    f = sample_case
    root = body(
        f.users.alice.post(
            "/api/v1/sessions",
            json={
                "name": "Header root",
                "workflow_kind": "subtitles",
                "workspace_id": f.users.workspace_id,
            },
            headers=headers(f.users.ah, "sample-header-root"),
        ),
        201,
    )
    rooted_path = f.services.paths.uploads / "rooted-policy.wav"
    rooted_path.write_bytes(silent_wav())
    rooted = f.services.artifacts.register(
        rooted_path, kind="audio", role="upload", session_id=root["id"]
    )
    for change in ("kind", "root", "lease", "payload", "grant"):
        data = payload(f, "voice.normalize_recording")
        if change == "root":
            data["source_artifact_id"] = rooted.id
            with execution_actor_scope(f.actor):
                queued = f.services.jobs.enqueue(
                    "voice.normalize_recording",
                    data,
                    workspace_id=f.users.workspace_id,
                    project_id=root["project_id"],
                    session_id=root["id"],
                )
        else:
            queued = enqueue(f, "voice.normalize_recording", data)
        with claimed(f, queued) as (altered, lease):
            with f.case.database.immediate_session() as session:
                if change == "kind":
                    session.get_one(m.Job, queued.id).kind = "voice.transcribe"
                if change == "grant":
                    session.delete(
                        session.get_one(ApiTokenWorkspaceGrant, (f.token.id, f.users.workspace_id))
                    )
            if change == "lease":
                lease["lease_generation"] += 1
                altered["_lease_generation"] += 1
            if change == "payload":
                altered["expected_voice_revision"] = True
            with (
                sql_capture(f.case.database) as statements,
                f.case.database.snapshot_session() as session,
            ):
                with pytest.raises(AccessDenied):
                    require_voice_sample_execution_workspace(
                        session,
                        altered,
                        "voice.normalize_recording",
                        voice_id=f.voice["id"],
                        artifact_ids=[data["source_artifact_id"]],
                    )
            reads = [sql for sql, _ in statements if "SELECT jobs.payload_json" in sql]
            assert len(reads) == (1 if change == "payload" else 0)
        with f.case.database.immediate_session() as session:
            session.get_one(m.Job, queued.id).status = "failed"
            if change == "grant":
                session.add(
                    ApiTokenWorkspaceGrant(token_id=f.token.id, workspace_id=f.users.workspace_id)
                )
    assert not f.calls


def test_membership_loss_in_progress_callback_stops_normalization_before_process(sample_case):
    f = sample_case
    queued = enqueue(f, "voice.normalize_recording")
    with claimed(f, queued) as (data, _lease):

        def progress(_fraction, _detail):
            with f.case.database.immediate_session() as session:
                session.delete(
                    session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
                )

        with pytest.raises(AccessDenied):
            f.services.workflow_handlers.normalize_voice_recording(
                data, progress, threading.Event()
            )
    assert not f.calls and f.path.is_file()
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.Voice, f.voice["id"]).revision == 1


@pytest.mark.parametrize("loss", ["membership", "lease", "settings"])
def test_transcription_processing_loss_cleans_only_owned_attempt_without_publication(
    sample_case, loss
):
    f = sample_case
    queued = enqueue(f, "voice.transcribe")
    retained = f.path.parent / "transcriptions" / "retained.srt"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_text("Prior result stays")

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
                session.add(
                    m.WorkspaceSetting(
                        workspace_id=f.users.workspace_id,
                        key="defaults.stt",
                        value_json={},
                        revision=1,
                    )
                )

    f.hook["during"] = change
    run(f)
    assert job(f, queued.id).status == "failed"
    assert (
        f.attempts
        and not f.attempts[0].exists()
        and retained.read_text() == "Prior result stays"
        and f.path.is_file()
    )
    with f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Artifact)) == 1
        assert session.scalar(select(func.count()).select_from(m.VoiceSample)) == 1
        assert session.get_one(m.Voice, f.voice["id"]).revision == 1


@pytest.mark.parametrize("transport", ["expired_token", "logged_out_browser"])
def test_natural_transport_end_does_not_cancel_saved_member_execution(sample_case, transport):
    f = sample_case
    if transport == "expired_token":
        actor = f.actor
    else:
        with f.users.alice.session_transaction() as state:
            browser = f.case.auth.browser_sessions.resolve(state["auth_session"])
        assert browser is not None
        actor = actor_from_principal(
            Principal(
                browser.subject,
                browser.kind,
                browser.scopes,
                None,
                "loopback",
                f.services.identity.snapshot(observed_origin="http://localhost").instance_id,
                user_id=browser.user_id,
                auth_revision=browser.auth_revision,
                browser_session_id=browser.session_id,
                client_id=browser.client_id,
            )
        )
    assert actor is not None
    queued = enqueue(f, "voice.normalize_recording", actor=actor)
    with f.case.database.immediate_session() as session:
        if transport == "expired_token":
            session.get_one(m.ApiToken, f.token.id).expires_at = m.utcnow() - timedelta(seconds=1)
        else:
            from pandrator.web.accounts.auth_models import BrowserAuthSession

            session.get_one(BrowserAuthSession, actor.browser_session_id).revoked_at = m.utcnow()
    run(f)
    assert job(f, queued.id).status == "succeeded" and f.calls == ["ffmpeg"]


def test_direct_calls_need_explicit_host_and_missing_actor_legacy_is_initial_only(sample_case):
    f = sample_case
    data = payload(f, "voice.normalize_recording")
    with execution_actor_scope(None), f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_voice_sample_execution_workspace(session, data, "voice.normalize_recording")
    with execution_actor_scope(f.actor), f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_voice_sample_execution_workspace(session, data, "voice.normalize_recording")
    with execution_actor_scope(HostActor("pandrator-cli")):
        result = f.services.workflow_handlers.normalize_voice_recording(
            data, lambda *_args: None, threading.Event()
        )
    with f.case.database.snapshot_session() as session:
        assert (
            session.get_one(m.Artifact, result["artifact_id"]).workspace_id == f.users.workspace_id
        )
    legacy_voice = body(
        f.case.client.post(
            "/api/v1/voices", json={"name": "Legacy stored"}, headers={"X-CSRF-Token": f.case.csrf}
        ),
        201,
    )
    # A genuine old rootless job has no actor sidecar; only its canonical
    # initial personal root can qualify the historical compatibility branch.
    for wid, voice_id, expected in (
        (f.users.workspace_id, f.voice["id"], False),
        (legacy_voice["workspace_id"], legacy_voice["id"], True),
    ):
        stored = {"voice_id": voice_id}
        with f.case.database.immediate_session() as session:
            row = m.Job(
                kind="voice.normalize_recording",
                workspace_id=wid,
                status="queued",
                payload_json=stored,
            )
            session.add(row)
            session.flush()
            identifier = row.id
        claim = f.services.jobs.claim("legacy-worker")
        assert claim.id == identifier
        lease = current_job_lease.set(
            {
                "job_id": identifier,
                "owner_id": "legacy-worker",
                "lease_generation": claim.lease_generation,
            }
        )
        try:
            with execution_actor_scope(None), f.case.database.snapshot_session() as session:
                supplied = {
                    **stored,
                    "_job_id": identifier,
                    "_lease_generation": claim.lease_generation,
                }
                if expected:
                    assert (
                        require_voice_sample_execution_workspace(
                            session, supplied, "voice.normalize_recording", voice_id=voice_id
                        ).id
                        == wid
                    )
                else:
                    with pytest.raises(AccessDenied):
                        require_voice_sample_execution_workspace(
                            session, supplied, "voice.normalize_recording", voice_id=voice_id
                        )
        finally:
            current_job_lease.reset(lease)
        with f.case.database.immediate_session() as session:
            session.get_one(m.Job, identifier).status = "failed"
