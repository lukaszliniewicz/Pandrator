"""Representative real-lease evidence execution and publication qualification."""

from __future__ import annotations

import json
import socket
import threading
import wave
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import (
    actor_from_principal,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore, Principal
from pandrator.web.evidence_execution import prepare_evidence_execution
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Network is outside evidence qualification")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def case(tmp_path, monkeypatch):
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap, background_maintenance=False
    )
    services = app.extensions["pandrator"]["services"]
    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()})
    with client.session_transaction() as state:
        resolved = services.auth.browser_sessions.resolve(state["auth_session"])
    assert resolved is not None
    principal = Principal(
        resolved.subject,
        resolved.kind,
        resolved.scopes,
        None,
        "loopback",
        services.identity.snapshot(observed_origin="http://localhost").instance_id,
        user_id=resolved.user_id,
        auth_revision=resolved.auth_revision,
        browser_session_id=resolved.session_id,
        client_id=resolved.client_id,
    )
    source = services.sessions.create("Private evidence source", workflow_kind="subtitles")
    directory = services.paths.sessions / source.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    media_path = directory / "source.wav"
    with wave.open(str(media_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 40000)
    media = services.artifacts.register(media_path, kind="wav", role="upload", session_id=source.id)
    subtitle_path = directory / "source.srt"
    subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nPRIVATE cue\n")
    subtitle = services.artifacts.register(
        subtitle_path, kind="srt", role="transcription", session_id=source.id
    )
    with services.database.immediate_session() as session:
        document = m.Document(session_id=source.id, stage="transcription", language="en")
        session.add(document)
        session.flush()
        revision = m.DocumentRevision(
            document_id=document.id, revision_number=1, content_hash="revision-one"
        )
        session.add(revision)
        session.flush()
        segment = m.Segment(
            revision_id=revision.id, ordinal=0, start_ms=1000, end_ms=2000, text="PRIVATE cue"
        )
        session.add(segment)
        session.flush()
        document.active_revision_id = revision.id
        session.get(m.Artifact, subtitle.id).metadata_json = {"revision_id": revision.id}
        workspace_id = session.get(m.Project, source.project_id).workspace_id
    state = SimpleNamespace(
        services=services,
        database=services.database,
        source=source,
        subtitle=subtitle,
        media=media,
        principal=principal,
        actor=actor_from_principal(principal),
        workspace_id=workspace_id,
        calls=[],
        hook=None,
        root=tmp_path,
        revision_id=revision.id,
    )

    def transcribe(output_dir, _clip, settings, **kwargs):
        state.calls.append(str(settings.get("stt_engine")))
        if state.hook is not None:
            state.hook()
        if kwargs.get("progress_callback"):
            kwargs["progress_callback"](0.5, "PRIVATE endpoint /private/path")
        path = Path(output_dir) / "transcript.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "language": "en",
                    "metadata": {"provider": "test", "model": "synthetic"},
                    "segments": [
                        {
                            "id": "one",
                            "start_ms": 1000,
                            "end_ms": 2000,
                            "text": "Verified cue",
                            "words": [{"text": "Verified cue", "start_ms": 1000, "end_ms": 2000}],
                        }
                    ],
                }
            )
        )
        return SimpleNamespace(
            word_timestamps_path=str(path), engine="whisper", compute_backend="cpu"
        )

    monkeypatch.setattr(
        "pandrator.web.subtitle_evidence.transcribe_source_file_with_metadata", transcribe
    )

    yield state
    services.database.dispose()


def admit(case, *, routes=None, audio_model_ids=None):
    with execution_actor_scope(case.actor):
        value = case.services.subtitle_evidence.request(
            case.source.id,
            {
                "source_artifact_id": case.subtitle.id,
                "cue_id": 1,
                "reason": "PRIVATE reason",
                "routes": routes or ["whisper"],
                "audio_model_ids": audio_model_ids or [],
            },
        )
    return value["record"]["id"], value["record"]["job_id"]


def handler(case):
    def run(payload, progress, canceled):
        execution = prepare_evidence_execution(case.database, case.services.jobs, payload)
        return case.services.subtitle_evidence.run_request(
            payload["evidence_id"],
            progress,
            canceled,
            force_refresh=payload.get("force_refresh", False),
            _execution=execution,
        )

    return run


