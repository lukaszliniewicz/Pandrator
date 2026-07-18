import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandrator_installer.supervisor import (
    InstanceAlreadyRunning,
    InstanceLock,
    ManagedProcessSpec,
    ProcessSupervisor,
    load_runtime_manifest,
)


class InstanceLockTests(unittest.TestCase):
    def test_only_one_live_owner_can_hold_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)
            first.acquire()
            with self.assertRaises(InstanceAlreadyRunning):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

    def test_stale_lock_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.lock"
            path.write_text(json.dumps({"pid": 999999999, "instance_id": "stale"}), encoding="utf-8")
            lock = InstanceLock(path)
            lock.acquire()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["instance_id"], lock.instance_id)
            lock.release()


class SupervisorTests(unittest.TestCase):
    def test_managed_processes_do_not_inherit_frozen_installer_libraries(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "pandrator_installer.supervisor.external_subprocess_environment",
            return_value={"PATH": "/usr/bin"},
        ) as sanitized_environment:
            spec = ManagedProcessSpec(
                key="worker",
                label="Worker",
                command=(sys.executable, "-c", "pass"),
                env={"PANDRATOR_BOOTSTRAP_TOKEN": "token"},
            )
            supervisor = ProcessSupervisor(data_root=directory, specs=[spec])

            environment = supervisor._managed_process_environment(spec)

        sanitized_environment.assert_called_once_with()
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["PANDRATOR_BOOTSTRAP_TOKEN"], "token")
        self.assertEqual(environment["PANDRATOR_DATA_DIR"], str(Path(directory).resolve()))
        self.assertEqual(environment["PANDRATOR_SUPERVISOR_INSTANCE"], supervisor.lock.instance_id)

    def test_manifest_requires_argument_arrays_and_unique_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "runtime.json"
            manifest.write_text(
                json.dumps(
                    {
                        "processes": [
                            {"key": "api", "label": "API", "command": [sys.executable, "-c", "print('ok')"]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            specs = load_runtime_manifest(manifest)
            self.assertEqual(specs[0].command[0], sys.executable)

            manifest.write_text(json.dumps({"processes": [{"key": "bad", "command": "shell command"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_runtime_manifest(manifest)

    def test_supervisor_starts_and_stops_process_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started.txt"
            code = (
                "from pathlib import Path; import time; "
                f"Path({str(marker)!r}).write_text('ready'); time.sleep(30)"
            )
            spec = ManagedProcessSpec(
                key="test",
                label="Test child",
                command=(sys.executable, "-c", code),
                startup_timeout_seconds=2,
                restart_limit=0,
            )
            supervisor = ProcessSupervisor(data_root=directory, specs=[spec])
            supervisor.start_all()
            try:
                self.assertTrue(marker.is_file())
                self.assertIsNone(supervisor.processes["test"].process.poll())
                state = json.loads((Path(directory) / "runtime-processes.json").read_text(encoding="utf-8"))
                self.assertIn("test", state["processes"])
            finally:
                supervisor.stop_all()
            self.assertFalse((Path(directory) / "pandrator.instance.lock").exists())

    def test_ready_marker_is_written_after_the_ready_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "import time; time.sleep(30)"
            spec = ManagedProcessSpec(
                key="worker",
                label="Worker",
                command=(sys.executable, "-c", code),
                startup_timeout_seconds=2,
                restart_limit=0,
            )
            callback_states = []

            def observe_callback_state():
                state = json.loads(
                    (Path(directory) / "runtime-processes.json").read_text(encoding="utf-8")
                )
                callback_states.append(state["ready"])

            supervisor = ProcessSupervisor(
                data_root=directory,
                specs=[spec],
                ready_callback=observe_callback_state,
            )
            supervisor.start_all()
            try:
                state = json.loads(
                    (Path(directory) / "runtime-processes.json").read_text(encoding="utf-8")
                )
                self.assertEqual(callback_states, [False])
                self.assertTrue(state["ready"])
                self.assertIsNotNone(state["ready_at"])
            finally:
                supervisor.stop_all()

    def test_control_request_stops_only_the_requested_service_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            specs = [
                ManagedProcessSpec(
                    key=key,
                    label=label,
                    command=(sys.executable, "-c", "import time; time.sleep(30)"),
                    startup_timeout_seconds=2,
                    restart_limit=2,
                )
                for key, label in (("service-kokoro", "Kokoro"), ("service-xtts", "XTTS"))
            ]
            supervisor = ProcessSupervisor(data_root=directory, specs=specs)
            supervisor.start_all()
            try:
                original_pid = supervisor.processes["service-kokoro"].process.pid
                (Path(directory) / "runtime-control.json").write_text(
                    json.dumps({"stop_processes": ["service-kokoro"]}),
                    encoding="utf-8",
                )

                supervisor.monitor_once()

                self.assertNotIn("service-kokoro", supervisor.processes)
                self.assertFalse(any(spec.key == "service-kokoro" for spec in supervisor.specs))
                self.assertIn("service-xtts", supervisor.processes)
                self.assertFalse(Path(directory, "runtime-control.json").exists())
                state = json.loads(
                    Path(directory, "runtime-processes.json").read_text(encoding="utf-8")
                )
                self.assertFalse(
                    any(record["pid"] == original_pid for record in state["processes"].values())
                )
            finally:
                supervisor.stop_all()


if __name__ == "__main__":
    unittest.main()

