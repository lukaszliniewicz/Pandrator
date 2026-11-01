"""Finite real-user admission, retry and atomic publication fences for samples."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from werkzeug.datastructures import FileStorage

from pandrator.web import models as m
from pandrator.web import voice_recording_uploads as uploads
from pandrator.web import voice_routes as routes
from pandrator.web import workflow_voice
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_crud_workspace_access import create, sample
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401
from tests.test_voice_lifecycle_idempotency import silent_wav
from tests.test_voice_sample_execution_access import api_actor


@pytest.fixture
def normalization(case, monkeypatch):
    members = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    voice = body(create(members.bob, members.bh, wid=members.workspace_id), 201)
    sid, aid, original = sample(case, voice)
    preview_path = services.paths.artifacts / "voice-preview.wav"
    preview_path.write_bytes(silent_wav())
    preview = services.artifacts.register(
        preview_path, kind="audio", role="tts_voice_preview", workspace_id=members.workspace_id,
        metadata={"service_id": "audio_cpp", "model": "breeze_tts_2_q8_0",
            "preview_text": "Reviewed reference", "language": "en"},
    )
    calls = []

    def ffmpeg(command, **_kwargs):
        calls.append(command)
        destination = Path(command[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(command[command.index("-i") + 1]), destination)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workflow_voice.subprocess, "run", ffmpeg)
    return SimpleNamespace(case=case, members=members, services=services, voice=voice,
        sid=sid, aid=aid, original=original, preview=preview, calls=calls)


def submit(f, kind, *, client=None, csrf=None, revision=1, key=None, query=None, extra=None):
    client = client or f.members.alice
    csrf = csrf or f.members.ah
    request_headers = headers(csrf, key) if key else dict(csrf)
    root = f"/api/v1/voices/{f.voice['id']}/samples"
    if kind in {"upload", "replace"}:
        request_headers["If-Match"] = f'"{revision}"'
        return client.post(
            root + (f"/{f.sid}/replace" if kind == "replace" else ""),
            data={"file": (io.BytesIO(silent_wav()), "recording.wav"),
                "expected_revision": str(revision), "noise_reduction": "none"},
            headers=request_headers, query_string=query or {},
        )
    requested = {"artifact_id": f.preview.id if kind == "preview" else f.aid,
        "expected_voice_revision": revision, "transcript": "Reviewed reference", "language": "en"}
    if kind == "artifact":
        requested["transcript_reviewed"] = True
    requested.update(extra or {})
    return client.post(root + f"/from-{kind}", json=requested,
        headers=request_headers, query_string=query or {})


def count(case, model):
    with case.database.snapshot_session() as session:
        return session.scalar(select(func.count()).select_from(model))


def run(f):
    assert Worker(f.services.jobs, "normalization-admission",
        f.services.workflow_handlers.handlers()).run_once()


def test_member_four_routes_and_registered_upload_and_artifact_workers(normalization):
    f = normalization
    queued = []
    for kind, revision, query in (
        ("upload", 1, {}), ("artifact", 2, {"workspace_id": f.members.workspace_id}),
        ("preview", 3, {}), ("replace", 3, {"workspace_id": f.members.workspace_id}),
    ):
        result = body(submit(f, kind, revision=revision, query=query), 202)
        queued.append(result["id"])
        assert result["workspace_id"] == f.members.workspace_id
        if kind in {"upload", "artifact"}:
            run(f)
            with f.case.database.snapshot_session() as session:
                job = session.get_one(m.Job, result["id"])
                assert job.status == "succeeded", job.error_message
                assert session.get_one(m.Artifact, job.result_json["artifact_id"]).workspace_id == f.members.workspace_id
                if kind == "upload":
                    source = session.get_one(m.Artifact, job.payload_json["source_artifact_id"])
                    assert job.payload_json["source_artifact_sha256"] == source.content_hash
                    assert job.payload_json["source_artifact_role"] == "recording_upload"
    with f.case.database.snapshot_session() as session:
        for identifier in queued:
            job = session.get_one(m.Job, identifier)
            actor = session.get_one(m.JobActorRecord, identifier)
            assert actor.user_id == f.members.alice_id
            assert job.workspace_id == f.members.workspace_id
            assert job.project_id is None and job.session_id is None
        assert session.get_one(m.Voice, f.voice["id"]).revision == 3
    assert len(f.calls) == 2 and f.original.exists()


def test_denials_precede_private_files_save_probes_and_enqueue(normalization, monkeypatch):
    f = normalization
    private = body(create(f.members.bob, f.members.bh, "Private"), 201)
    _sid, foreign_artifact, _path = sample(f.case, private)
    root = body(f.members.owner.post("/api/v1/sessions", json={"name": "Scope-only",
        "workflow_kind": "subtitles"}, headers=headers(f.members.oh, "normalization-project")), 201)
    current_viewers = body(f.members.owner.get(f"/api/v1/projects/{root['project_id']}/viewers"))
    body(f.members.owner.put(
        f"/api/v1/projects/{root['project_id']}/viewers/{f.members.alice_id}",
        json={"expected_revision": current_viewers["revision"]}, headers=f.members.oh,
    ))
    private_case = SimpleNamespace(**{**vars(f), "voice": private})
    credentials = []
    for scopes in ({"app.read"}, {"app.read", "app.write"}, {"app.read", "app.run"}):
        _, bearer = scoped_token(f.case, f.members.alice_id, scopes, workspace=f.members.workspace_id)
        credentials.append((bearer, 403))
    _, project_only = scoped_token(f.case, f.members.alice_id,
        {"app.read", "app.write", "app.run"}, project=root["project_id"])
    credentials.append((project_only, 404))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied normalization reached protected file/save/probe/job work")

    monkeypatch.setattr(routes, "sha256_file", forbidden)
    monkeypatch.setattr(routes, "inspect_voice_reference_trim", forbidden)
    monkeypatch.setattr(FileStorage, "save", forbidden)
    monkeypatch.setattr(f.services.jobs, "enqueue_in_session", forbidden)
    for kind in ("preview", "artifact", "upload", "replace"):
        for bearer, status in credentials:
            assert submit(f, kind, csrf=bearer).status_code == status
        assert submit(private_case, kind, client=f.members.owner, csrf=f.members.oh).status_code == 404
        assert submit(f, kind, query={"workspace_id": private["workspace_id"]}).status_code == 404
        for query in ({"session_id": "root"}, {"workspace_id": ""},
            {"workspace_id": " spaced "}, {"workspace_id": "x" * 81}):
            assert submit(f, kind, query=query).status_code == 422
    assert submit(f, "artifact", extra={"artifact_id": foreign_artifact}).status_code == 404
    saved = f.voice
    f.voice = private
    try:
        assert submit(f, "artifact").status_code == 404
        assert submit(f, "upload", client=f.members.owner, csrf=f.members.oh).status_code == 404
    finally:
        f.voice = saved
    assert count(f.case, m.Job) == 0


@pytest.mark.parametrize("kind", ["preview", "artifact"])
def test_json_receipts_replay_after_source_removal_with_current_user_authority(
    normalization, kind,
):
    f = normalization
    key = "normalization-retained-key"
    first = body(submit(f, kind, key=key), 202)
    distinct = body(submit(f, kind, key=key, client=f.members.bob, csrf=f.members.bh), 202)
    assert distinct["id"] != first["id"]
    conflict = {"recipe_signature": "a" * 64} if kind == "preview" else {"transcript": "Changed"}
    assert submit(f, kind, key=key, extra=conflict).status_code == 409
    source_id = f.preview.id if kind == "preview" else f.aid
    with f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, source_id).state = "deleted"
        session.get_one(m.Voice, f.voice["id"]).revision += 1
    extra = {"start_seconds": 0.0, "end_seconds": None} if kind == "artifact" else {}
    response = submit(f, kind, key=key, extra=extra)
    assert body(response, 202) == first and response.headers["Idempotency-Replayed"] == "true"
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (f.members.workspace_id, f.members.alice_id)))
    assert submit(f, kind, key=key).status_code == 404
    assert count(f.case, m.Job) == 2
    with f.case.database.snapshot_session() as session:
        receipts = session.scalars(select(m.ApiIdempotency).where(m.ApiIdempotency.idempotency_key == key)).all()
        assert {r.principal_subject for r in receipts} == {
            f"workspace:{f.members.workspace_id}:user:{uid}" for uid in (f.members.alice_id, f.members.bob_id)}


@pytest.mark.parametrize("loss,kind,key", [
    ("membership", "preview", "normalization-rights-drift"),
    ("revision", "artifact", None), ("source", "artifact", "normalization-source-drift"),
])
def test_final_writer_fences_rights_cas_and_source_header(normalization, monkeypatch, loss, kind, key):
    f = normalization
    real_hash = routes.sha256_file
    altered = []

    def hash_then_change(path):
        value = real_hash(path)
        if not altered:
            altered.append(True)
            with f.case.database.immediate_session() as session:
                if loss == "membership":
                    session.delete(session.get_one(m.WorkspaceMembership, (f.members.workspace_id, f.members.alice_id)))
                elif loss == "revision":
                    session.get_one(m.Voice, f.voice["id"]).revision += 1
                else:
                    session.get_one(m.Artifact, f.aid).role = "changed_after_preflight"
        return value

    monkeypatch.setattr(routes, "sha256_file", hash_then_change)
    response = submit(f, kind, key=key)
    assert response.status_code == (404 if loss == "membership" else 409), response.get_json()
    assert count(f.case, m.Job) == 0
    with f.case.database.snapshot_session() as session:
        assert not session.scalar(select(m.ApiIdempotency.id).where(m.ApiIdempotency.idempotency_key == key))
    assert f.original.exists()


def test_membership_loss_during_real_streaming_cleans_only_new_upload(normalization, monkeypatch):
    f = normalization
    prior_artifacts = count(f.case, m.Artifact)
    prior_samples = count(f.case, m.VoiceSample)
    real_save = FileStorage.save
    temporary = []

    def save_then_leave(incoming, destination, *args, **kwargs):
        real_save(incoming, destination, *args, **kwargs)
        temporary.append(Path(destination))
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.WorkspaceMembership, (f.members.workspace_id, f.members.alice_id)))

    monkeypatch.setattr(FileStorage, "save", save_then_leave)
    assert submit(f, "replace").status_code == 404
    assert len(temporary) == 1 and not temporary[0].exists()
    assert f.original.exists() and count(f.case, m.Job) == 0
    assert count(f.case, m.Artifact) == prior_artifacts and count(f.case, m.VoiceSample) == prior_samples
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.Voice, f.voice["id"]).revision == 1


def test_direct_user_cannot_omit_explicit_access_context(normalization, monkeypatch):
    f = normalization
    actor, _token = api_actor(f.case, f.members.alice_id, f.members.workspace_id)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Missing access context reached incoming.save")

    monkeypatch.setattr(FileStorage, "save", forbidden)
    with execution_actor_scope(actor), pytest.raises(AccessDenied):
        uploads.queue_voice_recording(f.case.database, f.services.paths, f.services.artifacts,
            f.services.jobs, FileStorage(io.BytesIO(silent_wav()), filename="direct.wav"),
            voice_id=f.voice["id"], workspace_id=f.members.workspace_id,
            expected_revision=1, noise_reduction="none")


def test_ready_preview_reuse_adds_actual_workspace_without_new_job(normalization):
    f = normalization
    body(submit(f, "preview"), 202)
    run(f)
    result = body(submit(f, "preview", revision=2, key="normalization-ready-reuse"))
    assert result["reused_reference"] is True and result["workspace_id"] == f.members.workspace_id
    assert count(f.case, m.Job) == 1
    assert body(submit(f, "preview", revision=2, key="normalization-ready-reuse")) == result


def test_legacy_receipt_without_workspace_replays_exactly_and_explicit_mismatch_denies(case):
    services = case.app.extensions["pandrator"]["services"]
    csrf = {"X-CSRF-Token": case.csrf}
    voice = body(create(case.client, csrf), 201)
    _sid, artifact, _path = sample(case, voice)
    route = f"/api/v1/voices/{voice['id']}/samples/from-artifact"
    requested = {"artifact_id": artifact, "expected_voice_revision": 1}
    request_headers = headers(csrf, "normalization-legacy-retry")
    first = body(case.client.post(route, json=requested, headers=request_headers), 202)
    with case.database.immediate_session() as session:
        receipt = session.scalar(select(m.ApiIdempotency).where(m.ApiIdempotency.idempotency_key == "normalization-legacy-retry"))
        receipt.response_json = {k: v for k, v in first.items() if k != "workspace_id"}
        session.get_one(m.Artifact, artifact).state = "deleted"
    legacy = case.client.post(route, json=requested, headers=request_headers)
    assert body(legacy, 202) == {k: v for k, v in first.items() if k != "workspace_id"}
    assert legacy.headers["Idempotency-Replayed"] == "true"
    with case.database.immediate_session() as session:
        receipt = session.scalar(select(m.ApiIdempotency).where(m.ApiIdempotency.idempotency_key == "normalization-legacy-retry"))
        receipt.response_json = {**receipt.response_json, "workspace_id": "another-workspace"}
    assert case.client.post(route, json=requested, headers=request_headers).status_code == 404
    assert count(case, m.Job) == 1 and services is not None
