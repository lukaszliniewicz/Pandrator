import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

