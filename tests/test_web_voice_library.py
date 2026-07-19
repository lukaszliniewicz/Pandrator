import io
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import AppSetting, Voice, VoiceSample
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


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
        prepare_web_test_data_root(self.temporary.name)
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

    def test_voice_list_seeds_bundled_reference_sample(self):
        voices = self.client.get("/api/v1/voices").get_json()["items"]
        bundled = next(item for item in voices if item["metadata_json"].get("bundled_voice"))
        samples = self.client.get(f"/api/v1/voices/{bundled['id']}/samples").get_json()["items"]
        self.assertEqual(bundled["name"], "Pandrator sample voice")
        self.assertEqual(len(samples), 1)
        self.assertTrue(samples[0]["transcript_reviewed"])

    def test_provider_publish_requires_sample_then_queues_exact_service(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Qwen narrator", "language": "en"},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()
        missing = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/kobold_qwen",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(missing.status_code, 422)

        extension = self.app.extensions["pandrator"]
        sample_path = extension["paths"].voices / voice["id"] / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(sample_path, kind="audio", role="voice_sample")
        with extension["database"].session() as session:
            session.add(VoiceSample(voice_id=voice["id"], artifact_id=artifact.id))

        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/kobold_qwen",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        job = response.get_json()
        self.assertEqual(job["kind"], "voice.publish")
        self.assertEqual(job["payload_json"]["service_id"], "kobold_qwen")
        self.assertEqual(job["payload_json"]["service"], "Qwen3 TTS")


class VoiceNormalizationTests(unittest.TestCase):
    def test_ffmpeg_normalization_registers_a_new_pcm_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
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


class InstallerAsrPreferenceTests(unittest.TestCase):
    def test_single_installed_asr_model_becomes_default_and_other_is_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.json").write_text(
                json.dumps(
                    {
                        "crispasr_engine": "parakeet-tdt-0.6b-v3",
                        "crispasr_model_quantization": "q4_k",
                    }
                ),
                encoding="utf-8",
            )
            prepare_web_test_data_root(directory)
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
            database = app.extensions["pandrator"]["database"]
            try:
                with database.session() as session:
                    defaults = session.get(AppSetting, "defaults.stt")
                    self.assertEqual(defaults.value_json["stt_engine"], "parakeet")
                    self.assertEqual(defaults.value_json["stt_model_quantization"], "q4_k")

                client = app.test_client()
                client.post("/api/v1/auth/bootstrap", json={"token": token})
                runtime = SimpleNamespace(
                    installed=True,
                    version="test",
                    executable="crispasr",
                    compute_backends=("cpu",),
                )
                with mock.patch("pandrator.web.capabilities.probe_crispasr_runtime", return_value=runtime):
                    capabilities = client.get("/api/v1/capabilities").get_json()["stt"]
                self.assertEqual(capabilities["default_engine"], "parakeet")
                self.assertTrue(capabilities["models"]["parakeet"]["default"])
                self.assertFalse(capabilities["models"]["whisper"]["default"])
                self.assertTrue(capabilities["models"]["whisper"]["download_on_demand"])
            finally:
                database.dispose()


class VoiceProviderPublishTests(unittest.TestCase):
    def test_provider_publish_persists_returned_voice_id(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="My narrator", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                sample_path = paths.voices / voice_id / "sample.wav"
                sample_path.parent.mkdir(parents=True)
                sample_path.write_bytes(silent_wav())
                artifacts = ArtifactService(database, paths)
                artifact = artifacts.register(sample_path, kind="audio", role="voice_sample")
                with database.session() as session:
                    session.add(
                        VoiceSample(
                            voice_id=voice_id,
                            artifact_id=artifact.id,
                            transcript="Reviewed words.",
                            transcript_reviewed=True,
                        )
                    )
                handler = WorkflowHandlers(database, paths)
                with mock.patch(
                    "pandrator.logic.tts_handler.upload_speaker_voice",
                    return_value="My_narrator",
                ) as upload:
                    result = handler.publish_voice(
                        {
                            "voice_id": voice_id,
                            "service_id": "kobold_qwen",
                            "service": "Qwen3 TTS",
                            "base_url": "http://127.0.0.1:8042",
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )
                self.assertEqual(result["provider_voice_id"], "My_narrator")
                self.assertEqual(upload.call_args.kwargs["voice_id"], "My narrator")
                self.assertEqual(upload.call_args.kwargs["prompt_text"], "Reviewed words.")
                with database.session() as session:
                    stored = session.get(Voice, voice_id)
                    self.assertEqual(
                        stored.metadata_json["providers"]["kobold_qwen"]["voice_id"],
                        "My_narrator",
                    )
            finally:
                database.dispose()


if __name__ == "__main__":
    unittest.main()
