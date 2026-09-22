"""Microphone voice-sample cleanup: DeepFilterNet2 opt-in coverage.

Covers only the multipart sample routes' ``noise_reduction`` flag and the
``normalize_voice_recording`` job branch. The shared
``pandrator.logic.audio_cpp_processing.clean_voice_sample`` helper is owned
elsewhere, so it is stubbed here; these tests assert the integration
contract (flag parsing, 48 kHz preprocess input, raw preservation, and
failure atomicity) using only temporary artifacts.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService, sha256_file
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import Artifact, Job, Voice, VoiceSample
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


def silent_wav(*, framerate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(b"\0\0" * 160)
    return output.getvalue()


def wav_params(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnchannels(), handle.getframerate()


def stub_cleanup_module(calls: dict, *, failure: Exception | None = None):
    module = types.ModuleType("pandrator.logic.audio_cpp_processing")

    def clean_voice_sample(source, destination, settings, cancel_event=None, progress=None):
        calls["source"] = Path(source)
        calls["destination"] = Path(destination)
        calls["settings"] = dict(settings)
        calls["cancel_event"] = cancel_event
        # Capture input properties now: the worker cleans up its
        # preprocess temporaries before returning.
        with wave.open(str(source), "rb") as handle:
            calls["source_channels"] = handle.getnchannels()
            calls["source_rate"] = handle.getframerate()
        if failure is not None:
            raise failure
        if progress is not None:
            # Real helper progress shape: (phase, current, total).
            progress("clean", 1, 2)
            progress("install", 1, 1)
        shutil.copy2(source, destination)
        return {"model": "deepfilternet2", "stub": True}

    module.clean_voice_sample = clean_voice_sample
    return module


class VoiceCleanupWorkerTests(unittest.TestCase):
    def test_off_bypass_needs_no_cleanup_dependency_or_download(self):
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
                # Any import of the cleanup helper must fail loudly here; the
                # default path must never touch it.
                with mock.patch.dict(
                    sys.modules,
                    {"pandrator.logic.audio_cpp_processing": None},
                ):
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
            finally:
                database.dispose()

    def test_deepfilternet2_preprocesses_48k_and_preserves_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="Noisy reference", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                source = paths.uploads / "noisy.wav"
                source.write_bytes(silent_wav())
                artifacts = ArtifactService(database, paths)
                upload = artifacts.register(
                    source, kind="audio", role="recording_upload"
                )
                raw_hash = sha256_file(artifacts.resolve(upload.id)[1])
                calls: dict = {}
                with mock.patch.dict(
                    sys.modules,
                    {
                        "pandrator.logic.audio_cpp_processing": stub_cleanup_module(
                            calls
                        )
                    },
                ):
                    result = WorkflowHandlers(database, paths).normalize_voice_recording(
                        {
                            "voice_id": voice_id,
                            "source_artifact_id": upload.id,
                            "expected_voice_revision": 1,
                            "noise_reduction": "deepfilternet2",
                            "ffmpeg_executable": "ffmpeg",
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )
                # The helper received a 48 kHz mono preprocess file, not the raw.
                self.assertEqual(
                    {"model": "deepfilternet2"}, calls["settings"]
                )
                self.assertIsNotNone(calls["cancel_event"])
                self.assertEqual(1, calls["source_channels"])
                self.assertEqual(48000, calls["source_rate"])
                # The raw upload artifact is preserved byte-for-byte.
                raw_artifact, raw_path = artifacts.resolve(upload.id)
                self.assertEqual("recording_upload", raw_artifact.role)
                self.assertEqual(raw_hash, sha256_file(raw_path))
                # The final sample is still the durable mono 24 kHz format.
                artifact, output = artifacts.resolve(result["artifact_id"])
                self.assertEqual("voice_sample", artifact.role)
                self.assertEqual((1, 24000), wav_params(output))
                self.assertNotEqual(output, raw_path)
                with database.session() as session:
                    sample = session.get(VoiceSample, result["sample_id"])
                    voice = session.get(Voice, voice_id)
                    self.assertFalse(sample.transcript_reviewed)
                    self.assertIsNone(sample.transcript)
                    self.assertEqual(2, voice.revision)
                    metadata = session.get(Artifact, result["artifact_id"]).metadata_json
                    self.assertEqual(
                        "deepfilternet2",
                        metadata["noise_reduction"]["method"],
                    )
            finally:
                database.dispose()

    def test_deepfilternet2_failure_commits_no_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="Failing cleanup", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                source = paths.uploads / "noisy.wav"
                source.write_bytes(silent_wav())
                artifacts = ArtifactService(database, paths)
                upload = artifacts.register(
                    source, kind="audio", role="recording_upload"
                )
                calls: dict = {}
                cleanup_stub = stub_cleanup_module(
                    calls, failure=RuntimeError("DeepFilterNet2 failed")
                )
                with (
                    mock.patch.dict(
                        sys.modules,
                        {"pandrator.logic.audio_cpp_processing": cleanup_stub},
                    ),
                    self.assertRaisesRegex(RuntimeError, "DeepFilterNet2 failed"),
                ):
                    WorkflowHandlers(database, paths).normalize_voice_recording(
                        {
                            "voice_id": voice_id,
                            "source_artifact_id": upload.id,
                            "expected_voice_revision": 1,
                            "noise_reduction": "deepfilternet2",
                            "ffmpeg_executable": "ffmpeg",
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )
                with database.session() as session:
                    self.assertEqual(
                        [],
                        list(
                            session.scalars(
                                select(VoiceSample).where(
                                    VoiceSample.voice_id == voice_id
                                )
                            )
                        ),
                    )
                    self.assertEqual(1, session.get(Voice, voice_id).revision)
                voice_files = list((paths.voices / voice_id).glob("*.wav"))
                self.assertEqual([], voice_files)
            finally:
                database.dispose()

    def test_unknown_flag_rejected_by_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            try:
                with database.session() as session:
                    voice = Voice(name="Strict flags", language="en")
                    session.add(voice)
                    session.flush()
                    voice_id = voice.id
                source = paths.uploads / "reference.wav"
                source.write_bytes(silent_wav())
                upload = ArtifactService(database, paths).register(
                    source, kind="audio", role="recording_upload"
                )
                with self.assertRaisesRegex(ValueError, "noise_reduction"):
                    WorkflowHandlers(database, paths).normalize_voice_recording(
                        {
                            "voice_id": voice_id,
                            "source_artifact_id": upload.id,
                            "noise_reduction": "superclean",
                            "ffmpeg_executable": "ffmpeg",
                        },
                        lambda *_args: None,
                        threading.Event(),
                    )
                with database.session() as session:
                    self.assertEqual(
                        [],
                        list(
                            session.scalars(
                                select(VoiceSample).where(
                                    VoiceSample.voice_id == voice_id
                                )
                            )
                        ),
                    )
            finally:
                database.dispose()


class VoiceCleanupRouteTests(unittest.TestCase):
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
        self.database = self.app.extensions["pandrator"]["database"]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _create_voice(self) -> dict:
        return self.client.post(
            "/api/v1/voices",
            json={"name": "Narrator", "language": "en"},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()

    def test_upload_rejects_unknown_flag_before_side_effects(self):
        voice = self._create_voice()
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples",
            data={
                "file": (io.BytesIO(silent_wav()), "capture.wav"),
                "expected_revision": str(voice["revision"]),
                "noise_reduction": "superclean",
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(422, response.status_code, response.get_json())
        with self.database.session() as session:
            self.assertEqual([], list(session.scalars(select(Job)).all()))
            self.assertEqual(
                voice["revision"], session.get(Voice, voice["id"]).revision
            )

    def test_upload_and_replace_pass_flag_to_job(self):
        voice = self._create_voice()
        upload = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples",
            data={
                "file": (io.BytesIO(silent_wav()), "capture.wav"),
                "expected_revision": str(voice["revision"]),
                "noise_reduction": "deepfilternet2",
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(202, upload.status_code, upload.get_json())
        with self.database.session() as session:
            keys = session.get(Job, upload.get_json()["id"]).resource_keys_json
        self.assertEqual(
            {f"voice:{voice['id']}", "service:tts:audio_cpp", "gpu:default"},
            set(keys),
        )
        self.assertEqual(
            "deepfilternet2",
            upload.get_json()["payload_json"]["noise_reduction"],
        )

        # Raw uploads stay unchanged unless the flag is chosen.
        plain = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples",
            data={
                "file": (io.BytesIO(silent_wav()), "plain.wav"),
                "expected_revision": str(voice["revision"]),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(202, plain.status_code, plain.get_json())
        with self.database.session() as session:
            keys = session.get(Job, plain.get_json()["id"]).resource_keys_json
        self.assertEqual([f"voice:{voice['id']}"], keys)
        self.assertEqual(
            "none", plain.get_json()["payload_json"]["noise_reduction"]
        )

        # Seed one saved sample through the worker, then replace it by revision.
        extension = self.app.extensions["pandrator"]
        source_path = extension["paths"].uploads / "seed.wav"
        source_path.write_bytes(silent_wav())
        seed_upload = extension["artifacts"].register(
            source_path, kind="audio", role="recording_upload"
        )
        seed = WorkflowHandlers(self.database, extension["paths"]).normalize_voice_recording(
            {
                "voice_id": voice["id"],
                "source_artifact_id": seed_upload.id,
                "expected_voice_revision": voice["revision"],
                "ffmpeg_executable": "ffmpeg",
            },
            lambda *_args: None,
            threading.Event(),
        )
        with self.database.session() as session:
            revision = session.get(Voice, voice["id"]).revision
        replace = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/{seed['sample_id']}/replace",
            data={
                "file": (io.BytesIO(silent_wav()), "replacement.wav"),
                "noise_reduction": "deepfilternet2",
            },
            content_type="multipart/form-data",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{revision}"',
            },
        )
        self.assertEqual(202, replace.status_code, replace.get_json())
        with self.database.session() as session:
            keys = session.get(Job, replace.get_json()["id"]).resource_keys_json
        self.assertIn("service:tts:audio_cpp", keys)
        self.assertIn("gpu:default", keys)
        self.assertEqual(
            "deepfilternet2",
            replace.get_json()["payload_json"]["noise_reduction"],
        )

    def test_replace_rejects_unknown_flag_before_side_effects(self):
        voice = self._create_voice()
        extension = self.app.extensions["pandrator"]
        source_path = extension["paths"].uploads / "seed.wav"
        source_path.write_bytes(silent_wav())
        seed_upload = extension["artifacts"].register(
            source_path, kind="audio", role="recording_upload"
        )
        seed = WorkflowHandlers(self.database, extension["paths"]).normalize_voice_recording(
            {
                "voice_id": voice["id"],
                "source_artifact_id": seed_upload.id,
                "expected_voice_revision": voice["revision"],
                "ffmpeg_executable": "ffmpeg",
            },
            lambda *_args: None,
            threading.Event(),
        )
        with self.database.session() as session:
            revision = session.get(Voice, voice["id"]).revision
            job_count = len(list(session.scalars(select(Job)).all()))
        response = self.client.post(
            f"/api/v1/voices/{voice['id']}/samples/{seed['sample_id']}/replace",
            data={
                "file": (io.BytesIO(silent_wav()), "replacement.wav"),
                "noise_reduction": "superclean",
            },
            content_type="multipart/form-data",
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": f'"{revision}"',
            },
        )
        self.assertEqual(422, response.status_code, response.get_json())
        with self.database.session() as session:
            self.assertEqual(
                job_count, len(list(session.scalars(select(Job)).all()))
            )
            self.assertEqual(revision, session.get(Voice, voice["id"]).revision)


if __name__ == "__main__":
    unittest.main()