def work(case, run=None):
    assert Worker(
        case.services.jobs, "evidence-worker", {"subtitle.evidence": run or handler(case)}
    ).run_once()


def record(case, evidence_id):
    with case.database.snapshot_session() as session:
        value = session.get(m.SubtitleEvidence, evidence_id)
        session.expunge(value)
        return value


@contextmanager
def claimed(case, job_id, *, actor=None, saved_actor=True):
    job = case.services.jobs.claim("manual-evidence-worker")
    assert job.id == job_id
    payload = {**job.payload_json, "_job_id": job.id, "_lease_generation": job.lease_generation}
    lease = current_job_lease.set(
        {
            "job_id": job.id,
            "owner_id": "manual-evidence-worker",
            "lease_generation": job.lease_generation,
        }
    )
    try:
        with execution_actor_scope(case.actor if saved_actor else actor):
            yield payload
    finally:
        current_job_lease.reset(lease)


def test_real_worker_atomic_completion_cache_attempts_and_private_progress(case):
    before = {p: p.read_bytes() for p in (case.root / "sessions").rglob("*") if p.is_file()}
    evidence_id, job_id = admit(case)
    writer = threading.local()
    writer.active = False

    def sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.upper().startswith("BEGIN IMMEDIATE"):
            writer.active = True

    def finish(_conn):
        writer.active = False

    event.listen(case.database.engine, "before_cursor_execute", sql)
    event.listen(case.database.engine, "commit", finish)
    event.listen(case.database.engine, "rollback", finish)
    original = case.services.artifacts.prepare_registration

    def prepared(*args, **kwargs):
        assert not writer.active
        return original(*args, **kwargs)

    case.hook = lambda: pytest.fail("Provider inside writer") if writer.active else None
    with patch.object(case.services.artifacts, "prepare_registration", side_effect=prepared):
        work(case)
    first = record(case, evidence_id)
    job = case.services.jobs.get(job_id)
    assert first.status == "completed" and job.status == "succeeded"
    assert len(first.candidates_json) == 1 and first.candidates_json[0]["status"] == "success"
    with case.database.snapshot_session() as session:
        events = list(session.scalars(select(m.JobEvent).where(m.JobEvent.job_id == job_id)))
        assert sum(e.event_type == "job.succeeded" for e in events) == 1
        actor = session.get(m.JobActorRecord, job_id)
        assert actor.user_id == case.actor.user_id
        outputs = list(
            session.scalars(select(m.Artifact).where(m.Artifact.role.like("subtitle_evidence_%")))
        )
        assert len(outputs) == 2
        assert all(
            (o.project_id, o.workspace_id, o.session_id)
            == (case.source.project_id, case.workspace_id, case.source.id)
            for o in outputs
        )
    assert "PRIVATE" not in str(
        [(e.event_type, e.payload_json) for e in events if e.event_type == "job.progress"]
    )
    second_id, _ = admit(case)
    work(case)
    second = record(case, second_id)
    assert len(case.calls) == 1
    assert second.candidates_json[0]["reused_from"]["evidence_id"] == evidence_id
    first_path = case.services.artifacts.resolve(first.clip_artifact_id)[1]
    second_path = case.services.artifacts.resolve(second.clip_artifact_id)[1]
    assert first_path.parent.parent != second_path.parent.parent
    assert all(p.read_bytes() == data for p, data in before.items())
    assert current_execution_actor.get() is None


@pytest.mark.parametrize("change", ["headers", "sidecar", "foreign_admin", "resources"])
def test_before_input_io_denials(case, change):
    evidence_id, job_id = admit(case)
    with claimed(case, job_id) as payload:
        with case.database.immediate_session() as session:
            if change == "sidecar":
                session.get(m.JobActorRecord, job_id).context_json = {"malformed": True}
            elif change == "foreign_admin":
                session.delete(
                    session.get(m.WorkspaceMembership, (case.workspace_id, case.actor.user_id))
                )
            elif change == "resources":
                session.get(m.Artifact, case.media.id).state = "deleted"
        if change == "headers":
            payload["session_id"] = new_id()
        with pytest.raises(PermissionError):
            prepare_evidence_execution(case.database, case.services.jobs, payload)
    assert not case.calls and record(case, evidence_id).clip_artifact_id is None


