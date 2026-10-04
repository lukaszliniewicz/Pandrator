import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from pandrator_installer.runtime_metadata import remove_stale_runtime_metadata

SUPERVISOR_PID = 2000000001
CHILD_PID = 2000000002
LOCK_PID = 2000000001
EXECUTABLE = "/fake/pandrator/python"


class _ProcessDouble:
    def __init__(self, create_time: float) -> None:
        self._create_time = create_time

    def create_time(self) -> float:
        return self._create_time

    def exe(self) -> str:
        return EXECUTABLE


def _record(pid: int, create_time: float = 1.0) -> dict[str, object]:
    return {
        "pid": pid,
        "process_create_time": create_time,
        "executable": EXECUTABLE,
        "instance_id": "test-instance",
    }


def _runtime_state(create_time: float = 1.0) -> dict[str, object]:
    return {
        "supervisor_pid": SUPERVISOR_PID,
        "supervisor_create_time": create_time,
        "supervisor_executable": EXECUTABLE,
        "instance_id": "test-instance",
        "processes": {},
    }


class InstallerRuntimeMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runtime_state = self.root / "runtime-processes.json"
        self.lock = self.root / "pandrator.instance.lock"

    def _write(self, path: Path, payload: object) -> bytes:
        contents = json.dumps(payload).encode("utf-8")
        path.write_bytes(contents)
        return contents

    def test_complete_stale_metadata_is_removed(self) -> None:
        self._write(self.runtime_state, _runtime_state())
        self._write(self.lock, _record(LOCK_PID))

        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                return_value=_ProcessDouble(2.0),
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=False),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertFalse(self.runtime_state.exists())
        self.assertFalse(self.lock.exists())

    def test_incomplete_live_metadata_is_preserved(self) -> None:
        runtime_bytes = self._write(self.runtime_state, {"supervisor_pid": SUPERVISOR_PID})
        lock_bytes = self._write(self.lock, {"pid": LOCK_PID})

        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=AssertionError("Unexpected process inspection"),
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=True),
        ):
            remove_stale_runtime_metadata(str(self.root))

        self.assertEqual(self.runtime_state.read_bytes(), runtime_bytes)
        self.assertEqual(self.lock.read_bytes(), lock_bytes)

    def test_live_child_preserves_state_but_stale_lock_is_removed(self) -> None:
        payload = _runtime_state()
        payload["processes"] = {"child": _record(CHILD_PID, 2.0)}
        runtime_bytes = self._write(self.runtime_state, payload)
        self._write(self.lock, _record(LOCK_PID))

        def process_double(pid: int) -> _ProcessDouble:
            if pid not in {SUPERVISOR_PID, CHILD_PID, LOCK_PID}:
                raise AssertionError(f"Unexpected fake PID: {pid}")
            return _ProcessDouble(2.0)

        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=process_double,
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=False),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertEqual(self.runtime_state.read_bytes(), runtime_bytes)
        self.assertFalse(self.lock.exists())

    def test_inspection_denied_preserves_complete_metadata(self) -> None:
        runtime_bytes = self._write(self.runtime_state, _runtime_state())
        lock_bytes = self._write(self.lock, _record(LOCK_PID))

        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=psutil.AccessDenied(pid=SUPERVISOR_PID),
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=False),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertEqual(self.runtime_state.read_bytes(), runtime_bytes)
        self.assertEqual(self.lock.read_bytes(), lock_bytes)

    def test_nonfinite_identity_with_live_pid_is_preserved(self) -> None:
        runtime_bytes = self._write(self.runtime_state, _runtime_state(float("nan")))
        lock_bytes = self._write(self.lock, _record(LOCK_PID, float("nan")))

        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=AssertionError("Unexpected process inspection"),
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=True),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertEqual(self.runtime_state.read_bytes(), runtime_bytes)
        self.assertEqual(self.lock.read_bytes(), lock_bytes)

    def test_missing_metadata_is_a_no_op(self) -> None:
        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=AssertionError("Unexpected process inspection"),
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=False),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertFalse(self.runtime_state.exists())
        self.assertFalse(self.lock.exists())

    def test_infinite_metadata_pids_do_not_abort_cleanup(self) -> None:
        for value in (float("inf"), float("-inf")):
            with self.subTest(pid=value):
                self._write(self.runtime_state, {"supervisor_pid": value})
                self._write(self.lock, {"pid": value})
                with (
                    patch(
                        "pandrator_installer.runtime_metadata.psutil.Process",
                        side_effect=AssertionError("Unexpected process inspection"),
                    ),
                    patch(
                        "pandrator_installer.runtime_metadata.psutil.pid_exists",
                        side_effect=AssertionError("Unexpected PID inspection"),
                    ),
                ):
                    remove_stale_runtime_metadata(self.root)
                self.assertFalse(self.runtime_state.exists())
                self.assertFalse(self.lock.exists())

    def _assert_replacement_retained(
        self, path: Path, original: object, replacement: object
    ) -> None:
        self._write(path, original)
        replacement_path = self.root / "replacement.json"
        replacement_bytes = self._write(replacement_path, replacement)

        def replace_before_stale_result(pid: int) -> None:
            if pid != SUPERVISOR_PID:
                raise AssertionError(f"Unexpected fake PID: {pid}")
            os.replace(replacement_path, path)
            raise psutil.NoSuchProcess(pid)

        with (
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=replace_before_stale_result,
            ),
            patch("pandrator_installer.runtime_metadata.psutil.pid_exists", return_value=False),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertEqual(path.read_bytes(), replacement_bytes)

    def test_runtime_state_replacement_during_inspection_is_retained(self) -> None:
        replacement = _runtime_state(2.0)
        replacement["supervisor_pid"] = CHILD_PID
        self._assert_replacement_retained(self.runtime_state, _runtime_state(), replacement)

    def test_lock_replacement_during_inspection_is_retained(self) -> None:
        self._assert_replacement_retained(self.lock, _record(LOCK_PID), _record(CHILD_PID, 2.0))

    def test_unreadable_metadata_is_retained_without_process_inspection(self) -> None:
        runtime_bytes = self._write(self.runtime_state, _runtime_state())
        lock_bytes = self._write(self.lock, _record(LOCK_PID))

        with (
            patch(
                "pandrator_installer.runtime_metadata_files.Path.open",
                side_effect=PermissionError("Metadata is unreadable"),
            ),
            patch(
                "pandrator_installer.runtime_metadata.psutil.Process",
                side_effect=AssertionError("Unexpected process inspection"),
            ),
            patch(
                "pandrator_installer.runtime_metadata.psutil.pid_exists",
                side_effect=AssertionError("Unexpected PID inspection"),
            ),
        ):
            remove_stale_runtime_metadata(self.root)

        self.assertEqual(self.runtime_state.read_bytes(), runtime_bytes)
        self.assertEqual(self.lock.read_bytes(), lock_bytes)
