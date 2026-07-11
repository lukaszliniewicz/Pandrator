import json
import os
import sqlite3
import tempfile
import unittest
import io
from pathlib import Path

from sqlalchemy import func, select

from pandrator.runtime import DataPaths, PathBoundaryError
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue, Worker, noop_handler
from pandrator.web.legacy_migration import import_legacy_data
from pandrator.web.models import DocumentRevision, ProviderModel, Segment, SessionRecord
from pandrator.web.sessions import SessionService


class DataPathsTests(unittest.TestCase):
    def test_managed_paths_are_independent_of_working_directory_and_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            previous = os.getcwd()
            try:
                os.chdir(Path(directory).parent)
                self.assertEqual(paths.managed_path("sessions/example"), Path(directory).resolve() / "sessions" / "example")
            finally:
                os.chdir(previous)

            with self.assertRaises(PathBoundaryError):
                paths.managed_path("../escape.txt")

    def test_external_paths_require_an_allowlisted_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            source = Path(directory) / "source.txt"
            source.write_text("hello", encoding="utf-8")
            paths = DataPaths.from_value(other).ensure()
            self.assertEqual(paths.allowed_external_path(source, [directory]), source.resolve())
            with self.assertRaises(PathBoundaryError):
                paths.allowed_external_path(source, [other])


