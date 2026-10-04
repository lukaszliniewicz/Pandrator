import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from pandrator_installer.runtime_metadata import uninstall_runtime_may_be_active

PID = 2000000001
CHILD_PID = 2000000002
EXECUTABLE = "/fake/pandrator/python"


class _ProcessDouble:
    def __init__(self, create_time: float = 1.0, executable: str = EXECUTABLE) -> None:
        self._create_time = create_time
        self._executable = executable

    def create_time(self) -> float:
        return self._create_time

    def exe(self) -> str:
        return self._executable


def _record(pid: int = PID) -> dict[str, object]:
    return {
        "pid": pid,
        "process_create_time": 1.0,
        "executable": EXECUTABLE,
        "instance_id": "test-instance",
    }


def _state() -> dict[str, object]:
    return {
        "supervisor_pid": PID,
        "supervisor_create_time": 1.0,
        "supervisor_executable": EXECUTABLE,
        "instance_id": "test-instance",
    }


class InstallerUninstallMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_path = self.root / "runtime-processes.json"
        self.lock_path = self.root / "pandrator.instance.lock"
        process_patch = patch(
            "pandrator_installer.runtime_metadata.psutil.Process",
            side_effect=psutil.NoSuchProcess(PID),
        )
        self.process = process_patch.start()
        self.addCleanup(process_patch.stop)
        pid_patch = patch(
            "pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=False
        )
        self.pid_exists = pid_patch.start()
        self.addCleanup(pid_patch.stop)
        for target in (
            "pandrator_installer.runtime_metadata._remove_file",
            "pandrator_installer.runtime_metadata.discard_runtime_metadata",
            "pandrator_installer.runtime_metadata.remove_stale_runtime_metadata",
            "pandrator_installer.runtime_metadata_files.runtime_metadata_guard",
        ):
            cleanup_patch = patch(target, side_effect=AssertionError("Unexpected cleanup"))
            cleanup_patch.start()
            self.addCleanup(cleanup_patch.stop)

    def _write(self, path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _assert_result(self, expected: bool) -> None:
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}

        self.assertEqual(uninstall_runtime_may_be_active(self.root), expected)

        after = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(after, before)

    def test_absent_files_are_inactive(self) -> None:
        self._assert_result(False)

    def test_complete_disappeared_records_are_inactive(self) -> None:
        state = _state()
        state["processes"] = {"child": _record(CHILD_PID)}
        self._write(self.state_path, state)
        self._write(self.lock_path, _record())

        self._assert_result(False)

    def test_complete_reused_pid_records_are_inactive(self) -> None:
        self._write(self.state_path, _state())
        self._write(self.lock_path, _record())
        self.process.side_effect = None
        for process in (_ProcessDouble(2.0), _ProcessDouble(executable="/fake/other/python")):
            with self.subTest(process=process):
                self.process.return_value = process
                self._assert_result(False)

    def test_live_complete_state_alone_may_be_active(self) -> None:
        self._write(self.state_path, _state())
        self.process.side_effect = None
        self.process.return_value = _ProcessDouble()

        self._assert_result(True)

    def test_live_complete_lock_alone_may_be_active(self) -> None:
        self._write(self.lock_path, _record())
        self.process.side_effect = None
        self.process.return_value = _ProcessDouble()

        self._assert_result(True)

    def test_live_child_with_dead_supervisor_may_be_active(self) -> None:
        state = _state()
        state["processes"] = {"dead": _record(), "live": _record(CHILD_PID)}
        self._write(self.state_path, state)

        def process(pid: int) -> _ProcessDouble:
            if pid == CHILD_PID:
                return _ProcessDouble()
            raise psutil.NoSuchProcess(pid)

        self.process.side_effect = process
        self._assert_result(True)

    def test_positive_legacy_pid_uses_existing_pid_presence(self) -> None:
        for path, payload in (
            (self.state_path, {"supervisor_pid": PID}),
            (self.lock_path, {"pid": PID}),
        ):
            with self.subTest(path=path.name):
                self._write(path, payload)
                for present in (False, True):
                    self.pid_exists.return_value = present
                    self._assert_result(present)
                path.unlink()

    def test_malformed_or_nonobject_metadata_is_uncertain(self) -> None:
        for path in (self.state_path, self.lock_path):
            for data in (b"not JSON", b"\xff", b"null", b"[]", b"1", b'"record"'):
                with self.subTest(path=path.name, data=data):
                    path.write_bytes(data)
                    self._assert_result(True)
            path.unlink()

    def test_invalid_or_missing_pid_is_uncertain(self) -> None:
        values: tuple[object, ...] = (
            None,
            0,
            -1,
            "invalid",
            [],
            float("nan"),
            float("inf"),
            float("-inf"),
        )
        for path, key in ((self.state_path, "supervisor_pid"), (self.lock_path, "pid")):
            with self.subTest(path=path.name, missing=True):
                self._write(path, {})
                self._assert_result(True)
            for value in values:
                with self.subTest(path=path.name, value=value):
                    self._write(path, {key: value})
                    self._assert_result(True)
            path.unlink()

    def test_invalid_processes_structure_is_uncertain_with_dead_supervisor(self) -> None:
        for value in (None, [], 1, "processes"):
            with self.subTest(processes=value):
                state = _state()
                state["processes"] = value
                self._write(self.state_path, state)
                self._assert_result(True)

    def test_every_child_value_is_checked_with_dead_supervisor(self) -> None:
        for value in (None, [], 1, {}, {"pid": 0}):
            with self.subTest(child=value):
                state = _state()
                state["processes"] = {"dead": _record(), "uncertain": value}
                self._write(self.state_path, state)
                self._assert_result(True)

    def test_read_failure_with_present_path_is_uncertain(self) -> None:
        self.process.side_effect = AssertionError("Unexpected process inspection")
        self.pid_exists.side_effect = AssertionError("Unexpected PID inspection")
        for path, payload in ((self.state_path, _state()), (self.lock_path, _record())):
            with self.subTest(path=path.name):
                self._write(path, payload)
                data = path.read_bytes()
                with patch(
                    "pandrator_installer.runtime_metadata_files.Path.open",
                    side_effect=PermissionError(),
                ):
                    self.assertTrue(uninstall_runtime_may_be_active(self.root))
                self.assertEqual(path.read_bytes(), data)
                path.unlink()

    def test_no_snapshot_with_truly_absent_files_is_inactive(self) -> None:
        with patch("pandrator_installer.runtime_metadata.read_runtime_metadata", return_value=None):
            self._assert_result(False)

    def test_lstat_denial_is_uncertain(self) -> None:
        with (
            patch("pandrator_installer.runtime_metadata.read_runtime_metadata", return_value=None),
            patch("pandrator_installer.runtime_metadata.Path.lstat", side_effect=PermissionError()),
        ):
            self._assert_result(True)

    def test_complete_access_denied_record_may_be_active(self) -> None:
        for path, payload in ((self.state_path, _state()), (self.lock_path, _record())):
            with self.subTest(path=path.name):
                self._write(path, payload)
                self.process.side_effect = psutil.AccessDenied(PID)
                self._assert_result(True)
                path.unlink()
