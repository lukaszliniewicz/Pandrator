import hashlib
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

from pandrator.logic import tts_handler
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import AppSetting, Artifact, Voice, VoiceSample
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
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]

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
            data={
                "file": (io.BytesIO(silent_wav()), "capture.wav"),
                "expected_revision": str(voice["revision"]),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["kind"], "voice.normalize_recording")

    def test_voice_list_seeds_bundled_reference_sample(self):
        voices = self.client.get("/api/v1/voices").get_json()["items"]
        bundled = next(
            item for item in voices if item["metadata_json"].get("bundled_voice")
        )
        samples = self.client.get(f"/api/v1/voices/{bundled['id']}/samples").get_json()[
            "items"
        ]
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
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{voice["revision"]}"',
            },
        )
        self.assertEqual(missing.status_code, 422)

        extension = self.app.extensions["pandrator"]
        sample_path = extension["paths"].voices / voice["id"] / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(
            sample_path, kind="audio", role="voice_sample"
        )
        with extension["database"].session() as session:
            session.add(VoiceSample(voice_id=voice["id"], artifact_id=artifact.id))

        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/kobold_qwen",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{voice["revision"]}"',
            },
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        job = response.get_json()
        self.assertEqual(job["kind"], "voice.publish")
        self.assertEqual(job["payload_json"]["service_id"], "kobold_qwen")
        self.assertEqual(job["payload_json"]["service"], "Qwen3 TTS")
        self.assertNotIn("base_url", job["payload_json"])

    def test_fish_publish_requires_a_reviewed_transcript(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Fish narrator", "language": "en"},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()
        extension = self.app.extensions["pandrator"]
        sample_path = extension["paths"].voices / voice["id"] / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(
            sample_path, kind="audio", role="voice_sample"
        )
        with extension["database"].session() as session:
            sample = VoiceSample(voice_id=voice["id"], artifact_id=artifact.id)
            session.add(sample)
            session.flush()
            sample_id = sample.id

        missing_review = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/fishs2",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{voice["revision"]}"',
            },
        )
        self.assertEqual(422, missing_review.status_code)
        self.assertEqual(
            "reviewed_transcript_required",
            missing_review.get_json()["error"]["code"],
        )

        reviewed = self.client.patch(
            f"/api/v1/voices/{voice['id']}/samples/{sample_id}/transcript",
            json={
                "transcript": "A reviewed Fish reference.",
                "language": "en",
                "expected_voice_revision": voice["revision"],
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(200, reviewed.status_code, reviewed.get_json())
        queued = self.client.post(
            f"/api/v1/voices/{voice['id']}/providers/fishs2",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{reviewed.get_json()["voice_revision"]}"',
            },
        )
        self.assertEqual(202, queued.status_code, queued.get_json())

    def test_owned_provider_copy_can_be_queued_for_removal(self):
        extension = self.app.extensions["pandrator"]
        with extension["database"].session() as session:
            voice = Voice(
                name="Managed narrator",
                metadata_json={
                    "providers": {
                        "kobold_qwen": {
                            "voice_id": "pandrator-managed-123",
                            "status": "ready",
                            "managed_by": "pandrator",
                            "endpoint_fingerprint": "recorded-by-upload",
                        }
                    }
                },
            )
            session.add(voice)
            session.flush()
            voice_id = voice.id
            revision = voice.revision

        queued = self.client.delete(
            f"/api/v1/voices/{voice_id}/providers/kobold_qwen",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{revision}"',
            },
        )
        self.assertEqual(202, queued.status_code, queued.get_json())
        job = queued.get_json()
        self.assertEqual("voice.unpublish", job["kind"])
        self.assertEqual(revision, job["payload_json"]["expected_voice_revision"])
        blocked_local_delete = self.client.delete(
            f"/api/v1/voices/{voice_id}",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{revision}"',
            },
        )
        self.assertEqual(409, blocked_local_delete.status_code)
        self.assertEqual("voice_busy", blocked_local_delete.get_json()["error"]["code"])

    def test_legacy_provider_copy_is_not_deleted_without_ownership_proof(self):
        extension = self.app.extensions["pandrator"]
        with extension["database"].session() as session:
            voice = Voice(
                name="Legacy narrator",
                metadata_json={
                    "providers": {
                        "kobold_qwen": {
                            "voice_id": "Legacy narrator",
                            "status": "ready",
                        }
                    }
                },
            )
            session.add(voice)
            session.flush()
            voice_id = voice.id
            revision = voice.revision

        response = self.client.delete(
            f"/api/v1/voices/{voice_id}/providers/kobold_qwen",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{revision}"',
            },
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("legacy_registration", response.get_json()["error"]["code"])

    def test_voice_and_sample_lifecycle_is_revisioned_and_cleans_managed_files(self):
        voice = self.client.post(
            "/api/v1/voices",
            json={"name": "Editable narrator", "language": "en"},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()
        missing_precondition = self.client.patch(
            f"/api/v1/voices/{voice['id']}",
            json={"description": "Updated"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(428, missing_precondition.status_code)

        extension = self.app.extensions["pandrator"]
        sample_path = extension["paths"].voices / voice["id"] / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(silent_wav())
        artifact = extension["artifacts"].register(
            sample_path,
            kind="audio",
            role="voice_sample",
        )
        with extension["database"].session() as session:
            sample = VoiceSample(voice_id=voice["id"], artifact_id=artifact.id)
            session.add(sample)
            session.flush()
            sample_id = sample.id

        listed = self.client.get(f"/api/v1/voices/{voice['id']}/samples").get_json()[
            "items"
        ]
        self.assertTrue(listed[0]["available"])
        sample_path.unlink()
        missing = self.client.get(f"/api/v1/voices/{voice['id']}/samples").get_json()[
            "items"
        ]
        self.assertEqual("missing", missing[0]["file_status"])

        replacement = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/{sample_id}/replace",
            data={"file": (io.BytesIO(silent_wav()), "replacement.wav")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf, "If-Match": '"1"'},
        )
        self.assertEqual(202, replacement.status_code, replacement.get_json())
        self.assertEqual(
            sample_id,
            replacement.get_json()["payload_json"]["replace_sample_id"],
        )

        deleted = self.client.delete(
            f"/api/v1/voices/{voice['id']}/samples/{sample_id}",
            headers={"X-CSRF-Token": self.csrf, "If-Match": '"1"'},
        )
        self.assertEqual(200, deleted.status_code, deleted.get_json())
        self.assertEqual(2, deleted.get_json()["voice_revision"])
        with extension["database"].session() as session:
            self.assertIsNone(session.get(VoiceSample, sample_id))
            self.assertEqual("deleted", session.get(Artifact, artifact.id).state)

        updated = self.client.patch(
            f"/api/v1/voices/{voice['id']}",
            json={"name": "Renamed narrator", "description": "Updated"},
            headers={"X-CSRF-Token": self.csrf, "If-Match": '"2"'},
        )
        self.assertEqual(200, updated.status_code, updated.get_json())
        self.assertEqual(3, updated.get_json()["revision"])
        self.assertEqual("Renamed narrator", updated.get_json()["name"])

        removed = self.client.delete(
            f"/api/v1/voices/{voice['id']}",
            headers={"X-CSRF-Token": self.csrf, "If-Match": '"3"'},
        )
        self.assertEqual(204, removed.status_code)

    def test_bundled_voice_and_sample_are_protected(self):
        bundled = next(
            item
            for item in self.client.get("/api/v1/voices").get_json()["items"]
            if item["bundled"]
        )
        sample = self.client.get(f"/api/v1/voices/{bundled['id']}/samples").get_json()[
            "items"
        ][0]

        voice_delete = self.client.delete(
            f"/api/v1/voices/{bundled['id']}",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{bundled["revision"]}"',
            },
        )
        sample_delete = self.client.delete(
            f"/api/v1/voices/{bundled['id']}/samples/{sample['id']}",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{bundled["revision"]}"',
            },
        )
        self.assertEqual(409, voice_delete.status_code)
        self.assertEqual(409, sample_delete.status_code)


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
                upload = artifacts.register(
                    source, kind="audio", role="recording_upload"
                )
                result = WorkflowHandlers(database, paths).normalize_voice_recording(
                    {
                        "voice_id": voice_id,
                        "source_artifact_id": upload.id,
                        "ffmpeg_executable": "ffmpeg",
                    },
                    lambda *_args: None,
                    threading.Event(),
                )
                artifact, output = artifacts.resolve(result["artifact_id"])
                self.assertEqual(artifact.role, "voice_sample")
                self.assertTrue(output.is_file())
                self.assertNotEqual(output, source)
                with database.session() as session:
                    sample = session.scalar(
                        select(VoiceSample).where(VoiceSample.id == result["sample_id"])
                    )
                    self.assertEqual(sample.voice_id, voice_id)
            finally:
                database.dispose()

    def test_replacement_is_atomic_stales_provider_and_removes_old_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(
                        name="Replaceable",
                        language="en",
                        metadata_json={
                            "providers": {
                                "kobold_qwen": {
                                    "voice_id": "Replaceable",
                                    "status": "ready",
                                }
                            }
                        },
                    )
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                artifacts = ArtifactService(database, paths)
                old_path = paths.voices / voice_id / "old.wav"
                old_path.parent.mkdir(parents=True)
                old_path.write_bytes(silent_wav())
                old_artifact = artifacts.register(
                    old_path,
                    kind="audio",
                    role="voice_sample",
                )
                with database.session() as session:
                    sample = VoiceSample(
                        voice_id=voice_id,
                        artifact_id=old_artifact.id,
                        transcript="Old words.",
                        transcript_reviewed=True,
                    )
                    session.add(sample)
                    session.flush()
                    sample_id = sample.id
                source_path = paths.uploads / "replacement.wav"
                source_path.write_bytes(silent_wav())
                source = artifacts.register(
                    source_path,
                    kind="audio",
                    role="recording_upload",
                )

                result = WorkflowHandlers(database, paths).normalize_voice_recording(
                    {
                        "voice_id": voice_id,
                        "replace_sample_id": sample_id,
                        "source_artifact_id": source.id,
                        "expected_voice_revision": 1,
                        "ffmpeg_executable": "ffmpeg",
                    },
                    lambda *_args: None,
                    threading.Event(),
                )

                self.assertTrue(result["replaced"])
                self.assertEqual(sample_id, result["sample_id"])
                self.assertFalse(old_path.exists())
                with database.session() as session:
                    sample = session.get(VoiceSample, sample_id)
                    voice = session.get(Voice, voice_id)
                    self.assertNotEqual(old_artifact.id, sample.artifact_id)
                    self.assertFalse(sample.transcript_reviewed)
                    self.assertIsNone(sample.transcript)
                    self.assertEqual(2, voice.revision)
                    self.assertEqual(
                        "stale",
                        voice.metadata_json["providers"]["kobold_qwen"]["status"],
                    )
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
            app = create_app(
                data_root=directory, testing=True, bootstrap_tokens=bootstrap
            )
            database = app.extensions["pandrator"]["database"]
            try:
                with database.session() as session:
                    defaults = session.get(AppSetting, "defaults.stt")
                    self.assertEqual(defaults.value_json["stt_engine"], "parakeet")
                    self.assertEqual(
                        defaults.value_json["stt_model_quantization"], "q4_k"
                    )

                client = app.test_client()
                client.post("/api/v1/auth/bootstrap", json={"token": token})
                runtime = SimpleNamespace(
                    installed=True,
                    version="test",
                    executable="crispasr",
                    compute_backends=("cpu",),
                )
                with mock.patch(
                    "pandrator.web.capabilities.probe_crispasr_runtime",
                    return_value=runtime,
                ):
                    capabilities = client.get("/api/v1/capabilities").get_json()["stt"]
                self.assertEqual(capabilities["default_engine"], "parakeet")
                self.assertTrue(capabilities["models"]["parakeet"]["default"])
                self.assertFalse(capabilities["models"]["whisper"]["default"])
                self.assertTrue(capabilities["models"]["whisper"]["download_on_demand"])
            finally:
                database.dispose()

    def test_moss_installer_preference_defaults_to_q8_and_is_exposed_to_web(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.json").write_text(
                json.dumps({"crispasr_engine": "moss-transcribe-diarize-0.9b"}),
                encoding="utf-8",
            )
            prepare_web_test_data_root(directory)
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            app = create_app(
                data_root=directory, testing=True, bootstrap_tokens=bootstrap
            )
            database = app.extensions["pandrator"]["database"]
            try:
                with database.session() as session:
                    defaults = session.get(AppSetting, "defaults.stt")
                    self.assertEqual(defaults.value_json["stt_engine"], "moss")
                    self.assertEqual(
                        defaults.value_json["stt_model_quantization"], "q8_0"
                    )

                client = app.test_client()
                client.post("/api/v1/auth/bootstrap", json={"token": token})
                runtime = SimpleNamespace(
                    installed=True,
                    version="test",
                    executable="crispasr",
                    compute_backends=("vulkan", "cpu"),
                )
                with mock.patch(
                    "pandrator.web.capabilities.probe_crispasr_runtime",
                    return_value=runtime,
                ):
                    capabilities = client.get("/api/v1/capabilities").get_json()["stt"]
                self.assertEqual(capabilities["default_engine"], "moss")
                self.assertEqual(capabilities["default_model_quantization"], "q8_0")
                self.assertEqual(
                    capabilities["models"]["moss"]["diarization"], "native"
                )
                self.assertEqual(capabilities["models"]["moss"]["word_timing"], "ctc")
            finally:
                database.dispose()


