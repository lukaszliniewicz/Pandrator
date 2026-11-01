"""Real media workers recheck authority and preserve earlier output files."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

from pandrator.web import media_process, video_previews, waveform
from pandrator.web.accounts.execution_actor import (
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from pandrator.web.media_process import MediaProcessResult
from pandrator.web.models import ApiToken, Artifact, ArtifactEdge, Job, new_id, utcnow
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.video_previews import PREVIEW_VERSION, VideoInfo
from pandrator.web.waveform import WaveformPeaks
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential
from tests.test_job_access_routes import no_network as no_network

KINDS = ("audio.preview", "audio.waveform", "video.preview")


def inputs(case, kind):
    path = case.services.paths.artifacts / f"{new_id()}.wav"
    path.write_bytes(b"synthetic original source")
    source = case.services.artifacts.register(
        path,
        kind="audio",
        role="source",
        session_id=case.a.id,
    )
    previous_path = case.services.paths.artifacts / f"{new_id()}.bin"
    previous_path.write_bytes(b"retain previous output bytes")
    role = {
        "audio.preview": "source_audio_preview",
        "audio.waveform": "waveform_peaks",
        "video.preview": "source_video_preview",
    }[kind]
    previous = case.services.artifacts.register(
        previous_path,
        kind="audio",
        role=role,
        session_id=case.a.id,
        parent_ids=[source.id],
        metadata={"preview_version": "previous"},
    )
    with case.database.immediate_session() as session:
        session.get(Artifact, previous.id).state = "stale"
    return source, previous, previous_path


def enqueue(case, caller, source, kind):
    payload = {"source_artifact_id": source.id}
    if kind == "audio.waveform":
        payload.update(max_points=128, start_ms=0, end_ms=500)
    elif kind == "video.preview":
        payload.update(source_content_hash=source.content_hash, preview_version=PREVIEW_VERSION)
    with execution_actor_scope(caller.actor):
        return case.services.jobs.enqueue(
            kind,
            payload,
            workspace_id=source.workspace_id,
            project_id=source.project_id,
            session_id=None if kind == "audio.preview" else source.session_id,
            resource_keys=[f"synthetic-media:{source.id}"],
        ).id


def media_files(case):
    return {
        str(path.relative_to(case.services.paths.root)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for directory in (case.services.paths.artifacts, case.services.paths.sessions)
        for path in directory.rglob("*")
        if path.is_file()
    }


def fake_media(case, monkeypatch, after_media=None):
    calls = []

    def check():
        actor = current_execution_actor.get()
        assert type(actor) is UserActor and actor.user_id == case.users["editor"]
        assert current_job_lease.get() is not None
        calls.append(actor.user_id)
        if after_media is not None:
            after_media()

    def transcode(command, **_kwargs):
        Path(command[-1]).write_bytes(b"independent new output")
        check()
        return MediaProcessResult(0)

    def peaks(*_args, **_kwargs):
        check()
        return WaveformPeaks(500, 0, 500, 1, (0.5,), 8000)

    info = VideoInfo(0.5, 96, 64, 24, False, "h264", "yuv420p", None)
    monkeypatch.setattr(media_process, "resolve_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(video_previews, "resolve_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(media_process, "run_media_process", transcode)
    monkeypatch.setattr(video_previews, "run_media_process", transcode)
    monkeypatch.setattr(video_previews, "probe_video", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(waveform, "generate_waveform_peaks", peaks)
    return calls


def worker(case):
    return Worker(
        case.services.jobs, "media-authority-worker", case.services.workflow_handlers.handlers()
    )


def assert_previous(case, previous, path):
    assert path.read_bytes() == b"retain previous output bytes"
    with case.database.snapshot_session() as session:
        retained = session.get(Artifact, previous.id)
        assert retained.relative_path == previous.relative_path
        assert retained.content_hash == previous.content_hash
        assert retained.metadata_json == {"preview_version": "previous"}
        assert retained.state == "stale"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("expired_transport", [False, True])
def test_current_user_worker_publishes_distinct_output_and_keeps_natural_expiry(
    case,
    monkeypatch,
    kind,
    expired_transport,
):
    caller = credential(case, "editor")
    source, previous, previous_path = inputs(case, kind)
    identifier = enqueue(case, caller, source, kind)
    if expired_transport:
        with case.database.immediate_session() as session:
            session.get(ApiToken, caller.token.id).expires_at = utcnow() - timedelta(seconds=1)
    calls = fake_media(case, monkeypatch)
    assert worker(case).run_once()
    assert calls == [case.users["editor"]]
    assert_previous(case, previous, previous_path)
    with case.database.snapshot_session() as session:
        job = session.get(Job, identifier)
        assert job.status == "succeeded", job.error_message
        output = session.get(Artifact, job.result_json["artifact_id"])
        assert output.id != previous.id
        assert output.relative_path != previous.relative_path
        assert (output.workspace_id, output.project_id, output.session_id) == (
            source.workspace_id,
            source.project_id,
            source.session_id,
        )
        path = case.services.paths.managed_path(output.relative_path)
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == output.content_hash
        if kind != "video.preview":
            assert isinstance(output.settings_hash, str) and len(output.settings_hash) == 64
        assert (
            session.scalar(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == output.id,
                )
            )
            == source.id
        )


@pytest.mark.parametrize("kind", KINDS)
def test_revoked_membership_blocks_worker_before_media_effects(case, monkeypatch, kind):
    caller = credential(case, "editor")
    source, previous, previous_path = inputs(case, kind)
    identifier = enqueue(case, caller, source, kind)
    with case.database.immediate_session() as session:
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["editor"],
            )
        )
    before = media_files(case)
    calls = fake_media(case, monkeypatch)
    assert worker(case).run_once()
    assert calls == []
    assert media_files(case) == before
    assert_previous(case, previous, previous_path)
    with case.database.snapshot_session() as session:
        assert session.get(Job, identifier).status == "failed"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("loss", ["membership", "lease"])
def test_postmedia_authority_loss_prevents_publication_and_preserves_old_files(
    case,
    monkeypatch,
    kind,
    loss,
):
    caller = credential(case, "editor")
    source, previous, previous_path = inputs(case, kind)
    identifier = enqueue(case, caller, source, kind)
    with case.database.snapshot_session() as session:
        baseline_ids = set(session.scalars(select(Artifact.id)))
    before = media_files(case)

    def revoke():
        with case.database.immediate_session(busy_timeout_ms=0) as session:
            if loss == "membership":
                session.execute(
                    delete(ProjectMembership).where(
                        ProjectMembership.project_id == case.a.project_id,
                        ProjectMembership.user_id == case.users["editor"],
                    )
                )
            else:
                session.execute(
                    update(Job)
                    .where(Job.id == identifier)
                    .values(
                        lease_owner="replacement-worker",
                        lease_generation=Job.lease_generation + 1,
                        lease_expires_at=utcnow() + timedelta(hours=1),
                    )
                )

    calls = fake_media(case, monkeypatch, revoke)
    assert worker(case).run_once()
    assert calls == [case.users["editor"]]
    assert_previous(case, previous, previous_path)
    assert media_files(case) == before
    with case.database.snapshot_session() as session:
        assert set(session.scalars(select(Artifact.id))) == baseline_ids
        assert session.get(Job, identifier).status != "succeeded"
