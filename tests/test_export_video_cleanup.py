"""Video preparation and rendering share one bounded scratch-file lifetime."""

import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.jobs import Worker
from pandrator.web.models import Artifact, ArtifactEdge, SessionStageSelection, utcnow
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class VideoExportCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = prepare_web_test_data_root(temporary.name)
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.session = SessionService(self.database).create(
            "Video cleanup", workflow_kind="voiceover", source_language="en"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()
        self.output_dir = self.session_dir / "exports"
        self.source_path = self.session_dir / "source.mp4"
        self.source_path.write_bytes(b"original source video")
        self.source = self.artifacts.register(
            self.source_path, kind="source", role="upload", session_id=self.session.id
        )
        self.speech_path = self.session_dir / "speech.wav"
        self.speech_path.write_bytes(b"original generated speech")
        self.speech = self.artifacts.register(
            self.speech_path, kind="audio", role="assembled_audio", session_id=self.session.id
        )
        self.retained_path = self.session_dir / "intermediates" / "retained.wav"
        self.retained_path.parent.mkdir()
        self.retained_path.write_bytes(b"previously registered soundtrack")
        self.retained = self.artifacts.register(
            self.retained_path, kind="audio", role="soundtrack_master", session_id=self.session.id
        )
        self.settings = {
            "export_mode": "media",
            "audio_mode": "dubbing_only",
            "subtitle_mode": "none",
            "video_tail_extension_policy": "extend",
        }
        self.failure_stage = None
        self.cancel_stage = None
        self.cancel_event = threading.Event()
        self.cancel_on_ready = False
        self.command_outputs = []
        self.enterContext(mock.patch.dict("os.environ", {
            "PANDRATOR_FFMPEG_EXE": "ffmpeg",
            "PANDRATOR_FFPROBE_EXE": "ffprobe",
        }))
        self.enterContext(mock.patch(
            "pandrator.logic.dubbing.audio_sync.media_has_audio_stream", return_value=True
        ))
        self.enterContext(mock.patch(
            "pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}
        ))
        self.enterContext(mock.patch(
            "pandrator.web.soundtrack_export.probe_soundtrack_media", side_effect=self.probe
        ))
        self.enterContext(mock.patch(
            "pandrator.web.export_video_commands.run_cancellable", side_effect=self.run_media_command
        ))

    def probe(self, path):
        is_video = Path(path) == self.source_path
        return {
            "duration": 1.0 if is_video else 2.0,
            "video_duration": 1.0 if is_video else None,
            "has_video": is_video,
            "has_audio": True,
            "fps": 25.0 if is_video else None,
        }

    def run_media_command(self, command, **_kwargs):
        destination = Path(command[-1])
        self.command_outputs.append(destination)
        destination.write_bytes(b"rendered or partial media")
        if self.cancel_stage and f"-{self.cancel_stage}-" in destination.name:
            self.cancel_event.set()
        if self.failure_stage and f"-{self.failure_stage}-" in destination.name:
            raise subprocess.CalledProcessError(1, command, stderr="injected encoder failure")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def export(self):
        def progress(value, _detail):
            if self.cancel_on_ready and value == 0.9:
                self.cancel_event.set()

        return self.handlers.export(
            {"session_id": self.session.id, "settings": self.settings},
            progress,
            self.cancel_event,
        )

    def assert_clean(self, *, published=False, expect_commands=True):
        self.assertEqual([], [
            str(path.relative_to(self.output_dir))
            for path in self.output_dir.rglob("*")
            if path.name.startswith(".")
        ])
        self.assertEqual(expect_commands, bool(self.command_outputs))
        for output in self.command_outputs:
            self.assertFalse(output.exists(), str(output))
        self.assertEqual(b"original source video", self.source_path.read_bytes())
        self.assertEqual(b"original generated speech", self.speech_path.read_bytes())
        retained, path = self.artifacts.resolve(self.retained.id)
        self.assertEqual("current", retained.state)
        self.assertEqual(b"previously registered soundtrack", path.read_bytes())
        with self.database.session() as session:
            exports = list(session.scalars(select(Artifact).where(Artifact.kind == "export")))
        self.assertEqual(1 if published else 0, len(exports))
        if not published:
            self.assertEqual([], list((self.output_dir / "video").glob("*.mp4")))

    def test_precanceled_video_never_starts_rendering(self):
        self.cancel_event.set()
        with self.assertRaises(InterruptedError):
            self.export()
        self.assert_clean(expect_commands=False)

    def test_cancel_after_tail_never_starts_audio_replacement(self):
        self.cancel_stage = "tail"
        with self.assertRaises(InterruptedError):
            self.export()
        self.assertEqual(1, len(self.command_outputs))
        self.assert_clean()

    def test_cancel_after_audio_replacement_never_starts_final_render(self):
        self.cancel_stage = "audio"
        with self.assertRaises(InterruptedError):
            self.export()
        self.assertEqual(2, len(self.command_outputs))
        self.assert_clean()

    def test_cancel_after_final_render_never_promotes_video(self):
        self.cancel_stage = "render"
        with self.assertRaises(InterruptedError):
            self.export()
        self.assertEqual(3, len(self.command_outputs))
        self.assert_clean()

    def test_cancel_before_registration_removes_unpublished_video(self):
        self.cancel_on_ready = True
        with self.assertRaises(InterruptedError):
            self.export()
        self.assert_clean()

    def add_subtitle(self):
        path = self.session_dir / "source.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:00,800\nA source line.\n", encoding="utf-8")
        self.settings["subtitle_mode"] = "soft"
        return self.artifacts.register(
            path, kind="srt", role="correction", session_id=self.session.id
        )

    def test_failed_tail_render_removes_partial_video(self):
        self.failure_stage = "tail"
        with self.assertRaises(subprocess.CalledProcessError):
            self.export()
        self.assert_clean()

    def test_failed_audio_replacement_removes_audio_and_completed_tail(self):
        self.failure_stage = "audio"
        with self.assertRaises(subprocess.CalledProcessError):
            self.export()
        self.assertEqual(2, len(self.command_outputs))
        self.assert_clean()

    def test_interrupted_mixed_soundtrack_removes_completed_tail(self):
        self.settings["audio_mode"] = "mixed"
        with mock.patch(
            "pandrator.web.soundtrack_export.ensure_soundtrack_master",
            side_effect=InterruptedError("Soundtrack export cancelled."),
        ):
            with self.assertRaises(InterruptedError):
                self.export()
        self.assert_clean()

    def test_destination_allocation_failure_removes_prepared_video(self):
        with mock.patch.object(
            self.handlers.artifacts, "next_available_path", side_effect=OSError("allocation failed")
        ):
            with self.assertRaisesRegex(OSError, "allocation failed"):
                self.export()
        # Final path allocation follows rendering, inside the publication guard.
        self.assertEqual(3, len(self.command_outputs))
        self.assert_clean()

    def test_subtitle_finalization_failure_removes_all_scratch(self):
        self.add_subtitle()

        def fail_finalization(_source, destination, _settings):
            destination.write_text("partial subtitle", encoding="utf-8")
            raise ValueError("subtitle finalization failed")

        with mock.patch(
            "pandrator.logic.dubbing.subtitle_finalization.finalize_srt_file",
            side_effect=fail_finalization,
        ):
            with self.assertRaisesRegex(ValueError, "subtitle finalization failed"):
                self.export()
        self.assert_clean()

    def test_failed_final_mux_cleans_scratch_and_retains_registered_sidecar(self):
        self.add_subtitle()
        self.failure_stage = "render"
        with self.assertRaisesRegex(RuntimeError, "Selectable-subtitle fallback"):
            self.export()
        self.assert_clean()
        with self.database.session() as session:
            sidecar = session.scalar(select(Artifact).where(
                Artifact.role == "video_subtitle_track_source"
            ))
            self.assertIsNotNone(sidecar)
            _record, path = self.artifacts.resolve(sidecar.id)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("WEBVTT"))

    def test_success_retains_final_output_sidecar_and_provenance(self):
        subtitle = self.add_subtitle()
        result = self.export()
        self.assert_clean(published=True)
        exported, path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(b"rendered or partial media", path.read_bytes())
        # One second of overrun plus the existing one-frame safety margin.
        self.assertEqual(1040, exported.metadata_json["tail_extension_ms"])
        self.assertIn("output_settings", exported.metadata_json)
        self.assertEqual(1, len(exported.metadata_json["subtitle_tracks"]))
        sidecar_id = exported.metadata_json["subtitle_tracks"][0]["artifact_id"]
        _sidecar, sidecar_path = self.artifacts.resolve(sidecar_id)
        self.assertTrue(sidecar_path.read_text(encoding="utf-8").startswith("WEBVTT"))
        with self.database.session() as session:
            parents = set(session.scalars(select(ArtifactEdge.parent_artifact_id).where(
                ArtifactEdge.child_artifact_id == exported.id
            )))
        self.assertEqual({self.source.id, self.speech.id, subtitle.id, sidecar_id}, parents)


