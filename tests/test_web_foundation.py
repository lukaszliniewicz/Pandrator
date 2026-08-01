import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest import mock

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from pandrator.runtime import DataPaths, PathBoundaryError
from pandrator.web.api import create_app
from pandrator.web.auth import (
    ALL_SCOPES,
    AuthService,
    BootstrapTokenStore,
)
from pandrator.web.database import SCHEMA_HEAD, Database, sqlite_url, upgrade_database
from pandrator.web.jobs import JobQueue, Worker, noop_handler
from pandrator.web.legacy_migration import import_legacy_data
from pandrator.web.models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    Job,
    JobEvent,
    ProviderModel,
    Segment,
    SessionRecord,
    SessionSource,
    SourceAsset,
    SourceRecord,
    utcnow,
)
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


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


class SchemaUpgradeTests(unittest.TestCase):
    def test_legacy_api_tokens_gain_compatible_admin_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pandrator.sqlite3"
            upgrade_database(database_path)
            config = Config()
            config.set_main_option(
                "script_location",
                str(
                    Path(__file__).parents[1]
                    / "pandrator"
                    / "web"
                    / "migrations"
                ),
            )
            config.set_main_option(
                "sqlalchemy.url",
                sqlite_url(database_path),
            )
            command.downgrade(config, "0022_source_asset_backfill")
            raw = "pan_legacy_compatible_token"
            token_id = "11111111-1111-4111-8111-111111111111"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO api_tokens (
                        id, label, token_hash, token_prefix, created_at,
                        last_used_at, revoked_at
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL, NULL)
                    """,
                    (
                        token_id,
                        "Legacy token",
                        hashlib.sha256(raw.encode()).hexdigest(),
                        raw[:12],
                    ),
                )
                connection.commit()

            upgrade_database(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT subject, scopes_json, principal_kind, created_by
                    FROM api_tokens WHERE id = ?
                    """,
                    (token_id,),
                ).fetchone()
            self.assertEqual(f"api-token:{token_id}", row[0])
            self.assertEqual(ALL_SCOPES, frozenset(json.loads(row[1])))
            self.assertEqual("api_token", row[2])
            self.assertEqual("legacy-migration", row[3])

            database = Database(database_path)
            try:
                principal = AuthService(database).resolve_api_token(
                    raw,
                    network_zone="loopback",
                    target_instance_id="current-instance",
                )
                self.assertIsNotNone(principal)
                self.assertTrue(principal.has_scope("app.admin"))
            finally:
                database.dispose()

    def test_existing_web_preview_database_upgrades_from_previous_head(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pandrator.sqlite3"
            upgrade_database(database_path)
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"))
            config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
            command.downgrade(config, "0003_parity_workspace")
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual("0003_parity_workspace", connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])
                self.assertNotIn("job_id", [row[1] for row in connection.execute("PRAGMA table_info(output_assemblies)")])
                self.assertNotIn("source_text_artifact_id", [row[1] for row in connection.execute("PRAGMA table_info(training_runs)")])
            upgrade_database(database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(SCHEMA_HEAD, connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])
                self.assertIn("job_id", [row[1] for row in connection.execute("PRAGMA table_info(output_assemblies)")])
                self.assertIn("source_text_artifact_id", [row[1] for row in connection.execute("PRAGMA table_info(training_runs)")])
                self.assertIn("node_kind", [row[1] for row in connection.execute("PRAGMA table_info(generation_segments)")])
                self.assertIn("paragraph_break_after", [row[1] for row in connection.execute("PRAGMA table_info(generation_segments)")])
                self.assertIn("voice", [row[1] for row in connection.execute("PRAGMA table_info(generation_segments)")])
                self.assertIn("speaker", [row[1] for row in connection.execute("PRAGMA table_info(generation_segments)")])
                self.assertIn("speech_plan_json", [row[1] for row in connection.execute("PRAGMA table_info(generation_segments)")])
                self.assertIn("node_kind", [row[1] for row in connection.execute("PRAGMA table_info(generation_segment_revisions)")])
                self.assertIn("paragraph_break_after", [row[1] for row in connection.execute("PRAGMA table_info(generation_segment_revisions)")])
                self.assertIn("voice", [row[1] for row in connection.execute("PRAGMA table_info(generation_segment_revisions)")])
                self.assertIn("speaker", [row[1] for row in connection.execute("PRAGMA table_info(generation_segment_revisions)")])
                self.assertIn("speech_plan_json", [row[1] for row in connection.execute("PRAGMA table_info(generation_segment_revisions)")])
                self.assertIn("kind", [row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")])
                self.assertIn("stored_credentials", [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])
                self.assertIn("research_cache_entries", [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])
                self.assertIn("pronunciation_entries", [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])
                self.assertIn("source_language", [row[1] for row in connection.execute("PRAGMA table_info(sessions)")])
                self.assertIn("target_language", [row[1] for row in connection.execute("PRAGMA table_info(sessions)")])
                self.assertIn("is_active", [row[1] for row in connection.execute("PRAGMA table_info(provider_models)")])
                self.assertIn("session_stage_selections", [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])
                self.assertIn("progress_detail", [row[1] for row in connection.execute("PRAGMA table_info(jobs)")])
                self.assertIn("lease_generation", [row[1] for row in connection.execute("PRAGMA table_info(jobs)")])
                self.assertIn("available_at", [row[1] for row in connection.execute("PRAGMA table_info(jobs)")])
                self.assertIn("lease_generation", [row[1] for row in connection.execute("PRAGMA table_info(resource_claims)")])
                self.assertIn("capability_snapshots", [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")])

    def test_segment_language_equal_to_plan_default_becomes_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pandrator.sqlite3"
            upgrade_database(database_path)
            database = Database(database_path)
            with database.session() as session:
                workspace = SessionRecord(name="Language migration", storage_key="language-migration")
                session.add(workspace)
                session.flush()
                plan = GenerationPlan(session_id=workspace.id)
                session.add(plan)
                session.flush()
                revision = GenerationPlanRevision(
                    plan_id=plan.id,
                    revision_number=1,
                    settings_json={"language": "en"},
                    content_hash="language-migration",
                )
                session.add(revision)
                session.flush()
                inherited = GenerationSegment(
                    plan_revision_id=revision.id,
                    ordinal=0,
                    text="Inherited.",
                    language="en",
                )
                overridden = GenerationSegment(
                    plan_revision_id=revision.id,
                    ordinal=1,
                    text="Overridden.",
                    language="pl",
                )
                session.add_all([inherited, overridden])
                session.flush()
                inherited_id, overridden_id = inherited.id, overridden.id
                plan.active_revision_id = revision.id
            database.dispose()

            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"))
            config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
            command.downgrade(config, "0014_usage_event_links")
            upgrade_database(database_path)

            with closing(sqlite3.connect(database_path)) as connection:
                languages = dict(
                    connection.execute(
                        "SELECT id, language FROM generation_segments WHERE id IN (?, ?)",
                        (inherited_id, overridden_id),
                    )
                )
            self.assertIsNone(languages[inherited_id])
            self.assertEqual("pl", languages[overridden_id])

    def test_inline_tts_key_is_migrated_to_write_only_credential_table(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pandrator.sqlite3"
            upgrade_database(database_path)
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"))
            config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
            command.downgrade(config, "0008_generation_paragraph_boundaries")
            secret = "legacy-speech-secret"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value_json, revision, updated_at) VALUES (?, ?, 1, CURRENT_TIMESTAMP)",
                    ("services.tts", json.dumps({"provider_configs": [{"id": "openai", "api_key": secret}], "deepl_api_key": "legacy-deepl-secret"})),
                )
                connection.commit()
            upgrade_database(database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                stored = connection.execute("SELECT secret_value FROM stored_credentials WHERE key = 'shared:openai'").fetchone()
                stored_deepl = connection.execute("SELECT secret_value FROM stored_credentials WHERE key = 'aux:deepl'").fetchone()
                setting = connection.execute("SELECT value_json FROM app_settings WHERE key = 'services.tts'").fetchone()
            self.assertEqual(secret, stored[0])
            self.assertEqual("legacy-deepl-secret", stored_deepl[0])
            self.assertNotIn(secret, setting[0])
            self.assertEqual("db:shared:openai", json.loads(setting[0])["provider_configs"][0]["secret_ref"])

    def test_legacy_sources_are_promoted_once_by_the_marked_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pandrator.sqlite3"
            upgrade_database(database_path)
            config = Config()
            config.set_main_option(
                "script_location",
                str(
                    Path(__file__).parents[1]
                    / "pandrator"
                    / "web"
                    / "migrations"
                ),
            )
            config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
            command.downgrade(config, "0021_query_path_indexes")

            database = Database(database_path)
            with database.session() as session:
                workspace = SessionRecord(
                    name="Legacy source",
                    storage_key="legacy-source",
                )
                session.add(workspace)
                session.flush()
                artifact = Artifact(
                    session_id=workspace.id,
                    kind="text",
                    role="upload",
                    relative_path="uploads/legacy-source.txt",
                    mime_type="text/plain",
                    size_bytes=12,
                    content_hash="legacy-source-hash",
                )
                session.add(artifact)
                session.flush()
                source = SourceRecord(
                    session_id=workspace.id,
                    kind="text",
                    display_name="Legacy source.txt",
                    artifact_id=artifact.id,
                )
                session.add(source)
                session.flush()
                session_id = workspace.id
                artifact_id = artifact.id
                source_id = source.id
            database.dispose()

            upgrade_database(database_path)
            database = Database(database_path)
            with database.session() as session:
                asset = session.scalar(
                    select(SourceAsset).where(
                        SourceAsset.artifact_id == artifact_id
                    )
                )
                self.assertIsNotNone(asset)
                self.assertEqual("Legacy source.txt", asset.display_name)
                self.assertEqual(
                    source_id,
                    asset.metadata_json["legacy_source_id"],
                )
                attachment = session.scalar(
                    select(SessionSource).where(
                        SessionSource.session_id == session_id,
                        SessionSource.source_asset_id == asset.id,
                    )
                )
                self.assertIsNotNone(attachment)
                self.assertEqual("primary", attachment.role)
                counts_before = (
                    session.scalar(select(func.count()).select_from(SourceAsset)),
                    session.scalar(select(func.count()).select_from(SessionSource)),
                )
            database.dispose()

            upgrade_database(database_path)
            database = Database(database_path)
            with database.session() as session:
                counts_after = (
                    session.scalar(select(func.count()).select_from(SourceAsset)),
                    session.scalar(select(func.count()).select_from(SessionSource)),
                )
            database.dispose()
            self.assertEqual(counts_before, counts_after)


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
                    {"sentence_number": "2", "processed_sentence": "Second sentence.", "paragraph": "yes"},
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
                generation_segments = list(session.scalars(select(GenerationSegment).order_by(GenerationSegment.ordinal)).all())
                self.assertEqual(len(generation_segments), 2)
                self.assertFalse(generation_segments[0].paragraph_break_after)
                self.assertTrue(generation_segments[1].paragraph_break_after)
                model = session.scalar(select(ProviderModel))
                self.assertEqual(model.default_temperature, 0)
                self.assertTrue(model.is_default)
            database.dispose()

            marker = json.loads(paths.migration_marker.read_text(encoding="utf-8"))
            self.assertEqual(SCHEMA_HEAD, marker["web_schema"])
            self.assertEqual(1, marker["generation_promotion_version"])
            with mock.patch(
                "pandrator.web.legacy_migration._legacy_session_rows",
                side_effect=AssertionError("completed imports must not rescan legacy sessions"),
            ), mock.patch(
                "pandrator.web.legacy_migration._import_generation",
                side_effect=AssertionError("completed imports must not re-promote generation data"),
            ):
                second = import_legacy_data(paths)
            self.assertEqual(second["completed_at"], result["completed_at"])


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = prepare_web_test_data_root(self.temporary.name).database
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

    def test_canceling_running_job_is_immediately_terminal(self):
        job = self.queue.enqueue("noop")
        claimed = self.queue.claim("worker")
        self.assertEqual(job.id, claimed.id)

        canceled = self.queue.request_cancel(job.id)

        self.assertEqual("canceled", canceled.status)
        self.assertTrue(
            self.queue.should_cancel(
                job.id,
                "worker",
                lease_generation=claimed.lease_generation,
            )
        )
        with self.assertRaises(RuntimeError):
            self.queue.complete(
                job.id,
                "worker",
                {"late": True},
                lease_generation=claimed.lease_generation,
            )

    def test_expired_exhausted_job_is_not_left_running(self):
        job = self.queue.enqueue("noop", max_attempts=1)
        self.queue.claim("worker", lease_seconds=5)
        with self.database.session() as session:
            record = session.get(Job, job.id)
            record.lease_expires_at = utcnow() - timedelta(seconds=1)

        reconciled = self.queue.get(job.id)

        self.assertEqual("failed", reconciled.status)
        self.assertEqual("worker_lease_expired", reconciled.error_code)

    def test_worker_python_logs_are_available_in_job_timeline(self):
        def logged(_payload, _progress, _cancel_event):
            logging.getLogger("pandrator.test").info("provider retry detail")
            return {"ok": True}

        job = self.queue.enqueue("logged")
        Worker(self.queue, "worker-one", {"logged": logged}).run_once()

        captured = [event for event in self.queue.events_for(job.id) if event.event_type == "job.log"]
        self.assertEqual(1, len(captured))
        self.assertEqual("INFO", captured[0].payload_json["level"])
        self.assertEqual("provider retry detail", captured[0].payload_json["message"])

    def test_worker_persists_latest_progress_detail(self):
        def reporting(_payload, progress, _cancel_event):
            progress(0.4, "Processed 4 of 10 units")
            return {"ok": True}

        job = self.queue.enqueue("reporting")
        Worker(self.queue, "worker-one", {"reporting": reporting}).run_once()

        completed = self.queue.get(job.id)
        self.assertEqual(1.0, completed.progress)
        self.assertEqual("Processed 4 of 10 units", completed.progress_detail)
        progress_events = [
            event
            for event in self.queue.events_for(job.id)
            if event.event_type == "job.progress"
        ]
        self.assertEqual("Processed 4 of 10 units", progress_events[-1].payload_json["detail"])

    def test_generation_progress_invalidates_incremental_audio_takes(self):
        session_record = self.sessions.create("Incremental generation")
        job = self.queue.enqueue(
            "audiobook.generate_audio",
            {"generation_run_id": "run-one"},
            session_id=session_record.id,
        )
        claimed = self.queue.claim("worker-one")

        self.queue.heartbeat(
            job.id,
            "worker-one",
            lease_generation=claimed.lease_generation,
            progress=0.4,
            detail="Generated segment 4 of 10",
        )

        progress_event = next(
            event
            for event in reversed(self.queue.events_for(job.id))
            if event.event_type == "job.progress"
        )
        self.assertEqual(
            ["generation", "jobs"],
            progress_event.payload_json["changed_entities"],
        )

    def test_job_progress_does_not_move_backward(self):
        job = self.queue.enqueue("noop")
        claimed = self.queue.claim("worker-one")

        self.queue.heartbeat(
            job.id,
            "worker-one",
            lease_generation=claimed.lease_generation,
            progress=0.7,
            detail="Later unit",
        )
        self.queue.heartbeat(
            job.id,
            "worker-one",
            lease_generation=claimed.lease_generation,
            progress=0.4,
            detail="Delayed earlier unit",
        )

        current = self.queue.get(job.id)
        self.assertEqual(0.7, current.progress)
        self.assertEqual("Later unit", current.progress_detail)


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
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

    def test_event_snapshot_captures_high_water_and_new_streams_do_not_replay(self):
        self.authenticate()
        queue = self.app.extensions["pandrator"]["jobs"]
        workspace = self.app.extensions["pandrator"]["sessions"].create("Event snapshot")
        queue.enqueue(
            "source.clean",
            {
                "source_artifact_id": "artifact-1",
                "api_key": "must-not-stream",
                "settings": {"token": "also-private"},
            },
            session_id=workspace.id,
        )
        with mock.patch(
            "pandrator.web.capabilities.probe_stable_capabilities",
            return_value={},
        ):
            snapshot_response = self.client.get("/api/v1/events/snapshot")
        self.assertEqual(200, snapshot_response.status_code)
        snapshot = snapshot_response.get_json()
        self.assertEqual(queue.event_bounds()["latest"], snapshot["cursor"])
        self.assertIn("sessions", snapshot)
        self.assertIn("jobs", snapshot)
        self.assertIn("capabilities", snapshot)

        response = self.client.get("/api/v1/events", buffered=False)
        try:
            first_chunk = next(response.response).decode()
        finally:
            response.close()
        self.assertEqual(": heartbeat\n\n", first_chunk)

    def test_event_stream_honors_cursor_and_projects_scoped_payload(self):
        self.authenticate()
        queue = self.app.extensions["pandrator"]["jobs"]
        workspace = self.app.extensions["pandrator"]["sessions"].create("Scoped event")
        queue.enqueue(
            "source.clean",
            {
                "source_artifact_id": "artifact-1",
                "api_key": "must-not-stream",
                "settings": {"token": "also-private"},
            },
            session_id=workspace.id,
        )

        response = self.client.get("/api/v1/events?after=0", buffered=False)
        try:
            first_chunk = next(response.response).decode()
        finally:
            response.close()
        self.assertIn("event: job.queued", first_chunk)
        data_line = next(
            line for line in first_chunk.splitlines() if line.startswith("data: ")
        )
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertEqual("source.clean", payload["job_kind"])
        self.assertEqual(workspace.id, payload["session_id"])
        self.assertEqual("artifact-1", payload["source_artifact_id"])
        self.assertEqual(["jobs", "sources", "workflow"], payload["changed_entities"])
        self.assertNotIn("api_key", payload)
        self.assertNotIn("settings", payload)
        self.assertNotIn("must-not-stream", first_chunk)

    def test_event_stream_resets_a_cursor_older_than_retention(self):
        self.authenticate()
        queue = self.app.extensions["pandrator"]["jobs"]
        queue.enqueue("first")
        queue.enqueue("second")
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as db_session:
            oldest = db_session.scalar(
                select(JobEvent).order_by(JobEvent.id.asc()).limit(1)
            )
            db_session.delete(oldest)

        response = self.client.get("/api/v1/events?after=0", buffered=False)
        try:
            first_chunk = next(response.response).decode()
        finally:
            response.close()
        self.assertIn("event: stream.reset", first_chunk)
        self.assertIn('"reason": "cursor_expired"', first_chunk)

    def test_event_stream_prefers_last_event_id_on_reconnect(self):
        self.authenticate()
        queue = self.app.extensions["pandrator"]["jobs"]
        queue.enqueue("first")
        latest = queue.event_bounds()["latest"]

        response = self.client.get(
            "/api/v1/events?after=0",
            headers={"Last-Event-ID": str(latest)},
            buffered=False,
        )
        try:
            first_chunk = next(response.response).decode()
        finally:
            response.close()
        self.assertEqual(": heartbeat\n\n", first_chunk)

    def test_global_event_stream_advances_past_logs_without_exposing_them(self):
        self.authenticate()
        queue = self.app.extensions["pandrator"]["jobs"]
        job = queue.enqueue("first")
        queued_cursor = queue.event_bounds()["latest"]
        queue.log(job.id, "INFO", "private worker detail")

        response = self.client.get(
            f"/api/v1/events?after={queued_cursor}",
            buffered=False,
        )
        try:
            first_chunk = next(response.response).decode()
        finally:
            response.close()
        self.assertIn("event: stream.cursor", first_chunk)
        self.assertNotIn("event: job.log", first_chunk)
        self.assertNotIn("private worker detail", first_chunk)

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

    def test_duplicate_session_name_requires_an_explicit_recoverable_overwrite(self):
        csrf = self.authenticate()
        headers = {"X-CSRF-Token": csrf}
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Existing session"},
            headers=headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        original = created.get_json()

        duplicate = self.client.post(
            "/api/v1/sessions",
            json={"name": "  EXISTING SESSION  "},
            headers=headers,
        )
        self.assertEqual(409, duplicate.status_code)
        self.assertEqual("duplicate_session", duplicate.get_json()["error"]["code"])
        self.assertEqual(original["id"], duplicate.get_json()["error"]["details"]["existing_session"]["id"])

        replaced = self.client.post(
            "/api/v1/sessions",
            json={"name": "Existing session", "overwrite_session_id": original["id"]},
            headers=headers,
        )
        self.assertEqual(201, replaced.status_code, replaced.get_json())
        self.assertNotEqual(original["id"], replaced.get_json()["id"])

        active = self.client.get("/api/v1/sessions").get_json()["items"]
        self.assertEqual([replaced.get_json()["id"]], [item["id"] for item in active])
        including_trash = self.client.get("/api/v1/sessions?include_trashed=true").get_json()["items"]
        old = next(item for item in including_trash if item["id"] == original["id"])
        self.assertEqual("trashed", old["status"])

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

    def test_job_logs_endpoint_returns_the_durable_timeline(self):
        self.authenticate()
        job = self.app.extensions["pandrator"]["jobs"].enqueue("noop", {"echo": "logged"})

        response = self.client.get(f"/api/v1/jobs/{job.id}/logs")

        self.assertEqual(200, response.status_code)
        self.assertEqual("job.queued", response.get_json()["items"][0]["event_type"])

    def test_finalized_export_can_be_removed_without_deleting_source_artifacts(self):
        csrf = self.authenticate()
        headers = {"X-CSRF-Token": csrf}
        record = self.client.post(
            "/api/v1/sessions",
            json={"name": "Removable output", "workflow_kind": "voiceover"},
            headers=headers,
        ).get_json()
        extension = self.app.extensions["pandrator"]
        output_path = extension["paths"].sessions / record["storage_key"] / "exports" / "finished.wav"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"finished export")
        exported = extension["artifacts"].register(
            output_path,
            kind="export",
            role="export_mixed_audio",
            session_id=record["id"],
        )
        assembly_path = output_path.parent / "assembly.wav"
        assembly_path.write_bytes(b"reusable assembly")
        assembly = extension["artifacts"].register(
            assembly_path,
            kind="audio",
            role="assembled_audio",
            session_id=record["id"],
        )

        rejected = self.client.delete(
            f"/api/v1/sessions/{record['id']}/outputs/{assembly.id}",
            headers=headers,
        )
        self.assertEqual(409, rejected.status_code)
        self.assertTrue(assembly_path.is_file())

        removed = self.client.delete(
            f"/api/v1/sessions/{record['id']}/outputs/{exported.id}",
            headers=headers,
        )
        self.assertEqual(200, removed.status_code, removed.get_json())
        self.assertEqual("deleted", removed.get_json()["state"])
        self.assertFalse(output_path.exists())
        visible_ids = {
            item["id"]
            for item in self.client.get(f"/api/v1/artifacts?session_id={record['id']}").get_json()["items"]
        }
        self.assertNotIn(exported.id, visible_ids)
        deleted = self.client.get(
            f"/api/v1/artifacts?session_id={record['id']}&include_deleted=true"
        ).get_json()["items"]
        deleted_export = next(item for item in deleted if item["id"] == exported.id)
        self.assertEqual("deleted", deleted_export["state"])
        self.assertEqual(str(output_path.resolve()), deleted_export["path"])
        self.assertEqual(404, self.client.get(f"/api/v1/artifacts/{exported.id}/content").status_code)
        self.assertEqual([], extension["artifacts"].reconcile(record["id"]))

        output_path.write_bytes(b"replacement export")
        restored = extension["artifacts"].register(
            output_path,
            kind="export",
            role="export_mixed_audio",
            session_id=record["id"],
        )
        self.assertEqual(exported.id, restored.id)
        self.assertEqual("current", restored.state)
        self.assertNotIn("deleted_at", restored.metadata_json)

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
        self.assertNotIn("transcribe", by_key)
        self.assertEqual(by_key["correct"]["number"], 1)
        self.assertEqual(by_key["correct"]["status"], "ready")

        queued = self.client.post(
            f"/api/v1/sessions/{session_id}/stages/correct/run",
            json={"correction_model": "default"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.get_json()["kind"], "dubbing.correct")

    def test_media_preview_waits_for_transcription_artifact(self):
        csrf = self.authenticate()
        record = self.client.post(
            "/api/v1/sessions",
            json={"name": "Media preview", "workflow_kind": "voiceover"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        session_id = record["id"]
        uploaded = self.client.post(
            "/api/v1/uploads",
            data={"session_id": session_id, "file": (io.BytesIO(b"ID3fixture"), "source.mp3")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(201, uploaded.status_code, uploaded.get_json())

        snapshot = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
        self.assertEqual("unavailable", next(stage for stage in snapshot["stages"] if stage["key"] == "preview")["status"])

        extension = self.app.extensions["pandrator"]
        transcription_path = extension["paths"].artifacts / "media-preview.srt"
        transcription_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nReady\n", encoding="utf-8")
        extension["artifacts"].register(
            transcription_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
        )
        ready = self.client.get(f"/api/v1/sessions/{session_id}/workflow").get_json()
        self.assertEqual("ready", next(stage for stage in ready["stages"] if stage["key"] == "preview")["status"])

    def test_session_languages_are_first_class_and_revision_safe(self):
        csrf = self.authenticate()
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Polish narration", "workflow_kind": "audiobook", "source_language": "pl"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 201)
        record = response.get_json()
        self.assertEqual(record["source_language"], "pl")
        self.assertIsNone(record["target_language"])
        changed = self.client.patch(
            f"/api/v1/sessions/{record['id']}",
            json={"target_language": "en"},
            headers={"X-CSRF-Token": csrf, "If-Match": response.headers["ETag"]},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.get_json()["target_language"], "en")


if __name__ == "__main__":
    unittest.main()