@pytest.mark.parametrize("change", ["logout", "expiry"])
def test_natural_transport_end_continues_real_lease(case, change):
    evidence_id, _ = admit(case)
    with case.database.immediate_session() as session:
        row = session.get(m.BrowserAuthSession, case.principal.browser_session_id)
        if change == "logout":
            row.revoked_at = m.utcnow()
        else:
            row.expires_at = m.utcnow() - timedelta(seconds=1)
    work(case)
    assert record(case, evidence_id).status == "completed"


@pytest.mark.parametrize("change", ["reset", "disable", "membership", "input", "lease"])
def test_real_provider_boundary_loss_stops_publication_with_only_own_failure_metadata(case, change):
    evidence_id, job_id = admit(case)

    def mutate():
        with case.database.immediate_session() as session:
            if change == "reset":
                session.get(m.UserCredential, case.actor.user_id).auth_revision += 1
            elif change == "disable":
                session.get(m.User, case.actor.user_id).is_active = False
            elif change == "membership":
                session.delete(
                    session.get(m.WorkspaceMembership, (case.workspace_id, case.actor.user_id))
                )
            elif change == "input":
                session.get(m.DocumentRevision, case.revision_id).content_hash = "revision-two"
            else:
                session.get(m.Job, job_id).lease_expires_at = m.utcnow() - timedelta(seconds=1)

    case.hook = mutate
    work(case)
    value = record(case, evidence_id)
    assert not value.candidates_json
    assert value.clip_artifact_id is not None  # already valid partial clip remains
    assert value.status == ("running" if change == "lease" else "failed")
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(m.Artifact.id).where(m.Artifact.role == "subtitle_evidence_transcript")
            )
            is None
        )
    assert case.services.jobs.get(job_id).status != "succeeded"


def test_atomic_final_rollback_and_fixed_unexpected_error(case, monkeypatch):
    evidence_id, job_id = admit(case)
    original = case.services.jobs.complete_in_session

    def fail(session, *args, **kwargs):
        original(session, *args, **kwargs)
        raise RuntimeError("PRIVATE SQL endpoint /secret/path")

    monkeypatch.setattr(case.services.jobs, "complete_in_session", fail)
    work(case)
    value = record(case, evidence_id)
    assert value.status == "failed"
    assert value.candidates_json[0]["status"] == "success"  # valid partial work retained
    job = case.services.jobs.get(job_id)
    assert job.status == "failed" and not job.result_json
    assert "PRIVATE" not in job.error_message and "/secret/path" not in job.error_message
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(m.JobEvent.id).where(
                    m.JobEvent.job_id == job_id, m.JobEvent.event_type == "job.succeeded"
                )
            )
            is None
        )

    monkeypatch.setattr(case.services.jobs, "complete_in_session", original)
    second_id, second_job = admit(case)
    original_writer = case.database.immediate_session
    sealed = False

    def mark_sealed(*args, **kwargs):
        nonlocal sealed
        original(*args, **kwargs)
        sealed = True

    @contextmanager
    def uncertain_writer(*args, **kwargs):
        with original_writer(*args, **kwargs) as session:
            yield session
        if sealed:
            raise RuntimeError("PRIVATE lost commit acknowledgement")

    # Scope the failure hook to the real handler, not the queue's own claim/ack.
    def uncertain_handler(payload, progress, canceled):
        with (
            patch.object(case.services.jobs, "complete_in_session", side_effect=mark_sealed),
            patch.object(case.database, "immediate_session", uncertain_writer),
        ):
            return handler(case)(payload, progress, canceled)

    work(case, uncertain_handler)
    assert record(case, second_id).status == "completed"
    assert case.services.jobs.get(second_job).status == "succeeded"
    with case.database.snapshot_session() as session:
        outputs = list(
            session.scalars(select(m.Artifact).where(m.Artifact.role.like("subtitle_evidence_%")))
        )
    assert outputs and all(
        case.services.paths.managed_path(o.relative_path).is_file() for o in outputs
    )


