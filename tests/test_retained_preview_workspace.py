"""Public Member admission retains exact local speech inputs across library edits."""

from __future__ import annotations

import base64
import json
import socket
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Literal

import pytest
import requests
from sqlalchemy import func, select

from pandrator.logic import tts_handler
from pandrator.logic.tts_retry import TtsRetryGate
from pandrator.web import models as m
from pandrator.web import speech_inputs
from pandrator.web.accounts.execution_actor import execution_actor_scope
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_settings_workspace_access import put
from tests.test_shared_tts_preview import worker
from tests.test_voice_crud_workspace_access import create, sample
from tests.test_voice_lifecycle_idempotency import silent_wav

MODEL = "qwen3_tts_1_7b_base_q8_0"
PATH = "/api/v1/services/tts/audio_cpp/preview"
TRANSCRIPT = "The selected original reference."


@pytest.fixture
def retained(design, monkeypatch):
    f = design
    body(
        put(
            f.users.owner,
            f.users.oh,
            "services.tts",
            {
                "provider_configs": [
                    {
                        "id": "audio_cpp",
                        "enabled": True,
                        "models": [MODEL],
                        "default_model": MODEL,
                        "api_base": "https://speech.invalid",
                        "connection_mode": "external",
                        "api_key": "synthetic-workspace-speech-key",
                        "credential_backend": "database",
                    }
                ]
            },
            revision=1,
            workspace=f.users.workspace_id,
        )
    )
    voice = body(
        create(f.users.alice, f.users.ah, "Retained workshop voice", wid=f.users.workspace_id), 201
    )
    sid, aid, path = sample(f.case, voice)
    # Publication of linked references is separately qualified; seed its saved state.
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "status": "ready",
                    "resource_kind": "linked_reference",
                    "voice_id": "workshop-reference",
                }
            }
        }
        row = session.get_one(m.VoiceSample, sid)
        row.transcript, row.transcript_reviewed = TRANSCRIPT, True
    monkeypatch.setattr(tts_handler, "_TTS_RETRY_GATE", TtsRetryGate())
    monkeypatch.setattr(
        tts_handler,
        "wait_for_retry",
        lambda *_a, **_k: pytest.fail("Unexpected retry in offline input acceptance"),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: pytest.fail("Unexpected DNS in offline input acceptance"),
    )
    return SimpleNamespace(f=f, voice=voice, sid=sid, aid=aid, path=path)


def submit(r, *, key="retained-preview-key", voice_id: str | None | Literal[True] = True):
    value = {
        "text": "An immutable shared audition.",
        "model": MODEL,
        "voice": "workshop-reference",
        "language": "en",
    }
    if voice_id is True:
        value["voice_id"] = r.voice["id"]
    elif voice_id is not None:
        value["voice_id"] = voice_id
    return r.f.users.alice.post(
        PATH,
        json=value,
        query_string={"workspace_id": r.f.users.workspace_id},
        headers=headers(r.f.users.ah, key),
    )


def snapshots(r):
    with r.f.case.database.snapshot_session() as session:
        return {
            role: session.scalar(
                select(func.count()).select_from(m.Artifact).where(m.Artifact.role == role)
            )
            for role in ("speech_input", "speech_reference_audio", "tts_voice_preview")
        }


def transport(monkeypatch, *, reference=None, transcript=TRANSCRIPT, after_send=None):
    reference = silent_wav() if reference is None else reference
    calls = []

    def post(url, **options):
        assert url == "https://speech.invalid/v1/audio/speech"
        assert options["allow_redirects"] is False
        wire = json.dumps(options["json"])
        assert base64.b64encode(reference).decode() in wire
        assert transcript in wire
        calls.append(options["json"])
        if after_send:
            after_send()
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "audio/wav"
        response._content = silent_wav()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        requests.Session, "post", lambda _session, url, **kwargs: post(url, **kwargs)
    )
    monkeypatch.setattr(
        requests, "get", lambda *_a, **_k: pytest.fail("Reference audition performed discovery")
    )
    return calls