@pytest.fixture
def video_publication_case():
    case = VideoExportCleanupTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@contextmanager
def lease_reclaimer(queue):
    """Advance the claim clock without letting a heartbeat inherit that clock."""
    clock_lock = threading.RLock()
    heartbeat = queue.heartbeat

    def guarded_heartbeat(*args, **kwargs):
        with clock_lock:
            return heartbeat(*args, **kwargs)

    def reclaim(worker_id):
        with clock_lock, mock.patch(
            "pandrator.web.jobs.utcnow", return_value=utcnow() + timedelta(minutes=2)
        ):
            return queue.claim(worker_id)

    with mock.patch.object(queue, "heartbeat", guarded_heartbeat):
        yield reclaim


@pytest.mark.parametrize("interruption", ["cancel", "lease"])
@pytest.mark.parametrize("boundary", ["before", "during", "after"])
def test_video_publication_respects_durable_job_boundary(video_publication_case, interruption, boundary):
    case = video_publication_case
    queue = case.handlers.jobs
    job = queue.enqueue("export.create", {
        "session_id": case.session.id, "settings": case.settings,
    }, session_id=case.session.id, max_attempts=2 if interruption == "lease" else 1)
    worker = Worker(queue, "original", {"export.create": case.handlers.export})
    artifacts = case.handlers.artifacts
    original_prepare = artifacts.prepare_registration
    original_register = artifacts.register_in_session
    publication_session = None
    interruptions = []
    interruption_threads = []
    interruption_errors = []
    with case.database.session() as session:
        before_selections = [(s.stage_key, s.artifact_id, s.revision)
                             for s in session.scalars(select(SessionStageSelection))]

    def interrupt():
        interruptions.append(interruption)
        if interruption == "cancel":
            assert queue.request_cancel(job.id).status == "cancel_requested"
        else:
            # Deterministic lease expiry, reclaimed through the real queue API.
            replacement = reclaim("replacement")
            assert replacement.id == job.id
            assert replacement.lease_generation == 2

    def prepare(path, **kwargs):
        result = original_prepare(path, **kwargs)
        if boundary == "before" and Path(path).suffix == ".mp4":
            interrupt()
        return result

    def register(session, path, **kwargs):
        nonlocal publication_session
        result = original_register(session, path, **kwargs)
        if kwargs.get("kind") == "export":
            publication_session = session
            if boundary == "during":
                started = threading.Event()

                def concurrently_interrupt():
                    started.set()
                    try:
                        interrupt()
                    except Exception as error:
                        interruption_errors.append(error)

                thread = threading.Thread(target=concurrently_interrupt)
                interruption_threads.append(thread)
                thread.start()
                assert started.wait(5)
        return result

    def after_commit_context(original):
        @contextmanager
        def wrapped():
            with original() as session:
                yield session
            if boundary == "after" and session is publication_session:
                interrupt()
            elif boundary == "during" and session is publication_session:
                # The publication writer already held the lock when the other
                # writer started. Wait for its post-publication transition.
                for thread in interruption_threads:
                    thread.join(10)
                    assert not thread.is_alive()
                assert not interruption_errors
        return wrapped

    with (
        lease_reclaimer(queue) as reclaim,
        mock.patch.object(artifacts, "prepare_registration", prepare),
        mock.patch.object(artifacts, "register_in_session", register),
        mock.patch.object(case.database, "session", after_commit_context(case.database.session)),
        mock.patch.object(case.database, "immediate_session", after_commit_context(case.database.immediate_session)),
    ):
        assert worker.run_once()
    assert interruptions == [interruption]
    finished = queue.get(job.id)
    if interruption == "cancel":
        assert finished.status == "canceled"
    else:
        assert finished.status == "running"
        assert finished.lease_owner == "replacement"
        assert finished.lease_generation == 2
    published = boundary != "before"
    case.assert_clean(published=published)
    with case.database.session() as session:
        after_selections = [(s.stage_key, s.artifact_id, s.revision)
                            for s in session.scalars(select(SessionStageSelection))]
        assert after_selections == before_selections
        exports = list(session.scalars(select(Artifact).where(Artifact.kind == "export")))
        if published:
            assert exports[0].state == "current"
            assert "output_settings" in exports[0].metadata_json
            parents = set(session.scalars(select(ArtifactEdge.parent_artifact_id).where(
                ArtifactEdge.child_artifact_id == exports[0].id
            )))
            assert parents == {case.source.id, case.speech.id}