def test_provider_failure_keeps_partial_candidate_and_cancellation_uses_metadata_only(case):
    evidence_id, job_id = admit(case, routes=["whisper", "moss"])
    calls = 0

    def fail_second():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("PRIVATE endpoint")

    case.hook = fail_second
    work(case)
    value = record(case, evidence_id)
    assert value.status == "completed"
    assert [c["status"] for c in value.candidates_json] == ["success", "failed"]
    assert value.candidates_json[1]["error"] == "Evidence route failed."
    second_id, second_job = admit(case)

    def cancel():
        case.services.jobs.request_cancel(second_job)

    case.hook = cancel
    # Avoid cache reuse so the actual provider boundary triggers cancellation.
    with case.database.immediate_session() as session:
        row = session.get(m.Job, second_job)
        row.payload_json = {**row.payload_json, "force_refresh": True}
    work(case)
    assert record(case, second_id).status == "failed"
    assert case.services.jobs.get(second_job).status == "canceled"


def test_unleased_user_and_actorless_sidecar_never_become_trusted(case):
    evidence_id, job_id = admit(case)
    with execution_actor_scope(case.actor), pytest.raises(PermissionError):
        prepare_evidence_execution(
            case.database,
            case.services.jobs,
            {"evidence_id": evidence_id, "session_id": case.source.id},
        )
    with claimed(case, job_id, saved_actor=False) as payload, pytest.raises(PermissionError):
        prepare_evidence_execution(case.database, case.services.jobs, payload)
    case.services.jobs.fail(
        job_id,
        "manual-evidence-worker",
        "evidence_failed",
        "Subtitle evidence execution failed.",
        lease_generation=1,
    )
    with execution_actor_scope(None):
        assert prepare_evidence_execution(case.database, case.services.jobs, {}) is None
        for marker in ({"_job_id": job_id}, {"_lease_generation": 1}):
            with pytest.raises(PermissionError):
                prepare_evidence_execution(case.database, case.services.jobs, marker)
        legacy = case.services.subtitle_evidence.request(
            case.source.id,
            {
                "source_artifact_id": case.subtitle.id,
                "cue_id": 1,
                "reason": "Legacy trusted request",
                "routes": ["whisper"],
            },
        )
    work(case)
    assert record(case, legacy["record"]["id"]).status == "completed"