class LegacyMigrationTests(unittest.TestCase):
    def _legacy_fixture(self, root: Path):
        outputs = root / "Outputs" / "Legacy Book"
        outputs.mkdir(parents=True)
        (outputs / "session_config.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "state": {
                        "workflow": {
                            "workflow_kind": "voiceover",
                            "workflow_preset": "voiceover",
                            "included_stages": ["correct", "generate_audio"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (outputs / "Legacy Book_sentences.json").write_text(
            json.dumps(
                [
                    {"sentence_number": "1", "original_sentence": "First sentence."},
                    {"sentence_number": "2", "processed_sentence": "Second sentence."},
                ]
            ),
            encoding="utf-8",
        )
        (outputs / "preview.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

        database_path = root / "pandrator_state.sqlite3"
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE app_settings_current (
                singleton_id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE sessions (
                session_name TEXT PRIMARY KEY,
                session_path TEXT,
                status TEXT,
                dubbing_mode INTEGER,
                trashed_at TEXT
            );
            """
        )
        settings = {
            "llm": {
                "default_model": "openai/demo",
                "provider_configs": [
                    {
                        "provider": "openai",
                        "name": "OpenAI",
                        "api_key": "must-not-migrate",
                        "models": [
                            {
                                "id": "demo",
                                "default_temperature": 0,
                                "cached_input_cost_per_million": 0.1,
                            }
                        ],
                    }
                ],
            }
        }
        connection.execute("INSERT INTO app_settings_current VALUES (1, ?)", (json.dumps(settings),))
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)",
            ("Legacy Book", str(outputs), "idle", 1),
        )
        connection.commit()
        connection.close()
        return database_path

    def test_forward_import_creates_new_database_and_leaves_legacy_untouched(self):
        with tempfile.TemporaryDirectory(prefix="Pandrator migration ") as directory:
            root = Path(directory)
            legacy_database = self._legacy_fixture(root)
            legacy_before = legacy_database.read_bytes()
            paths = DataPaths.from_value(root)
            result = import_legacy_data(paths)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["sessions"], 1)
            self.assertEqual(result["segments"], 2)
            self.assertEqual(legacy_database.read_bytes(), legacy_before)
            self.assertTrue(paths.database.is_file())
            self.assertTrue(paths.migration_marker.is_file())

            database = Database(paths.database)
            with database.session() as session:
                record = session.scalar(select(SessionRecord))
                self.assertEqual(record.name, "Legacy Book")
                self.assertEqual(record.workflow_kind, "voiceover")
                self.assertNotEqual(record.storage_key, record.name)
                self.assertEqual(session.scalar(select(func.count()).select_from(Segment)), 2)
                model = session.scalar(select(ProviderModel))
                self.assertEqual(model.default_temperature, 0)
                self.assertTrue(model.is_default)
            database.dispose()

            second = import_legacy_data(paths)
            self.assertEqual(second["completed_at"], result["completed_at"])


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "web.sqlite3"
        upgrade_database(self.database_path)
        self.database = Database(self.database_path)
        self.sessions = SessionService(self.database)
        self.queue = JobQueue(self.database)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_worker_completes_job_and_records_events(self):
        job = self.queue.enqueue("noop", {"echo": "ready"})
        worker = Worker(self.queue, "worker-one", {"noop": noop_handler})
        self.assertTrue(worker.run_once())
        completed = self.queue.get(job.id)
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.result_json["echo"], "ready")
        self.assertEqual([event.event_type for event in self.queue.events_after()], ["job.queued", "job.started", "job.succeeded"])

    def test_session_jobs_are_serialized_while_other_sessions_can_run(self):
        first_session = self.sessions.create("First")
        second_session = self.sessions.create("Second")
        first = self.queue.enqueue("noop", session_id=first_session.id)
        blocked = self.queue.enqueue("noop", session_id=first_session.id)
        available = self.queue.enqueue("noop", session_id=second_session.id)

        claimed_first = self.queue.claim("worker-one")
        claimed_second = self.queue.claim("worker-two")
        self.assertEqual(claimed_first.id, first.id)
        self.assertEqual(claimed_second.id, available.id)
        self.assertEqual(self.queue.get(blocked.id).status, "queued")

    def test_canceling_queued_job_is_terminal(self):
        job = self.queue.enqueue("noop")
        canceled = self.queue.request_cancel(job.id)
        self.assertEqual(canceled.status, "canceled")
        self.assertIsNone(self.queue.claim("worker"))


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bootstrap = BootstrapTokenStore()
        self.token = self.bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=self.bootstrap)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def authenticate(self):
        response = self.client.post("/api/v1/auth/bootstrap", json={"token": self.token})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrf_token"]

    def test_bootstrap_is_single_use_and_session_mutations_require_csrf(self):
        csrf = self.authenticate()
        reused = self.client.post("/api/v1/auth/bootstrap", json={"token": self.token})
        self.assertEqual(reused.status_code, 401)

        without_csrf = self.client.post("/api/v1/sessions", json={"name": "Rejected"})
        self.assertEqual(without_csrf.status_code, 403)
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Web Session", "workflow_kind": "subtitles"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["workflow_kind"], "subtitles")
        self.assertEqual(created.headers["ETag"], '"1"')

    def test_revision_conflicts_do_not_overwrite(self):
        csrf = self.authenticate()
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Original"},
            headers={"X-CSRF-Token": csrf},
        )
        session_id = created.get_json()["id"]
        updated = self.client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"name": "Updated"},
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
        )
        self.assertEqual(updated.status_code, 200)
        conflict = self.client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"name": "Stale"},
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
        )
        self.assertEqual(conflict.status_code, 409)

    def test_static_shell_and_health_are_available_without_authentication(self):
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/_app/immutable/entry/", response.data)
        response.close()

    def test_workflow_is_source_aware_and_stage_run_queues_durable_job(self):
        csrf = self.authenticate()
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Subtitle review", "workflow_kind": "voiceover"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        session_id = created["id"]
        initial = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
        self.assertEqual(initial["stages"][0]["status"], "unavailable")

        uploaded = self.client.post(
            "/api/v1/uploads",
            data={
                "session_id": session_id,
                "file": (io.BytesIO(b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"), "source.srt"),
            },
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        snapshot = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
        by_key = {stage["key"]: stage for stage in snapshot["stages"]}
        self.assertEqual(by_key["transcribe"]["status"], "unavailable")
        self.assertEqual(by_key["correct"]["status"], "ready")

        queued = self.client.post(
            f"/api/v1/sessions/{session_id}/stages/correct/run",
            json={"correction_model": "default"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.get_json()["kind"], "dubbing.correct")


if __name__ == "__main__":
    unittest.main()
