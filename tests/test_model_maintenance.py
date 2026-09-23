import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from pathlib import Path

from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.model_maintenance import (
    MANAGER_TERMINAL_OPERATION_STATES,
    ensure_app_job_allowed,
    has_active_audio_cpp_maintenance,
    managed_manager_database,
)
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.errors import ManagerError
from pandrator_manager.model_maintenance import (
    ensure_application_quiescent_for_model_maintenance,
    plan_touches_audio_cpp,
)
from pandrator_manager.models import TERMINAL_OPERATION_STATES


def _create_manager_database(root: Path, *, state: str, component_id: str) -> Path:
    database = root / "state" / "manager.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "tasks": [
            {
                "id": "stage",
                "kind": "stage_component",
                "label": "Stage",
                "component_id": component_id,
            }
        ]
    }
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL
            );
            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO plans(plan_id, plan_json) VALUES (?, ?)",
            ("plan-1", json.dumps(plan)),
        )
        connection.execute(
            """
            INSERT INTO operations(operation_id, plan_id, state, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("operation-1", "plan-1", state, 1.0),
        )
    return database


def _create_application_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_database(path)


def _create_minimal_application_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE generation_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )


class ModelMaintenanceGuardTests(unittest.TestCase):
    def test_web_terminal_state_mirror_matches_manager_model(self):
        self.assertEqual(
            MANAGER_TERMINAL_OPERATION_STATES,
            {state.value for state in TERMINAL_OPERATION_STATES},
        )

    def test_standalone_app_skips_manager_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pandrator.sqlite3"
            self.assertIsNone(managed_manager_database(database))
            ensure_app_job_allowed(database)

    def test_web_rejects_audio_cpp_operation_and_caller_rollback_removes_job(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            base = Path(directory) / "Pandrator"
            database_path = base / "data" / "pandrator.sqlite3"
            _create_application_database(database_path)
            _create_manager_database(
                base,
                state="queued",
                component_id="audio_cpp",
            )
            database = Database(database_path)
            cleanup.callback(database.dispose)
            queue = JobQueue(database)

            with self.assertRaisesRegex(ValueError, "audio.cpp model maintenance"):
                with database.session() as session:
                    queue.enqueue_in_session(session, "noop", {})

            with closing(sqlite3.connect(database_path)) as connection, connection:
                count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            self.assertEqual(0, count)

    def test_terminal_or_unrelated_manager_operation_does_not_block_app(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Pandrator"
            database_path = base / "data" / "pandrator.sqlite3"
            _create_application_database(database_path)
            _create_manager_database(base, state="succeeded", component_id="xtts")
            self.assertFalse(
                has_active_audio_cpp_maintenance(base / "state" / "manager.sqlite3")
            )
            ensure_app_job_allowed(database_path)

    def test_malformed_known_manager_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Pandrator"
            database_path = base / "data" / "pandrator.sqlite3"
            _create_application_database(database_path)
            manager_database = base / "state" / "manager.sqlite3"
            manager_database.parent.mkdir(parents=True, exist_ok=True)
            manager_database.write_text("not sqlite", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "managed Manager database"):
                ensure_app_job_allowed(database_path)

    def test_active_manager_operation_without_plan_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Pandrator"
            database_path = base / "data" / "pandrator.sqlite3"
            _create_application_database(database_path)
            manager_database = _create_manager_database(
                base,
                state="succeeded",
                component_id="xtts",
            )
            with closing(sqlite3.connect(manager_database)) as connection, connection:
                connection.execute(
                    "INSERT INTO operations(operation_id, plan_id, state, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("operation-missing-plan", "missing-plan", "queued", 2.0),
                )

            with self.assertRaisesRegex(ValueError, "operation plan"):
                ensure_app_job_allowed(database_path)

    def test_terminal_historical_plan_is_not_parsed_by_active_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Pandrator"
            database_path = base / "data" / "pandrator.sqlite3"
            _create_application_database(database_path)
            manager_database = _create_manager_database(
                base,
                state="succeeded",
                component_id="xtts",
            )
            with closing(sqlite3.connect(manager_database)) as connection, connection:
                connection.execute(
                    "INSERT INTO plans(plan_id, plan_json) VALUES (?, ?)",
                    ("malformed-history", "not json"),
                )
                connection.execute(
                    "INSERT INTO operations(operation_id, plan_id, state, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("operation-history", "malformed-history", "failed", 2.0),
                )

            ensure_app_job_allowed(database_path)

    def test_valid_descriptor_for_another_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Pandrator"
            other = Path(directory) / "other"
            database_path = base / "data" / "pandrator.sqlite3"
            _create_application_database(database_path)
            _create_manager_database(base, state="succeeded", component_id="xtts")
            descriptor = other / "Pandrator" / "state" / "connection.json"
            descriptor.parent.mkdir(parents=True)
            descriptor.write_text(
                json.dumps({"workspace": str(other)}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                managed_manager_database(database_path, descriptor_path=descriptor)

    def test_manager_allows_initial_install_without_app_database(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.state.mkdir(parents=True)
            ensure_application_quiescent_for_model_maintenance(layout)

    def test_manager_rejects_active_job_with_short_writer_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            application_database = layout.data / "pandrator.sqlite3"
            _create_minimal_application_database(application_database)
            with closing(sqlite3.connect(application_database)) as connection, connection:
                connection.execute(
                    "INSERT INTO jobs(id, kind, status, created_at) VALUES (?, ?, ?, ?)",
                    ("job-1", "noop", "queued", 1.0),
                )

            with self.assertRaises(ManagerError) as raised:
                ensure_application_quiescent_for_model_maintenance(layout)
            self.assertEqual("application_busy", raised.exception.code)

            with closing(sqlite3.connect(application_database)) as first, first:
                with closing(sqlite3.connect(application_database)) as second, second:
                    first.execute("BEGIN IMMEDIATE")
                    first.execute(
                        "UPDATE jobs SET status='running' WHERE id='job-1'"
                    )
                    first.commit()
                    second.execute("BEGIN")
                    self.assertEqual(
                        "running",
                        second.execute(
                            "SELECT status FROM jobs WHERE id='job-1'"
                        ).fetchone()[0],
                    )

    def test_manager_first_and_app_first_orderings_have_deterministic_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Pandrator"
            app_database = base / "data" / "pandrator.sqlite3"
            _create_minimal_application_database(app_database)

            # Manager-first: the durable manager operation makes app enqueue
            # fail before its caller commits the queued job.
            manager_database = _create_manager_database(
                base,
                state="running",
                component_id="audio_cpp",
            )
            self.assertTrue(has_active_audio_cpp_maintenance(manager_database))
            with self.assertRaises(ValueError):
                ensure_app_job_allowed(app_database)

            # App-first: an independent writer transaction is visible to the
            # manager's BEGIN IMMEDIATE check once the app transaction commits.
            ready = threading.Event()
            checked = threading.Event()
            failure: list[Exception] = []
            first = sqlite3.connect(app_database, isolation_level=None)
            first.execute("BEGIN IMMEDIATE")
            first.execute(
                "INSERT INTO jobs(id, kind, status, created_at) VALUES (?, ?, ?, ?)",
                ("job-1", "noop", "queued", 1.0),
            )
            ready.set()

            def manager_check() -> None:
                ready.wait()
                try:
                    ensure_application_quiescent_for_model_maintenance(
                        WorkspaceLayout.from_value(directory)
                    )
                except Exception as error:  # noqa: BLE001 - assert exact outcome below
                    failure.append(error)
                finally:
                    checked.set()

            thread = threading.Thread(target=manager_check)
            thread.start()
            first.commit()
            self.assertTrue(checked.wait(timeout=3))
            thread.join(timeout=3)
            first.close()
            self.assertEqual(1, len(failure))
            self.assertIsInstance(failure[0], ManagerError)

    def test_typed_manager_plan_uses_audio_cpp_component_id(self):
        class Task:
            def __init__(self, component_id):
                self.component_id = component_id

        class Plan:
            def __init__(self, *component_ids):
                self.tasks = tuple(Task(component_id) for component_id in component_ids)

        self.assertTrue(plan_touches_audio_cpp(Plan("audio_cpp")))
        self.assertFalse(plan_touches_audio_cpp(Plan("audiocpp")))
        self.assertFalse(plan_touches_audio_cpp(Plan()))


if __name__ == "__main__":
    unittest.main()