def test_audio_library_requires_independent_grant_and_freezes_model_revision(case, monkeypatch):
    # A project grant permits builtin routes but not browsing the workspace's
    # selected audio model namespace. Real credential ceilings decide both.
    from pandrator.web.accounts.resource_grant_models import ApiTokenProjectGrant
    from tests.test_project_copy_jobs import _token

    case.source_workspace = case.workspace_id
    case.destination = case.workspace_id
    # _token iterates the two workspace slots; use distinct real destination.
    with case.database.immediate_session() as session:
        destination = m.Workspace(id=new_id(), kind="team", name="Other namespace")
        session.add(destination)
        session.flush()
        case.destination = destination.id
        provider = m.Provider(
            workspace_id=case.workspace_id,
            kind="llm",
            provider_key="openai",
            label="Synthetic audio",
            enabled=True,
        )
        session.add(provider)
        session.flush()
        model = m.ProviderModel(
            provider_id=provider.id,
            model_id="synthetic-audio",
            is_active=True,
            input_modalities_json=["audio"],
            revision=1,
        )
        session.add(model)
        session.flush()
        model_id = model.id
    case.target = case.services.identity.snapshot(observed_origin="http://localhost").model_dump(
        mode="json"
    )
    token_principal = _token(case, client=True)
    case.actor = actor_from_principal(token_principal)
    evidence_id, job_id = admit(case, routes=["whisper", "audio_llm"], audio_model_ids=[model_id])
    with case.database.immediate_session() as session:
        for grant in session.scalars(
            select(m.ApiTokenWorkspaceGrant).where(
                m.ApiTokenWorkspaceGrant.token_id == token_principal.token_id,
                m.ApiTokenWorkspaceGrant.workspace_id == case.workspace_id,
            )
        ):
            session.delete(grant)
        session.add(
            ApiTokenProjectGrant(
                token_id=token_principal.token_id, project_id=case.source.project_id
            )
        )
    with claimed(case, job_id) as payload, pytest.raises(PermissionError):
        prepare_evidence_execution(case.database, case.services.jobs, payload)
    assert not case.calls
    # Restore exact real library grant, then change model revision after actual
    # excerpt/clip staging. A later operation must not consume the new binding.
    with case.database.immediate_session() as session:
        session.add(
            m.ApiTokenWorkspaceGrant(
                token_id=token_principal.token_id, workspace_id=case.workspace_id
            )
        )

    def change_model():
        with case.database.immediate_session() as session:
            session.get(m.ProviderModel, model_id).revision += 1

    case.hook = change_model
    # Requeue is not needed: use the same actual still-live manually captured
    # lease; the check at execution start now sees the restored namespace.
    lease_token = current_job_lease.set(
        {"job_id": job_id, "owner_id": "manual-evidence-worker", "lease_generation": 1}
    )
    try:
        with execution_actor_scope(case.actor):
            payload = {
                **case.services.jobs.get(job_id).payload_json,
                "_job_id": job_id,
                "_lease_generation": 1,
            }
            execution = prepare_evidence_execution(case.database, case.services.jobs, payload)
            with pytest.raises(PermissionError):
                case.services.subtitle_evidence.run_request(
                    evidence_id, lambda *_: None, threading.Event(), _execution=execution
                )
    finally:
        current_job_lease.reset(lease_token)
    assert record(case, evidence_id).status == "failed"
    assert not record(case, evidence_id).candidates_json

    assert case.services.jobs.fail(
        job_id,
        "manual-evidence-worker",
        "evidence_failed",
        "Subtitle evidence execution failed.",
        lease_generation=1,
    )
    case.hook = None

    def audio_boundary(*_args, **_kwargs):
        return SimpleNamespace(
            transcript="Audio witness",
            transport_metadata={},
            completion=SimpleNamespace(usage={}, cost=None, cost_source=None),
        )

    monkeypatch.setattr("pandrator.web.subtitle_evidence.transcribe_audio_evidence", audio_boundary)
    successful_id, _ = admit(case, routes=["audio_llm"], audio_model_ids=[model_id])
    work(case)
    final = record(case, successful_id)
    assert final.status == "completed"
    assert final.candidates_json[0]["route"] == "audio_llm"
    assert case.services.artifacts.resolve(final.candidates_json[0]["transcript_artifact_id"])[
        1
    ].is_file()

    # The same leased boundary uses current token/client/resource policy; use
    # separate real credentials so one revocation cannot mask another.
    for revoked in ("scope", "client", "resource", "workspace"):
        principal = _token(case, client=True)
        case.actor = actor_from_principal(principal)
        next_id, next_job = admit(case)
        with case.database.immediate_session() as session:
            job = session.get(m.Job, next_job)
            job.payload_json = {**job.payload_json, "force_refresh": True}

        def revoke_current(revoked=revoked, principal=principal):
            with case.database.immediate_session() as session:
                if revoked == "scope":
                    session.get(m.ApiToken, principal.token_id).scopes_json = [
                        "app.read",
                        "app.write",
                    ]
                elif revoked == "client":
                    session.get(m.AutomationClient, principal.client_id).revoked_at = m.utcnow()
                elif revoked == "workspace":
                    session.get(m.Workspace, case.workspace_id).state = "archived"
                else:
                    for grant in session.scalars(
                        select(m.ApiTokenWorkspaceGrant).where(
                            m.ApiTokenWorkspaceGrant.token_id == principal.token_id,
                            m.ApiTokenWorkspaceGrant.workspace_id == case.workspace_id,
                        )
                    ):
                        session.delete(grant)

        case.hook = revoke_current
        work(case)
        final = record(case, next_id)
        assert final.status == "failed" and not final.candidates_json
        assert final.clip_artifact_id is not None
        assert case.services.jobs.get(next_job).status != "succeeded"
        if revoked == "workspace":
            with case.database.immediate_session() as session:
                session.get(m.Workspace, case.workspace_id).state = "active"
