"""Disposable compatible-video preview and publication checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pandrator.web import video_previews
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import Worker
from pandrator.web.media_process import (
    MediaProcessCancelled,
    MediaProcessError,
    MediaProcessResult,
    MediaProcessTimeout,
)
from pandrator.web.models import Artifact, ArtifactEdge, Job
from pandrator.web.session_purge import SessionPurgeService
from pandrator.web.video_previews import (
    PREVIEW_ROLE,
    PREVIEW_VERSION,
    VideoInfo,
    VideoPreviewService,
    VideoPreviewUnsupported,
    probe_video,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def harness(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()})
    extension = app.extensions["pandrator"]
    session = extension["sessions"].create("Disposable video preview")
    folder = paths.sessions / session.storage_key
    folder.mkdir(parents=True)
    service = VideoPreviewService(extension["database"], paths, extension["artifacts"], extension["jobs"])
    yield SimpleNamespace(paths=paths, app=app, client=client, extension=extension,
                          database=extension["database"], artifacts=extension["artifacts"],
                          session=session, folder=folder, service=service,
                          handlers=extension["workflow_handlers"])
    extension["database"].dispose()


@pytest.fixture
def ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg or FFprobe is unavailable")
    return executable


def _source(harness, content=b"source", *, name="source.mkv", owner=True):
    path = harness.folder / name if owner else harness.paths.artifacts / name
    path.write_bytes(content)
    artifact = harness.artifacts.register(path, kind="video", role="upload",
                                          session_id=harness.session.id if owner else None)
    return artifact, path


def _real_source(harness, ffmpeg, *, audio=True, large=False):
    path = harness.folder / ("source-av.mkv" if audio else "source-video.mkv")
    dimensions = "641x721" if large else "96x64"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-filter_threads", "1",
               "-f", "lavfi", "-i", f"testsrc=size={dimensions}:rate=40:duration=0.5"]
    if audio:
        subtitles = harness.folder / "fixture.srt"
        subtitles.write_text("1\n00:00:00,000 --> 00:00:00,400\nPrivate subtitle text.\n")
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
                    "-i", str(subtitles), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
                    "-c:a", "pcm_s16le", "-c:s", "srt"]
    command += ["-c:v", "ffv1", "-pix_fmt", "bgr0", "-threads", "1",
                "-metadata", "title=private source title", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=20)
    artifact = harness.artifacts.register(path, kind="video", role="upload", session_id=harness.session.id)
    return artifact, path


def _request(harness, source):
    return harness.client.get(f"/api/v1/artifacts/{source.id}/video-preview")


def _generate(harness, source, cancel_event=None):
    return harness.handlers.generate_video_preview(
        {"source_artifact_id": source.id, "source_content_hash": source.content_hash,
         "preview_version": PREVIEW_VERSION}, lambda *_args: None, cancel_event or threading.Event(),
    )


def _mock_video(harness, monkeypatch, *, info=None, output=None, content=b"preview", failure=None):
    info = info or VideoInfo(1.0, 96, 64, 25.0, True, "ffv1", "bgr0", "pcm_s16le")
    output = output or VideoInfo(1.0, 96, 64, 25.0, True, "h264", "yuv420p", "aac")
    calls = []

    def probe(path, cancel_event=None):
        return output if path.name.startswith(".video-preview-") else info

    def transcode(command, **kwargs):
        calls.append((command, kwargs))
        destination = harness.paths.root / command[-1]
        destination.write_bytes(content)
        if failure is not None:
            raise failure
        return MediaProcessResult(0)

    monkeypatch.setattr(video_previews, "probe_video", probe)
    monkeypatch.setattr(video_previews, "run_media_process", transcode)
    return calls


@pytest.mark.parametrize("audio", [True, False])
def test_real_codec_conversion_source_integrity_lineage_and_ready_cache(harness, ffmpeg, audio):
    source, source_path = _real_source(harness, ffmpeg, audio=audio, large=audio)
    original = source_path.read_bytes()
    queued = _request(harness, source)
    assert queued.status_code == 202, queued.get_json()
    assert _request(harness, source).get_json() == queued.get_json()
    with harness.database.session() as session:
        job = session.get(Job, queued.get_json()["job_id"])
        assert job.kind == "video.preview" and job.session_id == source.session_id
        assert job.resource_keys_json == [f"artifact:video-preview:{source.id}"]
        payload = dict(job.payload_json)
        assert payload["source_content_hash"] == source.content_hash
    result = harness.handlers.generate_video_preview(payload, lambda *_args: None, threading.Event())
    with harness.database.session() as session:
        preview = session.get(Artifact, result["artifact_id"])
        assert preview.role == PREVIEW_ROLE and preview.kind == "video"
        assert preview.session_id == source.session_id and preview.mime_type == "video/mp4"
        assert preview.metadata_json["source_content_hash"] == source.content_hash
        assert preview.metadata_json["preview_version"] == PREVIEW_VERSION
        assert session.get(ArtifactEdge, (source.id, preview.id)) is not None
        output = harness.paths.root / preview.relative_path
    info = probe_video(output)
    assert info.video_codec == "h264" and info.pixel_format == "yuv420p"
    assert 0 < info.frame_rate <= 30.001 and info.height <= 720 and info.width % 2 == 0
    assert info.has_audio == audio
    if audio:
        assert info.audio_codec == "aac"
    assert source_path.read_bytes() == original
    with harness.database.session() as session:
        assert session.get(Artifact, source.id).content_hash == source.content_hash
    ready = _request(harness, source)
    assert ready.status_code == 200, ready.get_json()
    assert ready.get_json() == {"status": "ready", "artifact_id": result["artifact_id"],
                               "content_url": f"/api/v1/artifacts/{result['artifact_id']}/content",
                               "mime_type": "video/mp4"}
    content = harness.client.get(ready.get_json()["content_url"])
    assert content.status_code == 200 and content.mimetype == "video/mp4"
    metadata = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_format", "-show_streams",
                               "-of", "json", str(output)], capture_output=True, text=True, check=True, timeout=10)
    payload = json.loads(metadata.stdout)
    assert "title" not in (payload["format"].get("tags") or {})
    assert {stream["codec_type"] for stream in payload["streams"]} <= {"video", "audio"}
    assert output.read_bytes().index(b"moov") < output.read_bytes().index(b"mdat")


def test_request_rejects_audio_only_malformed_deleted_missing_and_unauthenticated(harness, ffmpeg):
    audio_path = harness.folder / "audio.wav"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=duration=0.2", str(audio_path)], check=True, capture_output=True, timeout=10)
    audio = harness.artifacts.register(audio_path, kind="audio", role="upload", session_id=harness.session.id)
    source, _path = _source(harness)
    for candidate in (audio, source):
        response = _request(harness, candidate)
        assert response.status_code == 422
        assert "audio playback" in response.get_json()["error"]["message"]
    assert harness.app.test_client().get(f"/api/v1/artifacts/{source.id}/video-preview").status_code == 401
    with harness.database.session() as session:
        session.get(Artifact, source.id).state = "deleted"
    assert _request(harness, source).status_code == 404
    missing, path = _source(harness, name="missing.mkv")
    path.unlink()
    assert _request(harness, missing).status_code == 410
    with harness.database.session() as session:
        assert not list(session.scalars(select(Job).where(Job.kind == "video.preview")))


@pytest.mark.parametrize("duration", [None, "N/A", "0", "-1", "nan", "inf", "14400.01"])
def test_source_duration_must_be_known_finite_positive_and_bounded(harness, monkeypatch, duration):
    source, _path = _source(harness)
    payload = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 96,
                             "height": 64, "duration": duration}], "format": {"duration": duration}}
    monkeypatch.setattr(video_previews, "run_media_process", lambda *_args, **_kwargs: MediaProcessResult(0, json.dumps(payload)))
    response = _request(harness, source)
    assert response.status_code == 422
    assert "finite duration" in response.get_json()["error"]["message"]


def test_duration_upper_boundary_and_first_stream_qualification(harness, monkeypatch):
    _source_record, path = _source(harness)
    payload = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 96,
                             "height": 64, "disposition": {"attached_pic": 0}}],
               "format": {"duration": "14400"}}
    monkeypatch.setattr(video_previews, "run_media_process", lambda *_args, **_kwargs: MediaProcessResult(0, json.dumps(payload)))
    assert probe_video(path).duration_seconds == 14400
    payload["streams"][0]["disposition"]["attached_pic"] = 1
    with pytest.raises(VideoPreviewUnsupported, match="No usable first video"):
        probe_video(path)


def test_concurrent_requests_deduplicate_by_hash_and_version_and_keep_owner(harness, monkeypatch):
    source, _path = _source(harness)
    _mock_video(harness, monkeypatch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(harness.service.request(source.id)), daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2 and results[0] == results[1] and results[0][1] == 202
    with harness.database.session() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "video.preview")))
        assert len(jobs) == 1 and jobs[0].session_id == source.session_id
    purge = SessionPurgeService(harness.database, harness.paths)
    harness.extension["sessions"].trash(harness.session.id, harness.session.revision)
    preview = purge.preview(harness.session.id)
    assert not preview["can_purge"] and "unfinished:jobs:queued" in preview["blockers"]


def test_cache_requires_parent_hash_version_state_and_existing_file(harness, monkeypatch):
    source, _path = _source(harness)
    _mock_video(harness, monkeypatch)
    result = _generate(harness, source)
    cached = _request(harness, source)
    assert cached.status_code == 200
    with harness.database.session() as session:
        preview = session.get(Artifact, result["artifact_id"])
        output = harness.paths.root / preview.relative_path
        original_metadata = dict(preview.metadata_json)
    for field, wrong in (("source_content_hash", "wrong"), ("preview_version", "old"), ("source_artifact_id", "other")):
        with harness.database.session() as session:
            session.get(Artifact, result["artifact_id"]).metadata_json = {**original_metadata, field: wrong}
        assert _request(harness, source).status_code == 202
        assert output.read_bytes() == b"preview"
    with harness.database.session() as session:
        preview = session.get(Artifact, result["artifact_id"])
        preview.metadata_json = original_metadata
        preview.state = "stale"
    assert _request(harness, source).status_code == 202
    with harness.database.session() as session:
        session.get(Artifact, result["artifact_id"]).state = "current"
    output.unlink()
    assert _request(harness, source).status_code == 202


def test_unbound_preview_stays_unbound_and_old_source_hash_does_not_deduplicate(harness, monkeypatch):
    source, path = _source(harness, owner=False)
    _mock_video(harness, monkeypatch)
    queued, _ = harness.service.request(source.id)
    path.write_bytes(b"changed source")
    import hashlib

    changed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with harness.database.session() as session:
        session.get(Artifact, source.id).content_hash = changed_hash
    second, status = harness.service.request(source.id)
    assert status == 202 and second["job_id"] != queued["job_id"]
    with harness.database.session() as session:
        job = session.get(Job, second["job_id"])
        assert job.session_id is None and job.payload_json["source_content_hash"] == changed_hash
    result = harness.service.generate(job.payload_json, lambda *_args: None, threading.Event())
    with harness.database.session() as session:
        preview = session.get(Artifact, result["artifact_id"])
        assert preview.session_id is None and preview.relative_path.startswith("artifacts/video-previews/")


@pytest.mark.parametrize("failure", [MediaProcessCancelled("cancelled"), MediaProcessError("encoder failed"), MediaProcessTimeout("timeout")])
def test_cancel_process_failure_and_timeout_clean_partial_and_allow_retry(harness, monkeypatch, failure):
    source, path = _source(harness)
    calls = _mock_video(harness, monkeypatch, failure=failure)
    with pytest.raises(type(failure)):
        _generate(harness, source)
    assert not list((harness.folder / "video-previews").glob("*"))
    assert path.read_bytes() == b"source"
    command, options = calls[0]
    assert options["timeout_seconds"] == 1800
    assert command[command.index("-fs") + 1] == str(2 * 1024 * 1024 * 1024)
    _mock_video(harness, monkeypatch)
    assert _generate(harness, source)["artifact_id"]


@pytest.mark.parametrize("problem", ["duration", "dimensions", "fps", "codec", "audio", "empty", "size"])
def test_truncated_or_incompatible_output_is_rejected_and_removed(harness, monkeypatch, problem):
    source, _path = _source(harness)
    output = VideoInfo(1.0, 96, 64, 25.0, True, "h264", "yuv420p", "aac")
    changes = {"duration": {"duration_seconds": 0.7}, "dimensions": {"height": 722},
               "fps": {"frame_rate": 31}, "codec": {"video_codec": "mpeg4"}, "audio": {"has_audio": False}}
    content = b"preview"
    if problem in changes:
        output = replace(output, **changes[problem])
    elif problem == "empty":
        content = b""
    elif problem == "size":
        monkeypatch.setattr(video_previews, "MAX_OUTPUT_BYTES", 4)
    _mock_video(harness, monkeypatch, output=output, content=content)
    with pytest.raises(MediaProcessError):
        _generate(harness, source)
    assert not list((harness.folder / "video-previews").glob("*"))
    with harness.database.session() as session:
        assert not list(session.scalars(select(Artifact).where(Artifact.role == PREVIEW_ROLE)))


def test_registration_failure_rolls_back_rows_and_removes_only_new_preview(harness, monkeypatch):
    source, source_path = _source(harness)
    _mock_video(harness, monkeypatch)
    previous = _generate(harness, source)
    with harness.database.session() as session:
        old = session.get(Artifact, previous["artifact_id"])
        old_path = harness.paths.root / old.relative_path
    monkeypatch.setattr(video_previews, "PREVIEW_VERSION", "v2")
    registration = harness.artifacts.register_in_session

    def fail_registration(*args, **kwargs):
        registration(*args, **kwargs)
        raise RuntimeError("registration interrupted")

    monkeypatch.setattr(harness.artifacts, "register_in_session", fail_registration)
    with pytest.raises(RuntimeError, match="registration interrupted"):
        harness.service.generate({"source_artifact_id": source.id, "source_content_hash": source.content_hash,
                                  "preview_version": "v2"}, lambda *_args: None, threading.Event())
    assert old_path.read_bytes() == b"preview" and source_path.read_bytes() == b"source"
    assert list((harness.folder / "video-previews").glob("*")) == [old_path]
    with harness.database.session() as session:
        assert len(list(session.scalars(select(Artifact).where(Artifact.role == PREVIEW_ROLE)))) == 1


def test_missing_source_hash_is_fingerprinted_without_mutating_source_row(harness, monkeypatch):
    source, path = _source(harness)
    with harness.database.session() as session:
        session.get(Artifact, source.id).content_hash = None
    _mock_video(harness, monkeypatch)
    queued, status = harness.service.request(source.id)
    assert status == 202
    with harness.database.session() as session:
        assert session.get(Artifact, source.id).content_hash is None
        job_payload = dict(session.get(Job, queued["job_id"]).payload_json)
    result = harness.service.generate(job_payload, lambda *_args: None, threading.Event())
    with harness.database.session() as session:
        preview = session.get(Artifact, result["artifact_id"])
        assert preview.metadata_json["source_content_hash"] == source.content_hash
        assert session.get(Artifact, source.id).content_hash is None
    assert path.read_bytes() == b"source"


@pytest.mark.parametrize("unsafe", ["traversal", "symlink", "nonregular"])
def test_only_regular_managed_source_files_are_eligible(harness, monkeypatch, unsafe):
    source, path = _source(harness)
    sentinel = harness.paths.root.parent / "outside-preview-source"
    sentinel.write_bytes(b"retain")
    if unsafe == "traversal":
        with harness.database.session() as session:
            session.get(Artifact, source.id).relative_path = "../outside-preview-source"
    else:
        path.unlink()
        if unsafe == "symlink":
            path.symlink_to(sentinel)
        else:
            path.mkdir()
    response = _request(harness, source)
    assert response.status_code == 422
    assert sentinel.read_bytes() == b"retain"


def test_cache_needs_parent_edge_and_successful_registration_commit(harness, monkeypatch):
    source, _path = _source(harness)
    _mock_video(harness, monkeypatch)
    unrelated = harness.folder / "unrelated.mp4"
    unrelated.write_bytes(b"valid historical preview")
    previous = harness.artifacts.register(unrelated, kind="video", role=PREVIEW_ROLE,
                                          session_id=source.session_id,
                                          metadata={"source_artifact_id": source.id,
                                                    "source_content_hash": source.content_hash,
                                                    "preview_version": PREVIEW_VERSION})
    assert _request(harness, source).status_code == 202
    immediate = harness.database.immediate_session

    @contextmanager
    def interrupted_commit():
        with immediate() as session:
            yield session
            raise RuntimeError("commit interrupted")

    monkeypatch.setattr(harness.database, "immediate_session", interrupted_commit)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        _generate(harness, source)
    assert unrelated.read_bytes() == b"valid historical preview"
    assert not list((harness.folder / "video-previews").glob("*"))
    with harness.database.session() as session:
        previews = list(session.scalars(select(Artifact).where(Artifact.role == PREVIEW_ROLE)))
        assert len(previews) == 1 and previews[0].id == previous.id
    monkeypatch.setattr(harness.database, "immediate_session", immediate)
    assert _generate(harness, source)["artifact_id"]


def test_handler_binding_uses_existing_durable_registry(harness, monkeypatch):
    calls = []

    def generate(payload, _progress, _cancel_event):
        calls.append(payload)
        return {"artifact_id": "preview"}

    monkeypatch.setattr(harness.handlers, "generate_video_preview", generate)
    payload = {"source_artifact_id": "source", "source_content_hash": "hash", "preview_version": PREVIEW_VERSION}
    result = harness.handlers.handlers()["video.preview"](payload, lambda *_args: None, threading.Event())
    assert result == {"artifact_id": "preview"} and calls == [payload]


def test_durable_worker_failure_retry_ready_and_owned_purge(harness, monkeypatch):
    source, source_path = _source(harness)
    _mock_video(harness, monkeypatch, failure=MediaProcessError("encoder interrupted"))
    first = _request(harness, source).get_json()
    worker = Worker(harness.extension["jobs"], "disposable-video-preview-worker", harness.handlers.handlers())
    assert worker.run_once()
    with harness.database.session() as session:
        failed = session.get(Job, first["job_id"])
        assert failed.status == "failed" and "encoder interrupted" in failed.error_message
    assert source_path.read_bytes() == b"source"
    _mock_video(harness, monkeypatch)
    retry = _request(harness, source)
    assert retry.status_code == 202 and retry.get_json()["job_id"] != first["job_id"]
    assert worker.run_once()
    with harness.database.session() as session:
        completed = session.get(Job, retry.get_json()["job_id"])
        assert completed.status == "succeeded"
        preview_id = completed.result_json["artifact_id"]
    ready = _request(harness, source)
    assert ready.status_code == 200 and ready.get_json()["artifact_id"] == preview_id
    harness.extension["sessions"].trash(harness.session.id, harness.session.revision)
    purge = SessionPurgeService(harness.database, harness.paths)
    impact = purge.preview(harness.session.id)
    assert impact["can_purge"] and impact["owned_file_count"] == 2
    assert purge.purge(harness.session.id, impact["revision"], impact["impact_token"])["state"] == "complete"
    assert not source_path.exists()
    with harness.database.session() as session:
        assert session.get(Artifact, preview_id) is None