class VoiceProviderPublishTests(unittest.TestCase):
    def test_audio_cpp_publish_links_local_sample_and_unpublish_never_calls_remote(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="Linked narrator", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                sample_path = paths.voices / voice_id / "sample.wav"
                sample_path.parent.mkdir(parents=True)
                sample_path.write_bytes(silent_wav())
                artifact = ArtifactService(database, paths).register(
                    sample_path,
                    kind="audio",
                    role="voice_sample",
                )
                with database.session() as session:
                    session.add(
                        VoiceSample(
                            voice_id=voice_id,
                            artifact_id=artifact.id,
                            transcript="Reviewed audio.cpp reference.",
                            transcript_reviewed=True,
                        )
                    )

                handler = WorkflowHandlers(database, paths)
                with mock.patch.object(handler.tts_providers, "upload_voice") as upload:
                    linked = handler.publish_voice(
                        {
                            "voice_id": voice_id,
                            "service_id": "audio_cpp",
                            "service": "audio.cpp",
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )
                upload.assert_not_called()
                self.assertTrue(linked["linked"])

                prepared = handler.prepare_audio_cpp_voice_reference(
                    {
                        "service": "audio_cpp",
                        "xtts_model": "qwen3_tts_1_7b_base_q8_0",
                        "speaker": linked["provider_voice_id"],
                    }
                )
                self.assertEqual(
                    "Reviewed audio.cpp reference.",
                    prepared["audio_cpp_reference_text"],
                )
                self.assertTrue(
                    prepared["audio_cpp_voice_ref"]["data"].startswith(
                        "data:audio/wav;base64,"
                    )
                )
                self.assertEqual(1, len(handler._audio_cpp_voice_ref_cache))

                with mock.patch.object(handler.tts_providers, "delete_voice") as remove:
                    unlinked = handler.unpublish_voice(
                        {
                            "voice_id": voice_id,
                            "service_id": "audio_cpp",
                            "service": "audio.cpp",
                            "expected_voice_revision": linked["voice_revision"],
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )
                remove.assert_not_called()
                self.assertFalse(unlinked["remote_deleted"])
                with database.session() as session:
                    stored = session.get(Voice, voice_id)
                    self.assertNotIn(
                        "audio_cpp",
                        stored.metadata_json.get("providers", {}),
                    )
            finally:
                database.dispose()

    def test_audio_cpp_external_profile_keeps_exact_registration_id(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="External narrator", language="en")
                    session.add(voice)
                    session.add(
                        AppSetting(
                            key="services.tts",
                            value_json={
                                "provider_configs": [
                                    {
                                        "id": "audio-cpp-experimental",
                                        "name": "External audio.cpp",
                                        "provider": "openai",
                                        "api_base": "http://127.0.0.1:8080",
                                        "adapter": "audio_cpp",
                                        "speech_path": "/v1/audio/speech",
                                        "models": ["qwen3_tts_1_7b_base_q8_0"],
                                        "voices": [],
                                        "supports_voice_cloning": True,
                                        "supports_voice_deletion": False,
                                        "voice_reference_text": "optional",
                                        "auth_mode": "none",
                                    }
                                ]
                            },
                        )
                    )
                    session.flush()
                    voice_id = voice.id
                sample_path = paths.voices / voice_id / "sample.wav"
                sample_path.parent.mkdir(parents=True)
                sample_path.write_bytes(silent_wav())
                artifact = ArtifactService(database, paths).register(
                    sample_path,
                    kind="audio",
                    role="voice_sample",
                )
                with database.session() as session:
                    session.add(
                        VoiceSample(
                            voice_id=voice_id,
                            artifact_id=artifact.id,
                            transcript="External profile reference.",
                            transcript_reviewed=True,
                        )
                    )

                handler = WorkflowHandlers(database, paths)
                linked = handler.publish_voice(
                    {
                        "voice_id": voice_id,
                        "service_id": "audio-cpp-experimental",
                        "service": "External audio.cpp",
                    },
                    lambda *_args: None,
                    threading.Event(),
                )
                with database.session() as session:
                    stored = session.get(Voice, voice_id)
                    self.assertIn(
                        "audio-cpp-experimental",
                        stored.metadata_json.get("providers", {}),
                    )

                prepared = handler.prepare_audio_cpp_voice_reference(
                    {
                        "service": tts_handler.OPENAI_COMPAT_SERVICE,
                        "openai_audio_endpoint": "audio-cpp-experimental",
                        "provider_configs": [
                            {
                                "id": "audio-cpp-experimental",
                                "name": "External audio.cpp",
                                "provider": "openai",
                                "api_base": "http://127.0.0.1:8080",
                                "adapter": "audio_cpp",
                                "speech_path": "/v1/audio/speech",
                                "models": ["qwen3_tts_1_7b_base_q8_0"],
                                "voices": [],
                            }
                        ],
                        "xtts_model": "qwen3_tts_1_7b_base_q8_0",
                        "speaker": linked["provider_voice_id"],
                    }
                )
                self.assertEqual(
                    "External profile reference.",
                    prepared["audio_cpp_reference_text"],
                )

                unlinked = handler.unpublish_voice(
                    {
                        "voice_id": voice_id,
                        "service_id": "audio-cpp-experimental",
                        "service": "External audio.cpp",
                        "expected_voice_revision": linked["voice_revision"],
                    },
                    lambda *_args: None,
                    threading.Event(),
                )
                self.assertFalse(unlinked["remote_deleted"])
            finally:
                database.dispose()

    def test_managed_fish_publish_resolves_endpoint_when_job_runs(self):
        class FakeManager:
            configured = True

            @staticmethod
            def managed_service(service_id):
                if service_id != "tts.fish_speech":
                    raise AssertionError(service_id)
                return {
                    "id": service_id,
                    "endpoint": "http://127.0.0.1:8022",
                    "health": {"state": "healthy"},
                }

        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="Fish narrator", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                sample_path = paths.voices / voice_id / "sample.wav"
                sample_path.parent.mkdir(parents=True)
                sample_path.write_bytes(silent_wav())
                artifacts = ArtifactService(database, paths)
                artifact = artifacts.register(
                    sample_path,
                    kind="audio",
                    role="voice_sample",
                )
                with database.session() as session:
                    session.add(
                        VoiceSample(
                            voice_id=voice_id,
                            artifact_id=artifact.id,
                            transcript="Reviewed Fish reference.",
                            transcript_reviewed=True,
                        )
                    )
                handler = WorkflowHandlers(
                    database,
                    paths,
                    manager_bridge=FakeManager(),
                )
                with mock.patch(
                    "pandrator.logic.tts_handler.upload_speaker_voice",
                    return_value="Fish_narrator",
                ) as upload:
                    result = handler.publish_voice(
                        {
                            "voice_id": voice_id,
                            "service_id": "fishs2",
                            "service": "FishS2",
                            "base_url": "http://127.0.0.1:8020",
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )

                self.assertEqual("Fish_narrator", result["provider_voice_id"])
                self.assertEqual(
                    "http://127.0.0.1:8022",
                    upload.call_args.kwargs["base_url"],
                )
            finally:
                database.dispose()

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
                artifact = artifacts.register(
                    sample_path, kind="audio", role="voice_sample"
                )
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
                self.assertRegex(
                    upload.call_args.kwargs["voice_id"],
                    rf"^pandrator-my-narrator-{voice_id.replace('-', '')[:10]}$",
                )
                self.assertIsNone(upload.call_args.kwargs["prompt_text"])
                with database.session() as session:
                    stored = session.get(Voice, voice_id)
                    self.assertEqual(
                        stored.metadata_json["providers"]["kobold_qwen"]["voice_id"],
                        "My_narrator",
                    )
                    registration = stored.metadata_json["providers"]["kobold_qwen"]
                    self.assertEqual("pandrator", registration["managed_by"])
                    self.assertEqual("pandrator-voices-v1", registration["protocol"])
            finally:
                database.dispose()

    def test_provider_removal_clears_registration_only_after_remote_success(self):
        from pandrator.logic import tts_handler

        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                service = tts_handler.get_service_config({}, "kobold_qwen") or {}
                base_url = str(service.get("api_base") or "").strip()
                endpoint_fingerprint = hashlib.sha256(
                    base_url.rstrip("/").casefold().encode("utf-8")
                ).hexdigest()
                with database.session() as session:
                    voice = Voice(
                        name="Disposable narrator",
                        metadata_json={
                            "providers": {
                                "kobold_qwen": {
                                    "voice_id": "pandrator-disposable-123",
                                    "status": "ready",
                                    "managed_by": "pandrator",
                                    "endpoint_fingerprint": endpoint_fingerprint,
                                }
                            }
                        },
                    )
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                    revision = voice.revision
                handler = WorkflowHandlers(database, paths)
                with mock.patch(
                    "pandrator.logic.tts_handler.delete_speaker_voice",
                    return_value=True,
                ) as remove:
                    result = handler.unpublish_voice(
                        {
                            "voice_id": voice_id,
                            "service_id": "kobold_qwen",
                            "service": "Qwen3 TTS",
                            "expected_voice_revision": revision,
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )

                self.assertTrue(result["remote_deleted"])
                self.assertEqual(2, result["voice_revision"])
                remove.assert_called_once_with(
                    "pandrator-disposable-123",
                    base_url=base_url,
                    service="Qwen3 TTS",
                    api_key="",
                )
                with database.session() as session:
                    stored = session.get(Voice, voice_id)
                    self.assertNotIn(
                        "kobold_qwen",
                        stored.metadata_json.get("providers", {}),
                    )
            finally:
                database.dispose()

    def test_provider_removal_failure_keeps_registration(self):
        from pandrator.logic import tts_handler

        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                service = tts_handler.get_service_config({}, "kobold_qwen") or {}
                base_url = str(service.get("api_base") or "").strip()
                endpoint_fingerprint = hashlib.sha256(
                    base_url.rstrip("/").casefold().encode("utf-8")
                ).hexdigest()
                with database.session() as session:
                    voice = Voice(
                        name="Retained narrator",
                        metadata_json={
                            "providers": {
                                "kobold_qwen": {
                                    "voice_id": "pandrator-retained-123",
                                    "status": "ready",
                                    "managed_by": "pandrator",
                                    "endpoint_fingerprint": endpoint_fingerprint,
                                }
                            }
                        },
                    )
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                    revision = voice.revision
                handler = WorkflowHandlers(database, paths)
                with (
                    mock.patch(
                        "pandrator.logic.tts_handler.delete_speaker_voice",
                        side_effect=RuntimeError("provider offline"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "provider offline"),
                ):
                    handler.unpublish_voice(
                        {
                            "voice_id": voice_id,
                            "service_id": "kobold_qwen",
                            "service": "Qwen3 TTS",
                            "expected_voice_revision": revision,
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )

                with database.session() as session:
                    stored = session.get(Voice, voice_id)
                    self.assertIn(
                        "kobold_qwen",
                        stored.metadata_json.get("providers", {}),
                    )
                    self.assertEqual(revision, stored.revision)
            finally:
                database.dispose()

    def test_qwen_preflight_republishes_a_stale_managed_voice_once(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(
                        name="My narrator",
                        language="en",
                        metadata_json={
                            "providers": {
                                "kobold_qwen": {
                                    "voice_id": "My_narrator",
                                    "status": "ready",
                                }
                            }
                        },
                    )
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                handler = WorkflowHandlers(database, paths)
                verified = set()
                with (
                    mock.patch(
                        "pandrator.logic.tts_handler.get_kobold_qwen_voice_catalog",
                        return_value=[
                            {
                                "id": "Aiden",
                                "type": "preset",
                                "model": "Prebuilt Voices",
                            },
                            {"id": "kobo", "type": "cloned", "model": "Voice Cloning"},
                        ],
                    ),
                    mock.patch.object(handler, "publish_voice") as publish,
                ):
                    handler._ensure_qwen_cloned_voice(
                        {
                            "service": "Qwen3 TTS",
                            "model": "Voice Cloning",
                            "voice": "My_narrator",
                        },
                        base_url="http://127.0.0.1:8042",
                        verified=verified,
                        cancel_event=threading.Event(),
                    )
                    handler._ensure_qwen_cloned_voice(
                        {
                            "service": "Qwen3 TTS",
                            "model": "Voice Cloning",
                            "voice": "My_narrator",
                        },
                        base_url="http://127.0.0.1:8042",
                        verified=verified,
                        cancel_event=threading.Event(),
                    )

                publish.assert_called_once()
                self.assertEqual(voice_id, publish.call_args.args[0]["voice_id"])
                self.assertEqual("kobold_qwen", publish.call_args.args[0]["service_id"])
                self.assertEqual({"my_narrator"}, verified)
            finally:
                database.dispose()

    def test_qwen_preflight_never_substitutes_an_unknown_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                handler = WorkflowHandlers(database, paths)
                with (
                    mock.patch(
                        "pandrator.logic.tts_handler.get_kobold_qwen_voice_catalog",
                        return_value=[
                            {"id": "kobo", "type": "cloned", "model": "Voice Cloning"}
                        ],
                    ),
                    self.assertRaisesRegex(ValueError, "no managed sample"),
                ):
                    handler._ensure_qwen_cloned_voice(
                        {
                            "service": "Qwen3 TTS",
                            "model": "Voice Cloning",
                            "voice": "missing",
                        },
                        base_url="http://127.0.0.1:8042",
                        verified=set(),
                        cancel_event=threading.Event(),
                    )
            finally:
                database.dispose()


if __name__ == "__main__":
    unittest.main()
