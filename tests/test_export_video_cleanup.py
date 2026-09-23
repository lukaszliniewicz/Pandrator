"""Video preparation and rendering share one bounded scratch-file lifetime."""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, ArtifactEdge
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
        self.enterContext(mock.patch("subprocess.run", side_effect=self.run_media_command))

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
        if self.failure_stage and f"-{self.failure_stage}-" in destination.name:
            raise subprocess.CalledProcessError(1, command, stderr="injected encoder failure")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def export(self):
        return self.handlers.export(
            {"session_id": self.session.id, "settings": self.settings},
            lambda *_: None,
            threading.Event(),
        )

    def assert_clean(self, *, published=False):
        self.assertEqual([], [
            str(path.relative_to(self.output_dir))
            for path in self.output_dir.rglob("*")
            if path.name.startswith(".")
        ])
        self.assertTrue(self.command_outputs, "The test must reach media preparation")
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
        self.assertEqual(2, len(self.command_outputs))
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
