import tempfile
import threading
import time
import unittest
from unittest import mock

from pandrator.web.api import create_app
from pandrator.web.database import Database
from pandrator.web.startup import StartupMaintenance
from tests.web_test_support import prepare_web_test_data_root


class StartupMaintenanceTests(unittest.TestCase):
    def test_maintenance_thread_does_not_block_application_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            uploads = mock.Mock()
            uploads.cleanup_expired.return_value = 3
            started = threading.Event()
            release = threading.Event()

            def delayed_retention(_database, _paths, retention_days):
                self.assertEqual(30, retention_days)
                started.set()
                release.wait(10)
                return {"removed_sessions": 2}

            runner = StartupMaintenance(database, paths, uploads)
            try:
                with mock.patch(
                    "pandrator.web.startup.apply_retention",
                    side_effect=delayed_retention,
                ):
                    before = time.monotonic()
                    runner.start()
                    elapsed = time.monotonic() - before
                    self.assertLess(elapsed, 0.5)
                    self.assertTrue(started.wait(2))
                    self.assertFalse(runner.completed)
                    release.set()
                    self.assertTrue(runner.wait(2))
                self.assertEqual(3, runner.result["expired_uploads"])
                self.assertEqual(
                    {"removed_sessions": 2},
                    runner.result["retention"],
                )
            finally:
                release.set()
                database.dispose()

    def test_application_starts_background_maintenance_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as enabled_directory:
            prepare_web_test_data_root(enabled_directory)
            with mock.patch(
                "pandrator.web.startup.StartupMaintenance.start"
            ) as start:
                app = create_app(
                    data_root=enabled_directory,
                    testing=True,
                    background_maintenance=True,
                )
            try:
                start.assert_called_once_with()
            finally:
                app.extensions["pandrator"]["database"].dispose()

        with tempfile.TemporaryDirectory() as testing_directory:
            prepare_web_test_data_root(testing_directory)
            with mock.patch(
                "pandrator.web.startup.StartupMaintenance.start"
            ) as start:
                app = create_app(
                    data_root=testing_directory,
                    testing=True,
                )
            try:
                start.assert_not_called()
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_one_maintenance_failure_does_not_skip_the_other_task(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            uploads = mock.Mock()
            uploads.cleanup_expired.return_value = 4
            runner = StartupMaintenance(database, paths, uploads)
            try:
                with mock.patch(
                    "pandrator.web.startup.apply_retention",
                    side_effect=RuntimeError("retention failed"),
                ):
                    result = runner.run()
                self.assertEqual({"expired_uploads": 4}, result)
                self.assertEqual("retention:RuntimeError", runner.error)
                uploads.cleanup_expired.assert_called_once_with()
            finally:
                database.dispose()


if __name__ == "__main__":
    unittest.main()