def test_member_queue_survives_original_sample_deletion_and_exact_retry(retained, monkeypatch):
    r = retained
    first = body(submit(r), 202)
    with r.f.case.database.snapshot_session() as session:
        job = session.get_one(m.Job, first["id"])
        pin = dict(job.payload_json["speech_input"])
        assert "audio_cpp_voice_ref" not in job.payload_json["settings"]
        assert job.workspace_id == r.f.users.workspace_id
    deleted = r.f.users.bob.delete(
        f"/api/v1/voices/{r.voice['id']}/samples/{r.sid}",
        headers={**r.f.users.bh, "If-Match": '"1"'},
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert not r.path.exists()
    before = snapshots(r)
    original_capture = speech_inputs.capture_speech_input
    monkeypatch.setattr(
        speech_inputs,
        "capture_speech_input",
        lambda *_a, **_k: pytest.fail("Exact retry reread mutable library"),
    )
    replay = submit(r)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert body(replay, 202) == first and snapshots(r) == before
    monkeypatch.setattr(speech_inputs, "capture_speech_input", original_capture)
    calls = transport(monkeypatch)
    handlers, run = worker(r.f)
    monkeypatch.setattr(
        handlers,
        "prepare_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("Worker reread library"),
    )
    assert run.run_once()
    result = r.f.services.jobs.get(first["id"])
    assert result.status == "succeeded", result.error_message
    assert len(calls) == 1
    with r.f.case.database.snapshot_session() as session:
        artifact = session.get_one(m.Artifact, result.result_json["artifact_id"])
        assert artifact.workspace_id == r.f.users.workspace_id
        edge = session.get(m.ArtifactEdge, (pin["artifact_id"], artifact.id))
        assert edge is not None and edge.relation == "derived_from"
        assert r.f.services.paths.managed_path(artifact.relative_path).read_bytes() == silent_wav()
    public = body(r.f.users.bob.get(f"/api/v1/jobs/{first['id']}"))
    assert "speech_input" not in public["payload_json"]
    assert "audio_cpp_voice_ref" not in json.dumps(public)


def test_same_reference_reuses_audio_and_metadata_edit_reuses_only_audio(retained):
    r = retained
    a = body(submit(r, key="first-retained-reference"), 202)
    b = body(submit(r, key="second-retained-reference"), 202)
    assert a["id"] != b["id"]
    assert snapshots(r) == {"speech_input": 1, "speech_reference_audio": 1, "tts_voice_preview": 0}
    changed = r.f.users.bob.patch(
        f"/api/v1/voices/{r.voice['id']}/samples/{r.sid}/transcript",
        json={"transcript": "The revised reference text.", "language": "en"},
        headers={**r.f.users.bh, "If-Match": '"1"'},
    )
    assert changed.status_code == 200, changed.get_json()
    body(submit(r, key="third-retained-reference"), 202)
    assert snapshots(r) == {"speech_input": 2, "speech_reference_audio": 1, "tts_voice_preview": 0}


@pytest.mark.parametrize("change", ["membership", "settings", "source"])
def test_late_staging_changes_cannot_publish_input_or_job(retained, monkeypatch, change):
    r = retained
    original = speech_inputs.prepare_speech_input
    paths = []

    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        paths.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
        with r.f.case.database.immediate_session() as session:
            if change == "membership":
                session.delete(
                    session.get_one(
                        m.WorkspaceMembership, (r.f.users.workspace_id, r.f.users.alice_id)
                    )
                )
            elif change == "settings":
                session.get_one(
                    m.WorkspaceSetting, (r.f.users.workspace_id, "services.tts")
                ).revision += 1
            else:
                session.get_one(m.Voice, r.voice["id"]).revision += 1
        return prepared

    monkeypatch.setattr(speech_inputs, "prepare_speech_input", prepare)
    response = submit(r)
    assert response.status_code in {403, 404, 422}, response.get_json()
    assert snapshots(r) == {"speech_input": 0, "speech_reference_audio": 0, "tts_voice_preview": 0}
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Job)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.ApiIdempotency)
                .where(m.ApiIdempotency.operation_id == "ttsVoicePreview")
            )
            == 0
        )
    assert paths and all(not r.f.services.paths.managed_path(path).exists() for path in paths)


def test_actual_source_bytes_are_verified_before_publication(retained):
    r = retained
    r.path.write_bytes(silent_wav() + b"unexpected")
    response = submit(r)
    assert response.status_code == 422, response.get_json()
    assert snapshots(r) == {"speech_input": 0, "speech_reference_audio": 0, "tts_voice_preview": 0}