def test_cancel_during_video_registration_rolls_back_publication(video_publication_case):
    case = video_publication_case
    register = case.handlers.artifacts.register_in_session

    def cancel_after_flush(session, path, **kwargs):
        artifact = register(session, path, **kwargs)
        if kwargs.get("kind") == "export":
            case.cancel_event.set()
        return artifact

    with mock.patch.object(case.handlers.artifacts, "register_in_session", cancel_after_flush):
        with pytest.raises(InterruptedError):
            case.export()
    case.assert_clean()


@pytest.mark.parametrize("failure", ["registration", "commit"])
def test_video_publication_failure_removes_uncommitted_file(video_publication_case, failure):
    case = video_publication_case
    original_register = case.handlers.artifacts.register_in_session
    publication_session = None

    def register(session, path, **kwargs):
        nonlocal publication_session
        artifact = original_register(session, path, **kwargs)
        if kwargs.get("kind") == "export":
            publication_session = session
            if failure == "registration":
                raise RuntimeError("S3 registration failure")
        return artifact

    def before_commit_context(original):
        @contextmanager
        def wrapped():
            with original() as session:
                yield session
                if failure == "commit" and session is publication_session:
                    raise RuntimeError("S3 commit failure")
        return wrapped

    with (
        mock.patch.object(case.handlers.artifacts, "register_in_session", register),
        mock.patch.object(case.database, "session", before_commit_context(case.database.session)),
        mock.patch.object(case.database, "immediate_session", before_commit_context(case.database.immediate_session)),
        pytest.raises(RuntimeError, match=f"S3 {failure} failure"),
    ):
        case.export()
    case.assert_clean()


