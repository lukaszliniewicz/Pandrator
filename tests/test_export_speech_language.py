"""The dubbed speech track is named from the branch when the run is silent.

A translation branch keeps its language on the session record, not in the
export settings, so a dubbed export used to tag its audio stream ``und``.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from pandrator.web.export_subtitles import speech_track_language
from pandrator.web.models import SessionRecord


class SpeechTrackLanguageTests(unittest.TestCase):
    def test_translation_branch_uses_the_session_target_language(self):
        record = SimpleNamespace(target_language="nl", source_language="en")
        self.assertEqual(
            ("nl", "session_target_language"),
            speech_track_language(record, {}),
        )

    def test_source_language_session_uses_its_own_language(self):
        record = SimpleNamespace(target_language=None, source_language="de")
        self.assertEqual(
            ("de", "session_source_language"),
            speech_track_language(record, {}),
        )

    def test_output_language_setting_wins(self):
        record = SimpleNamespace(target_language="nl", source_language="en")
        self.assertEqual(
            ("pl", "output_language"),
            speech_track_language(record, {"language": "pl"}),
        )

    def test_auto_output_language_does_not_mask_a_concrete_language(self):
        record = SimpleNamespace(target_language=None, source_language="en")
        self.assertEqual(
            ("de", "output_language"),
            speech_track_language(
                record, {"output_language": "auto", "language": "de"}
            ),
        )

    def test_run_language_is_the_last_resort(self):
        record = SimpleNamespace(target_language=None, source_language="auto")
        self.assertEqual(
            ("ja", "run_language"),
            speech_track_language(record, {"target_language": "ja"}),
        )

    def test_unusable_values_add_no_tag(self):
        for record in (
            SimpleNamespace(target_language=None, source_language="auto"),
            SimpleNamespace(target_language="", source_language="und"),
            SimpleNamespace(target_language="unknown", source_language=""),
        ):
            with self.subTest(record=record):
                self.assertEqual(("", "none"), speech_track_language(record, {}))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required"
)
class DubbedTrackTaggingTests(unittest.TestCase):
    """One tiny real export per language source, asserting the stream tag."""

    def setUp(self):
        from pandrator.web.api import create_app
        from pandrator.web.auth import BootstrapTokenStore

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.addCleanup(self.app.extensions["pandrator"]["database"].dispose)
        self.client = self.app.test_client()
        token = bootstrap.issue()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]["services"]
        self.handlers = self.services.workflow_handlers

    def _session(self):
        return self.client.post(
            "/api/v1/sessions",
            json={"name": "Dubbed language", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]

    def _video(self, session_id, duration=2):
        path = self.services.paths.uploads / f"language-{session_id}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"color=black:s=64x64:r=25:d={duration}",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", str(duration),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(path),
            ],
            check=True, capture_output=True, timeout=120,
        )
        artifact = self.services.artifacts.register(
            path, kind="mp4", role="upload", session_id=session_id
        )
        asset = self.services.source_library.ensure_for_artifact(
            artifact.id, display_name=path.name
        )
        self.services.source_library.attach(
            session_id, asset["id"] if isinstance(asset, dict) else asset.id
        )

    def _speech(self, session_id, duration=1.5):
        path = self.services.paths.uploads / f"language-{session_id}.wav"
        rate = 48000
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(
                b"".join(
                    struct.pack(
                        "<h", int(4000 * math.sin(2 * math.pi * 440 * index / rate))
                    )
                    for index in range(round(duration * rate))
                )
            )
        return self.services.artifacts.register(
            path, kind="audio", role="dubbing_audio", session_id=session_id
        )

    def _export(self, session_id):
        from pandrator.web.models import Artifact

        result = self.handlers.export(
            {
                "session_id": session_id,
                "settings": {
                    "export_mode": "media",
                    "audio_mode": "dubbing_only",
                    "subtitle_mode": "none",
                },
            },
            lambda *_: None,
            threading.Event(),
        )
        self.assertEqual(1, len(result["artifact_ids"]), result)
        with self.services.database.session() as session:
            artifact = session.get(Artifact, result["artifact_ids"][0])
            assert artifact is not None
            metadata = dict(artifact.metadata_json or {})
            path = self.services.paths.managed_path(artifact.relative_path)
        return metadata, path

    def _audio_tags(self, path: Path) -> dict[str, str]:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream_tags", "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return json.loads(probe.stdout)["streams"][0].get("tags", {})

    def test_translation_branch_tags_the_dubbed_track(self):
        session_id = self._session()
        self._video(session_id)
        self._speech(session_id)
        with self.services.database.session() as session:
            record = session.get(SessionRecord, session_id)
            record.target_language = "nl"

        metadata, path = self._export(session_id)

        self.assertEqual("nl", metadata["audio_language"])
        self.assertEqual("session_target_language", metadata["audio_language_origin"])
        self.assertEqual("nld", self._audio_tags(path).get("language"))

    def test_source_language_session_tags_its_own_language(self):
        session_id = self._session()
        self._video(session_id)
        self._speech(session_id)
        with self.services.database.session() as session:
            record = session.get(SessionRecord, session_id)
            record.target_language = None
            record.source_language = "de"

        metadata, path = self._export(session_id)

        self.assertEqual("de", metadata["audio_language"])
        self.assertEqual("session_source_language", metadata["audio_language_origin"])
        self.assertEqual("deu", self._audio_tags(path).get("language"))

    def test_untargeted_session_leaves_the_track_untagged(self):
        session_id = self._session()
        self._video(session_id)
        self._speech(session_id)

        metadata, path = self._export(session_id)

        self.assertIsNone(metadata["audio_language"])
        self.assertEqual("none", metadata["audio_language_origin"])
        self.assertEqual("und", self._audio_tags(path).get("language", "und"))


if __name__ == "__main__":
    unittest.main()
