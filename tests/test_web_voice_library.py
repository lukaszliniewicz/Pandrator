import io
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database, upgrade_database
from pandrator.web.models import Voice, VoiceSample
from pandrator.web.workflow_handlers import WorkflowHandlers


def silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)
    return output.getvalue()


class VoiceLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_recording_upload_queues_normalization_without_overwriting_input(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Narrator", "language": "en"},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples",
            data={"file": (io.BytesIO(silent_wav()), "capture.wav")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["kind"], "voice.normalize_recording")


class VoiceNormalizationTests(unittest.TestCase):
    def test_ffmpeg_normalization_registers_a_new_pcm_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            upgrade_database(paths.database)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="Reference", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                source = paths.uploads / "reference.wav"
                source.write_bytes(silent_wav())
                artifacts = ArtifactService(database, paths)
                upload = artifacts.register(source, kind="audio", role="recording_upload")
                result = WorkflowHandlers(database, paths).normalize_voice_recording(
                    {"voice_id": voice_id, "source_artifact_id": upload.id, "ffmpeg_executable": "ffmpeg"},
                    lambda *_args: None,
                    threading.Event(),
                )
                artifact, output = artifacts.resolve(result["artifact_id"])
                self.assertEqual(artifact.role, "voice_sample")
                self.assertTrue(output.is_file())
                self.assertNotEqual(output, source)
                with database.session() as session:
                    sample = session.scalar(select(VoiceSample).where(VoiceSample.id == result["sample_id"]))
                    self.assertEqual(sample.voice_id, voice_id)
            finally:
                database.dispose()


if __name__ == "__main__":
    unittest.main()