def test_stale_video_attempt_cannot_overwrite_new_owner_output(video_publication_case):
    case = video_publication_case
    queue = case.handlers.jobs
    job = queue.enqueue("export.create", {
        "session_id": case.session.id, "settings": case.settings,
    }, session_id=case.session.id, max_attempts=2)
    worker = Worker(queue, "old", {"export.create": case.handlers.export})
    winner = []

    def render(command, **kwargs):
        result = case.run_media_command(command, **kwargs)
        if "-render-" in Path(command[-1]).name:
            claim = reclaim("new")
            assert claim.id == job.id and claim.lease_generation == 2
            # Seed the new owner's committed output at the pathname the old
            # attempt picked before rendering. Use real artifact registration.
            path = case.output_dir / "video" / "Video_cleanup.mp4"
            path.write_bytes(b"new owner output")
            artifact = case.artifacts.register(
                path, kind="export", role="export", session_id=case.session.id,
                parent_ids=[case.source.id, case.speech.id],
            )
            winner.append((artifact.id, path, artifact.content_hash))
        return result

    with lease_reclaimer(queue) as reclaim, mock.patch("pandrator.web.export_video_commands.run_cancellable", render):
        assert worker.run_once()
    artifact_id, path, content_hash = winner[0]
    assert path.read_bytes() == b"new owner output"
    artifact, resolved = case.artifacts.resolve(artifact_id)
    assert resolved == path and artifact.content_hash == content_hash
    assert queue.get(job.id).lease_generation == 2
    case.assert_clean(published=True)