def test_retired_snapshot_after_provider_response_cannot_publish_output(retained, monkeypatch):
    r = retained
    queued = body(submit(r), 202)
    with r.f.case.database.snapshot_session() as session:
        pin = session.get_one(m.Job, queued["id"]).payload_json["speech_input"]

    def retire():
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, pin["artifact_id"]).state = "deleted"

    calls = transport(monkeypatch, after_send=retire)
    _handlers, run = worker(r.f)
    assert run.run_once() and len(calls) == 1
    assert r.f.services.jobs.get(queued["id"]).status == "failed"
    assert snapshots(r)["tts_voice_preview"] == 0


def test_foreign_and_missing_managed_selectors_have_the_same_public_denial(retained):
    r = retained
    private = body(create(r.f.users.bob, r.f.users.bh, "Private reference"), 201)
    foreign = submit(r, key="foreign-managed-selection", voice_id=private["id"])
    absent = submit(
        r, key="missing-managed-selection", voice_id="00000000-0000-0000-0000-000000000000"
    )
    assert foreign.status_code == absent.status_code == 422
    expected, actual = foreign.get_json()["error"], absent.get_json()["error"]
    assert {k: v for k, v in expected.items() if k != "request_id"} == {
        k: v for k, v in actual.items() if k != "request_id"
    }
    assert snapshots(r)["speech_input"] == 0


def test_concurrent_exact_retry_wins_without_writer_staging_or_orphans(retained, monkeypatch):
    r = retained
    original_prepare = speech_inputs.prepare_speech_input
    original_writer = r.f.case.database.immediate_session
    active_writers = []
    unused = []
    winner = []
    reentered = False

    @contextmanager
    def writer():
        with original_writer() as session:
            active_writers.append(True)
            try:
                yield session
            finally:
                active_writers.pop()

    def prepare(*args, **kwargs):
        nonlocal reentered
        assert not active_writers, "Protected byte staging held a database writer"
        prepared = original_prepare(*args, **kwargs)
        if not reentered:
            reentered = True
            unused.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
            # Separate request context, matching two real concurrent clients.
            with execution_actor_scope(None), r.f.case.app.app_context():
                winner.append(body(submit(r, key="concurrent-retained-input"), 202))
        return prepared

    monkeypatch.setattr(r.f.case.database, "immediate_session", writer)
    monkeypatch.setattr(speech_inputs, "prepare_speech_input", prepare)
    response = submit(r, key="concurrent-retained-input")
    assert response.headers["Idempotency-Replayed"] == "true"
    assert body(response, 202) == winner[0]
    assert snapshots(r) == {"speech_input": 1, "speech_reference_audio": 1, "tts_voice_preview": 0}
    assert unused and all(not r.f.services.paths.managed_path(path).exists() for path in unused)
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Job)) == 1


def test_new_alias_after_admission_cannot_change_a_no_reference_preview(retained, monkeypatch):
    from pydub import AudioSegment

    r = retained
    queued = body(
        r.f.users.alice.post(
            PATH,
            query_string={"workspace_id": r.f.users.workspace_id},
            json={
                "text": "No new reference.",
                "model": MODEL,
                "voice": "later-reference",
                "language": "en",
            },
            headers=headers(r.f.users.ah, "no-reference-input"),
        ),
        202,
    )
    with r.f.case.database.immediate_session() as session:
        assert session.get_one(m.Job, queued["id"]).payload_json["speech_input"] is None
        session.get_one(m.Voice, r.voice["id"]).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "status": "ready",
                    "resource_kind": "linked_reference",
                    "voice_id": "later-reference",
                }
            }
        }
    settings_seen = []
    handlers, run = worker(r.f)
    monkeypatch.setattr(
        handlers,
        "prepare_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("No-reference preview resolved a late alias"),
    )

    def synthesize(_text, settings, **_kwargs):
        settings_seen.append(settings)
        return AudioSegment.silent(20)

    # This test qualifies input selection, not a provider's no-reference capability.
    monkeypatch.setattr(handlers.tts_providers, "synthesize", synthesize)
    assert run.run_once()
    assert r.f.services.jobs.get(queued["id"]).status == "succeeded"
    assert settings_seen and "audio_cpp_voice_ref" not in settings_seen[0]
    assert snapshots(r) == {"speech_input": 0, "speech_reference_audio": 0, "tts_voice_preview": 1}
