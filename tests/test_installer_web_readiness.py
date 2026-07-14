import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from pandrator_installer.models import LaunchSelection
from pandrator_installer.service import HeadlessInstaller


class InstallerWebReadinessTests(unittest.TestCase):
    def test_launcher_rejects_legacy_desktop_only_install(self):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = os.path.join(workspace, "Pandrator")
            repo_root = os.path.join(install_root, "Pandrator")
            os.makedirs(repo_root)
            with open(os.path.join(repo_root, "main.py"), "w", encoding="utf-8") as handle:
                handle.write("# retired desktop entry point")

            installer = HeadlessInstaller(working_dir=workspace)
            with patch.object(installer, "check_pixi", return_value=True), patch.object(
                installer, "get_pixi_executable", return_value="pixi.exe"
            ), patch.object(installer, "load_install_config", return_value={}):
                with self.assertRaisesRegex(FileNotFoundError, "web runtime is incomplete"):
                    installer.launch_process(LaunchSelection(pandrator=True))

    def test_launcher_waits_for_supervisor_ready_marker_and_health(self):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = os.path.join(workspace, "Pandrator")
            os.makedirs(install_root)
            state_path = os.path.join(install_root, "runtime-processes.json")
            process = MagicMock(pid=4321)
            process.poll.return_value = None
            installer = HeadlessInstaller(working_dir=workspace)

            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "supervisor_pid": 4321,
                        "ready": False,
                        "processes": {"api": {}, "worker": {}},
                    },
                    handle,
                )

            def mark_ready():
                time.sleep(0.05)
                with open(state_path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "supervisor_pid": 4321,
                            "ready": True,
                            "processes": {"api": {}, "worker": {}},
                        },
                        handle,
                    )

            writer = threading.Thread(target=mark_ready)
            writer.start()
            response = MagicMock(status_code=200)
            try:
                with patch("pandrator_installer.runtime.requests.get", return_value=response) as get:
                    state = installer.wait_for_web_runtime_ready(
                        process,
                        install_root,
                        timeout_seconds=1,
                        poll_interval=0.01,
                    )
            finally:
                writer.join()

            self.assertTrue(state["ready"])
            get.assert_called_with("http://127.0.0.1:8097/api/v1/health", timeout=1)

    def test_launcher_reports_an_early_supervisor_exit(self):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = os.path.join(workspace, "Pandrator")
            os.makedirs(install_root)
            process = MagicMock(pid=4321)
            process.poll.return_value = 2
            process.log_file_path = ""
            installer = HeadlessInstaller(working_dir=workspace)

            with self.assertRaisesRegex(RuntimeError, "exited before"):
                installer.wait_for_web_runtime_ready(
                    process,
                    install_root,
                    timeout_seconds=1,
                    poll_interval=0.01,
                )


if __name__ == "__main__":
    unittest.main()
