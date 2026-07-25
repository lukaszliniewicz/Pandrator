import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pandrator.runtime import DataPaths
from pandrator.web.cli import _database, command_worker
from pandrator.web.process_guard import WorkerPresence


class WorkerPresenceTests(unittest.TestCase):
    def test_second_live_worker_is_reported_without_replacing_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "worker.json"
            with WorkerPresence(marker, "first") as first:
                self.assertTrue(first.acquired)
                with WorkerPresence(marker, "second") as second:
                    self.assertFalse(second.acquired)
                    self.assertEqual(os.getpid(), second.conflict["pid"])
                    self.assertEqual("first", second.conflict["worker_id"])
                self.assertTrue(marker.is_file())
                self.assertEqual("first", json.loads(marker.read_text(encoding="utf-8"))["worker_id"])
            self.assertFalse(marker.exists())

    def test_stale_worker_record_is_replaced_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "worker.json"
            marker.write_text(
                json.dumps({"pid": 999999999, "worker_id": "stale", "token": "stale"}),
                encoding="utf-8",
            )
            with WorkerPresence(marker, "replacement") as presence:
                self.assertTrue(presence.acquired)
                self.assertIsNone(presence.conflict)
                payload = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual("replacement", payload["worker_id"])
                self.assertEqual(os.getpid(), payload["pid"])
            self.assertFalse(marker.exists())

    def test_data_paths_places_presence_record_at_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory)
            self.assertEqual(paths.root / "pandrator.worker.presence.json", paths.worker_presence)

    def test_worker_command_warns_when_another_live_worker_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            paths.worker_presence.write_text(
                json.dumps({"pid": os.getpid(), "worker_id": "existing", "token": "existing"}),
                encoding="utf-8",
            )
            arguments = SimpleNamespace(
                data_dir=directory,
                command="worker",
                worker_id="second",
                once=True,
                poll_interval=0.01,
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = command_worker(arguments)

            self.assertEqual(0, result)
            self.assertIn("Another Pandrator worker appears to be active", stderr.getvalue())
            self.assertEqual("existing", json.loads(paths.worker_presence.read_text(encoding="utf-8"))["worker_id"])

    def test_serve_database_warns_when_foreign_supervisor_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            paths.instance_lock.write_text(
                json.dumps({"pid": os.getpid(), "instance_id": "foreign"}),
                encoding="utf-8",
            )
            arguments = SimpleNamespace(data_dir=directory, command="serve")
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"PANDRATOR_SUPERVISOR_INSTANCE": ""}), redirect_stderr(stderr):
                _paths, database = _database(arguments)
            database.dispose()

            self.assertIn("owned by running Pandrator supervisor", stderr.getvalue())
            self.assertIn("Starting another web supervisor", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
