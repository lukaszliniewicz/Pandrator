"""Real FFmpeg regressions for standalone soundtrack export from video sources."""

import math
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact
from pandrator.web.soundtrack_export import (
    ensure_soundtrack_master,
    export_soundtrack_file,
    probe_soundtrack_media,
)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required"
)
class SoundtrackExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]["services"]
        self.sid = self.client.post(
            "/api/v1/sessions",
            json={"name": "Soundtrack regression", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        self.handlers = self.services.workflow_handlers
        self.original = self.services.paths.uploads / "recording.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=black:s=64x64:r=25:d=3",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(self.original),
            ],
            check=True,
            capture_output=True,
        )
        self.source = self.services.artifacts.register(
            self.original, kind="mp4", role="upload", session_id=self.sid
        )
        asset = self.services.source_library.ensure_for_artifact(
            self.source.id, display_name="recording.mp4"
        )
        self.services.source_library.attach(
            self.sid, asset["id"] if isinstance(asset, dict) else asset.id
        )
        self.speech_path = self.services.paths.uploads / "speech.wav"
        self.write_speech(self.speech_path, 2.0)
        self.speech = self.services.artifacts.register(
            self.speech_path, kind="audio", role="dubbing_audio", session_id=self.sid
        )
        self.settings = {
            "export_mode": "audio",
            "audio_mode": "mixed",
            "format": "wav",
            "subtitle_mode": "none",
            "target_language": "de",
            "mix_ducking": "strong",
            "audio_match_source_duration": True,
        }

    @staticmethod
    def write_speech(path, duration):
        rate = 48000
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(
                b"".join(
                    struct.pack(
                        "<h",
                        int(4000 * math.sin(2 * math.pi * 440 * index / rate))
                        if 0.5 <= index / rate <= 1.5
                        else 0,
                    )
                    for index in range(round(duration * rate))
                )
            )

    def master(self, settings=None, event=None):
        return ensure_soundtrack_master(
            self.handlers,
            session_id=self.sid,
            source=self.source,
            speech=self.speech,
            audio_mode="mixed",
            settings=settings or self.settings,
            cancel_event=event or threading.Event(),
        )

    def test_audio_only_settings_survive_saving_with_a_video_source(self):
        endpoint = f"/api/v1/sessions/{self.sid}/settings/output"
        for audio_format in ("flac", "mp3"):
            with self.subTest(format=audio_format):
                current = self.client.get(endpoint).get_json()
                response = self.client.put(
                    endpoint,
                    json={
                        "value": {
                            "export_mode": "audio",
                            "audio_mode": "mixed",
                            "format": audio_format,
                            "bitrate": "192k",
                            "audio_match_source_duration": False,
                            "subtitle_mode": "burned",
                            "video_transcode": True,
                        }
                    },
                    headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                saved = self.client.get(endpoint).get_json()
                self.assertEqual(saved["effective"]["export_mode"], "audio")
                self.assertEqual(saved["effective"]["format"], audio_format)
                self.assertEqual(saved["effective"]["audio_mode"], "mixed")
                self.assertFalse(saved["effective"]["audio_match_source_duration"])
                self.assertNotIn("subtitle_mode", saved["override"])
                self.assertNotIn("video_transcode", saved["override"])

    def test_video_source_produces_only_audio_with_full_timeline_and_silence(self):
        master = self.master()
        path = self.services.paths.managed_path(master.relative_path)
        info = probe_soundtrack_media(path)
        self.assertFalse(info["has_video"])
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 3, places=3)
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        samples = struct.unpack("<" + "f" * (len(raw) // 4), raw)
        self.assertLess(max(abs(value) for value in samples[:16000]), 0.00001)
        self.assertGreater(max(abs(value) for value in samples[24000:72000]), 0.001)
        self.assertLess(max(abs(value) for value in samples[-24000:]), 0.00001)
        self.assertEqual(master.metadata_json["language"], "de")

    def test_audio_and_video_requests_reuse_the_same_lossless_master(self):
        first = self.master()
        second = self.master({**self.settings, "export_mode": "media", "format": "mp3"})
        self.assertEqual(first.id, second.id)
        with self.services.database.session() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(Artifact)
                    .where(Artifact.role == "soundtrack_master")
                ),
                1,
            )

    def test_handler_audio_mode_does_not_render_video_or_export_sidecar_subtitles(self):
        with patch(
            "pandrator.logic.dubbing.video_muxing.build_replace_video_audio_command",
            side_effect=AssertionError("must not render video"),
        ):
            result = self.handlers.export(
                {"session_id": self.sid, "settings": self.settings},
                lambda *_: None,
                threading.Event(),
            )
        self.assertEqual(len(result["artifact_ids"]), 1)
        with self.services.database.session() as session:
            artifact = session.get(Artifact, result["artifact_ids"][0])
            self.assertEqual(artifact.role, "export_mixed_audio")
            self.assertTrue(artifact.metadata_json["audio_only"])
            info = probe_soundtrack_media(
                self.services.paths.managed_path(artifact.relative_path)
            )
        self.assertFalse(info["has_video"])
        self.assertAlmostEqual(info["duration"], 3, places=3)

    def test_long_speech_is_rejected_instead_of_clipped(self):
        path = self.services.paths.uploads / "too-long.wav"
        self.write_speech(path, 4)
        speech = self.services.artifacts.register(
            path, kind="audio", role="dubbing_audio", session_id=self.sid
        )
        with self.assertRaisesRegex(ValueError, "exceeds the recording"):
            ensure_soundtrack_master(
                self.handlers,
                session_id=self.sid,
                source=self.source,
                speech=speech,
                audio_mode="mixed",
                settings=self.settings,
                cancel_event=threading.Event(),
            )

    def test_flac_export_preserves_master_duration_and_has_no_video(self):
        master = self.master()
        result = export_soundtrack_file(
            self.handlers,
            session_id=self.sid,
            master=master,
            destination=self.services.paths.uploads / "final.flac",
            settings={**self.settings, "format": "flac"},
            cancel_event=threading.Event(),
        )
        info = probe_soundtrack_media(
            self.services.paths.managed_path(result.relative_path)
        )
        self.assertFalse(info["has_video"])
        self.assertAlmostEqual(info["duration"], 3, places=3)
        self.assertEqual(result.metadata_json["soundtrack_master_id"], master.id)

    def test_cancellation_does_not_publish_partial_soundtrack(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            self.master(event=event)
        with self.services.database.session() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(Artifact)
                    .where(Artifact.role == "soundtrack_master")
                ),
                0,
            )
