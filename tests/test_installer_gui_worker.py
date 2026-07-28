import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from pandrator_installer.gui.main_window import PandratorInstaller
from pandrator_installer.gui.support import Worker


class InstallerGuiWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["pandrator-installer-worker-tests"]
        )

    def _wait(self, worker):
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        QTimer.singleShot(5000, loop.quit)
        worker.start()
        loop.exec()
        self.app.processEvents()
        self.assertFalse(worker.isRunning())

    def test_worker_has_distinct_success_and_failure_signals(self):
        succeeded = []
        failed = []
        success_worker = Worker(lambda: None)
        success_worker.succeeded.connect(lambda: succeeded.append(True))
        success_worker.failed.connect(failed.append)
        self._wait(success_worker)
        self.assertEqual(succeeded, [True])
        self.assertEqual(failed, [])

        def fail():
            raise RuntimeError("expected failure")

        failed_worker = Worker(fail)
        failed_worker.succeeded.connect(lambda: succeeded.append(False))
        failed_worker.failed.connect(failed.append)
        self._wait(failed_worker)
        self.assertEqual(failed, ["expected failure"])

    def test_worker_warning_is_delivered_on_the_gui_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            window = PandratorInstaller(
                headless=True,
                working_dir=directory,
                skip_space_warning=True,
            )
            worker = Worker(window.notify_warning, "Warning", "From worker")
            with mock.patch(
                "pandrator_installer.gui.main_window.QMessageBox.warning"
            ) as warning:
                self._wait(worker)
                warning.assert_called_once_with(window, "Warning", "From worker")
            window.worker = None
            window.deleteLater()
            self.app.processEvents()

    def test_close_and_tray_quit_refuse_while_worker_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            window = PandratorInstaller(
                headless=True,
                working_dir=directory,
                skip_space_warning=True,
            )
            active_worker = mock.Mock()
            active_worker.isRunning.return_value = True
            window.worker = active_worker
            close_event = mock.Mock()

            with mock.patch(
                "pandrator_installer.gui.actions.QMessageBox.information"
            ) as information, mock.patch.object(window, "shutdown_apps") as shutdown:
                window.closeEvent(close_event)
                close_event.ignore.assert_called_once()
                shutdown.assert_not_called()
                information.assert_called_once()

            with mock.patch(
                "pandrator_installer.gui.main_window.QMessageBox.information"
            ) as information, mock.patch.object(window, "shutdown_apps") as shutdown:
                window.quit_from_tray()
                shutdown.assert_not_called()
                information.assert_called_once()

            window.worker = None
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
