import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database, upgrade_database
from pandrator.web.models import Provider, ProviderModel, TrainingRun, UsageEvent
from pandrator.web.provider_settings import build_llm_settings
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from pandrator.web.jobs import JobQueue


class ProviderSettingsTests(unittest.TestCase):
    def test_database_models_preserve_zero_temperature_reasoning_and_secret_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            upgrade_database(paths.database)
            database = Database(paths.database)
            os.environ["PANDRATOR_TEST_KEY"] = "secret-value"
            try:
                with database.session() as session:
                    provider = Provider(provider_key="openai", label="OpenAI", secret_ref="env:PANDRATOR_TEST_KEY")
                    session.add(provider)
                    session.flush()
                    session.add(ProviderModel(provider_id=provider.id, model_id="gpt-test", is_default=True, default_temperature=0.0, default_reasoning_effort="custom-fast"))
                settings, model = build_llm_settings(database, paths, request_timeout_seconds=777)
                self.assertEqual(model, "openai/gpt-test")
                record = settings.provider_configs[0]["models"][0]
                self.assertEqual(record["default_temperature"], 0.0)
                self.assertEqual(record["default_reasoning_effort"], "custom-fast")
                self.assertEqual(settings.provider_configs[0]["api_key_env"], "PANDRATOR_TEST_KEY")
                self.assertNotIn("secret-value", str(settings.provider_configs))
                self.assertEqual(settings.request_timeout_seconds, 777)
            finally:
                os.environ.pop("PANDRATOR_TEST_KEY", None)
                database.dispose()


class AdvancedApiTests(unittest.TestCase):
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

    def upload(self, name: str, body: bytes = b"fixture") -> str:
        response = self.client.post(
            "/api/v1/uploads",
            data={"file": (io.BytesIO(body), name)},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()["artifact_id"]

    def test_training_creation_is_durable_and_cancelable(self):
        source_id = self.upload("training.wav")
        created = self.client.post(
            "/api/v1/training",
            json={"model_name": "narrator", "source_artifact_id": source_id, "settings": {"epochs": 2}},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(created.status_code, 202)
        payload = created.get_json()
        self.assertEqual(payload["kind"], "training.xtts")
        canceled = self.client.post(
            f"/api/v1/training/{payload['training_id']}/cancel",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(canceled.status_code, 202)
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            record = session.get(TrainingRun, payload["training_id"])
            self.assertEqual(record.status, "cancel_requested")

    def test_rvc_upload_and_conversion_are_managed_jobs(self):
        weights = self.upload("voice.pth")
        index = self.upload("voice.index")
        audio = self.upload("voice.wav")
        uploaded = self.client.post(
            "/api/v1/rvc/models",
            json={"pth_artifact_id": weights, "index_artifact_id": index},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(uploaded.status_code, 202)
        self.assertEqual(uploaded.get_json()["kind"], "rvc.model.upload")
        converted = self.client.post(
            "/api/v1/rvc/convert",
            json={"source_artifact_id": audio, "settings": {"rvc_model": "voice"}},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(converted.status_code, 202)
        self.assertEqual(converted.get_json()["kind"], "rvc.convert")


class AgenticCleaningTests(unittest.TestCase):
    def test_agentic_cleaning_writes_report_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            upgrade_database(paths.database)
            database = Database(paths.database)
            try:
                session_record = SessionService(database).create("Agentic")
                (paths.sessions / session_record.storage_key).mkdir()
                source_path = paths.uploads / "book.txt"
                source_path.write_text("Chapter One\n\nA beginning.", encoding="utf-8")
                source = ArtifactService(database, paths).register(source_path, kind="source", role="upload", session_id=session_record.id)
                with database.session() as session:
                    provider = Provider(provider_key="openai", label="Local", base_url="http://127.0.0.1:1234/v1")
                    session.add(provider); session.flush()
                    session.add(ProviderModel(provider_id=provider.id, model_id="test", is_default=True))
                from pandrator.logic.source_cleaning.models import PipelineResult

                pipeline = PipelineResult(llm_usage={"prompt_tokens": 10, "completion_tokens": 4, "token_details": {"cached_tokens": 3}, "models": ["openai/test"], "cost_usd": 0.01, "cost_sources": ["custom_rates"]})
                with mock.patch("pandrator.logic.source_cleaning.run_cleaning_pipeline", return_value=pipeline):
                    result = WorkflowHandlers(database, paths).clean_source(
                        {"session_id": session_record.id, "source_artifact_id": source.id, "settings": {"agentic": True, "phase_names": ["metadata"]}},
                        lambda *_args: None,
                        threading.Event(),
                    )
                self.assertEqual(result["report"]["pipeline"]["llm_usage"]["prompt_tokens"], 10)
                with database.session() as session:
                    usage = session.scalar(select(UsageEvent).where(UsageEvent.stage == "source_cleaning"))
                    self.assertEqual(usage.cached_input_tokens, 3)
                    self.assertEqual(usage.cost_source, "custom_rates")
            finally:
                database.dispose()


class SourceAwareWorkflowTests(unittest.TestCase):
    def test_srt_workflow_omits_transcription_and_renumbers_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure(); upgrade_database(paths.database); database = Database(paths.database)
            try:
                record = SessionService(database).create("SRT", workflow_kind="subtitles")
                source_path = paths.uploads / "captions.srt"; source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
                ArtifactService(database, paths).register(source_path, kind="source", role="upload", session_id=record.id, metadata={"original_filename":"captions.srt"})
                stages = WorkflowService(database, JobQueue(database)).snapshot(record.id)["stages"]
                self.assertEqual(stages[0]["key"], "correct")
                self.assertEqual([item["number"] for item in stages], list(range(1, len(stages)+1)))
            finally:
                database.dispose()

    def test_reusable_source_is_copied_as_a_managed_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure(); upgrade_database(paths.database); database = Database(paths.database)
            try:
                first = SessionService(database).create("First"); second = SessionService(database).create("Second")
                (paths.sessions / second.storage_key).mkdir()
                source_path = paths.uploads / "book.txt"; source_path.write_text("Text", encoding="utf-8")
                source = ArtifactService(database, paths).register(source_path, kind="source", role="upload", session_id=first.id)
                result = WorkflowHandlers(database, paths).reuse_source({"session_id":second.id,"artifact_id":source.id}, lambda *_:None, threading.Event())
                copied, copied_path = ArtifactService(database, paths).resolve(result["artifact_id"])
                self.assertEqual(copied.session_id, second.id)
                self.assertEqual(copied_path.read_text(encoding="utf-8"), "Text")
            finally:
                database.dispose()

    def test_url_download_rejects_local_network_targets(self):
        with self.assertRaisesRegex(ValueError, "non-public"):
            WorkflowHandlers._validate_download_url("http://127.0.0.1/private")


if __name__ == "__main__":
    unittest.main()
