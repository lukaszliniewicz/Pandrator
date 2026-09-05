import shutil
import tempfile
import threading
import unittest
from unittest import mock

from pydub.generators import Sine
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
)
from tests.web_test_support import prepare_web_test_data_root


class AudioPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.bootstrap = BootstrapTokenStore()
        token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
        )
        self.client = self.app.test_client()
        self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        extension = self.app.extensions["pandrator"]
        self.database = extension["database"]
        self.artifacts = extension["artifacts"]
        self.sessions = extension["sessions"]
        self.session = self.sessions.create("Audio preview")
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.handlers = extension["workflow_handlers"]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _source(self, name: str = "source.wav"):
        path = self.session_dir / name
        path.write_bytes(b"source")
        return self.artifacts.register(
            path,
            kind="audio",
            role="upload",
            session_id=self.session.id,
        )

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
    def test_handler_transcodes_and_registers_parent_lineage(self):
        source_path = self.session_dir / "source.wav"
        Sine(440).to_audio_segment(duration=250).export(source_path, format="wav")
        source = self.artifacts.register(
            source_path,
            kind="audio",
            role="upload",
            session_id=self.session.id,
        )

        result = self.handlers.generate_audio_preview(
            {"source_artifact_id": source.id}, lambda *_args: None, threading.Event()
        )

        self.assertEqual(source.id, result["source_artifact_id"])
        with self.database.session() as session:
            preview = session.get(Artifact, result["artifact_id"])
            self.assertEqual("audio", preview.kind)
            self.assertEqual("source_audio_preview", preview.role)
            self.assertEqual("audio/mpeg", preview.mime_type)
            self.assertEqual("v1", preview.metadata_json["preview_version"])
            self.assertEqual(source.id, preview.metadata_json["source_artifact_id"])
            self.assertEqual(
                {source.id},
                set(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == preview.id
                        )
                    ).all()
                ),
            )
            destination = self.paths.managed_path(preview.relative_path)
            self.assertTrue(destination.is_file())
            self.assertEqual(64, len(preview.settings_hash or ""))
        self.assertFalse(
            any(
                path.name.startswith(".audio-preview-v1-")
                for path in self.session_dir.iterdir()
            )
        )

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
    def test_cancellation_and_failure_remove_temporary_preview(self):
        source = self._source()
        destination_dir = self.session_dir
        with (
            mock.patch(
                "pandrator.web.media_process.run_media_process",
                side_effect=RuntimeError("bad ffmpeg"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.handlers.generate_audio_preview(
                {"source_artifact_id": source.id},
                lambda *_args: None,
                threading.Event(),
            )
        self.assertFalse(
            any(
                path.name.startswith(".audio-preview-v1-")
                for path in destination_dir.iterdir()
            )
        )

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
    def test_failed_retry_restores_existing_preview_destination(self):
        source = self._source("retry.wav")
        destination = self.session_dir / f"audio-preview-v1-{source.id}.mp3"
        destination.write_bytes(b"previous-valid-preview")

        def write_temporary(command, *, cancel_event):
            del cancel_event
            with open(command[-1], "wb") as output:
                output.write(b"replacement-preview")

        with (
            mock.patch(
                "pandrator.web.media_process.run_media_process",
                side_effect=write_temporary,
            ),
            mock.patch.object(
                self.handlers.artifacts,
                "register",
                side_effect=RuntimeError("registration failed"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.handlers.generate_audio_preview(
                {"source_artifact_id": source.id},
                lambda *_args: None,
                threading.Event(),
            )

        self.assertEqual(b"previous-valid-preview", destination.read_bytes())
        self.assertFalse(
            any(
                path.name.startswith(".audio-preview-v1-")
                for path in self.session_dir.iterdir()
            )
        )

    def test_cancel_during_registration_restores_existing_preview(self):
        source = self._source("cancel.wav")
        destination = self.session_dir / f"audio-preview-v1-{source.id}.mp3"
        destination.write_bytes(b"previous-valid-preview")
        cancel_event = threading.Event()

        def write_temporary(command, *, cancel_event):
            del cancel_event
            with open(command[-1], "wb") as output:
                output.write(b"replacement-preview")

        def cancel_before_registration(value, _detail=None):
            if value >= 0.9:
                cancel_event.set()

        with (
            mock.patch(
                "pandrator.web.media_process.run_media_process",
                side_effect=write_temporary,
            ),
            mock.patch.object(self.handlers.artifacts, "register") as register,
        ):
            result = self.handlers.generate_audio_preview(
                {"source_artifact_id": source.id},
                cancel_before_registration,
                cancel_event,
            )

        self.assertEqual({}, result)
        register.assert_not_called()
        self.assertEqual(b"previous-valid-preview", destination.read_bytes())
        self.assertFalse(
            any(
                path.name.startswith(".audio-preview-v1-")
                for path in self.session_dir.iterdir()
            )
        )

    def test_transcode_error_does_not_expose_managed_paths(self):
        source = self._source("private-source.wav")
        with (
            mock.patch(
                "pandrator.web.media_process.run_media_process",
                side_effect=RuntimeError(
                    f"ffmpeg failed for {self.session_dir}/private-source.wav"
                ),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "The source audio preview could not be prepared.",
            ) as raised,
        ):
            self.handlers.generate_audio_preview(
                {"source_artifact_id": source.id},
                lambda *_args: None,
                threading.Event(),
            )
        self.assertNotIn(str(self.session_dir), str(raised.exception))

    def test_input_resolution_error_does_not_expose_managed_paths(self):
        with (
            mock.patch.object(
                self.handlers,
                "_resolve_input",
                side_effect=FileNotFoundError(
                    f"Missing {self.session_dir}/private-source.wav"
                ),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "The source audio preview could not be prepared.",
            ) as raised,
        ):
            self.handlers.generate_audio_preview(
                {"source_artifact_id": "missing"},
                lambda *_args: None,
                threading.Event(),
            )
        self.assertNotIn(str(self.session_dir), str(raised.exception))

    def test_endpoint_deduplicates_jobs_and_uses_parent_based_ready_lookup(self):
        source = self._source("endpoint.wav")
        first = self.client.get(f"/api/v1/artifacts/{source.id}/audio-preview")
        self.assertEqual(202, first.status_code, first.get_json())
        first_payload = first.get_json()
        self.assertEqual("queued", first_payload["status"])
        second = self.client.get(f"/api/v1/artifacts/{source.id}/audio-preview")
        self.assertEqual(first_payload, second.get_json())
        with self.database.session() as session:
            job = session.get(Job, first_payload["job_id"])
            self.assertEqual("audio.preview", job.kind)
            self.assertIsNone(job.session_id)
            self.assertEqual({"source_artifact_id": source.id}, job.payload_json)
            self.assertEqual(
                [f"artifact:audio-preview:{source.id}"], job.resource_keys_json
            )
            job.status = "running"

        wrong_parent_path = self.session_dir / "wrong-parent.mp3"
        wrong_parent_path.write_bytes(b"wrong")
        wrong_parent = self.artifacts.register(
            wrong_parent_path,
            kind="audio",
            role="source_audio_preview",
            session_id=self.session.id,
        )
        self.assertNotEqual(source.id, wrong_parent.id)
        pending = self.client.get(f"/api/v1/artifacts/{source.id}/audio-preview")
        self.assertEqual(first_payload["job_id"], pending.get_json()["job_id"])
        self.assertEqual("running", pending.get_json()["status"])

        preview_path = self.session_dir / f"ready-{source.id}.mp3"
        preview_path.write_bytes(b"ready")
        preview = self.artifacts.register(
            preview_path,
            kind="audio",
            role="source_audio_preview",
            session_id=self.session.id,
            parent_ids=[source.id],
        )
        ready = self.client.get(f"/api/v1/artifacts/{source.id}/audio-preview")
        self.assertEqual(200, ready.status_code, ready.get_json())
        self.assertEqual("ready", ready.get_json()["status"])
        self.assertEqual(preview.id, ready.get_json()["artifact_id"])
        self.assertEqual(
            f"/api/v1/artifacts/{preview.id}/content",
            ready.get_json()["content_url"],
        )

    def test_workflow_snapshot_projects_resumed_generation_job_and_segment_progress(
        self,
    ):
        with self.database.session() as session:
            plan = GenerationPlan(session_id=self.session.id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                content_hash="plan-hash",
                settings_json={},
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            session.add_all(
                [
                    GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=0,
                        text="Done",
                        status="completed",
                    ),
                    GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=1,
                        text="Still waiting",
                        status="ready",
                    ),
                    GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=2,
                        text="Removed",
                        status="completed",
                        removed=True,
                    ),
                ]
            )
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision.id,
                sequence_number=1,
                operation="generate",
                status="running",
            )
            session.add(run)
            session.flush()
            wrapper = Job(
                kind="workflow.continue",
                session_id=self.session.id,
                status="succeeded",
                progress=1.0,
                payload_json={"session_id": self.session.id},
            )
            direct = Job(
                kind="generation.run",
                session_id=self.session.id,
                status="running",
                progress=0.8,
                progress_detail="Generating resumed run",
                payload_json={"generation_run_id": run.id},
            )
            session.add_all([wrapper, direct])
            session.flush()
            run.job_id = direct.id

            replacement_revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=2,
                content_hash="replacement-plan-hash",
                settings_json={},
            )
            session.add(replacement_revision)
            session.flush()
            session.add(
                GenerationSegment(
                    plan_revision_id=replacement_revision.id,
                    ordinal=0,
                    text="New plan segment",
                    status="ready",
                )
            )
            plan.active_revision_id = replacement_revision.id

        snapshot = self.app.extensions["pandrator"]["workflows"].snapshot(
            self.session.id
        )
        stage = next(
            item for item in snapshot["stages"] if item["key"] == "generate_audio"
        )
        self.assertEqual(direct.id, stage["job_id"])
        self.assertEqual(0.5, stage["progress"])
        self.assertEqual("segments", stage["progress_basis"])
        self.assertEqual("Generating resumed run", stage["detail"])


if __name__ == "__main__":
    unittest.main()
