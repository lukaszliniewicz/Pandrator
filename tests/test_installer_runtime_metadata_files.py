import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator_installer.runtime_metadata_files import (
    RuntimeMetadataSnapshot,
    discard_runtime_metadata,
    read_runtime_metadata,
    runtime_metadata_guard,
    runtime_metadata_matches,
)


class InstallerRuntimeMetadataFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "runtime-processes.json"
        self.data = b'{"supervisor_pid": 2000000001}'
        self.path.write_bytes(self.data)

    def _snapshot(self) -> RuntimeMetadataSnapshot:
        snapshot = read_runtime_metadata(self.path)
        if snapshot is None:
            self.fail("Expected a stable readable snapshot")
        return snapshot

    def test_unchanged_snapshot_is_discarded(self) -> None:
        snapshot = self._snapshot()

        self.assertTrue(runtime_metadata_matches(snapshot))
        self.assertTrue(discard_runtime_metadata(snapshot))
        self.assertFalse(self.path.exists())
        self.assertTrue((self.root / ".runtime-metadata.guard").exists())

    def test_replaced_snapshot_is_retained(self) -> None:
        snapshot = self._snapshot()
        replacement = self.root / "replacement.json"
        data = b'{"supervisor_pid": 2000000002}'
        replacement.write_bytes(data)
        os.replace(replacement, self.path)

        self.assertFalse(discard_runtime_metadata(snapshot))
        self.assertEqual(self.path.read_bytes(), data)

    def test_identical_bytes_with_replaced_inode_are_retained(self) -> None:
        snapshot = self._snapshot()
        replacement = self.root / "replacement.json"
        replacement.write_bytes(self.data)
        os.replace(replacement, self.path)

        self.assertFalse(discard_runtime_metadata(snapshot))
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_missing_snapshot_file_is_not_discarded(self) -> None:
        snapshot = self._snapshot()
        self.path.unlink()

        self.assertIsNone(read_runtime_metadata(self.path))
        self.assertFalse(discard_runtime_metadata(snapshot))
        self.assertFalse(self.path.exists())

    def test_malformed_bytes_are_captured_with_none_payload(self) -> None:
        for data in (b"not JSON", b"\xff"):
            with self.subTest(data=data):
                self.path.write_bytes(data)

                snapshot = self._snapshot()

                self.assertIsNone(snapshot.payload)
                self.assertIsInstance(snapshot.parse_error, ValueError)
                self.assertEqual(snapshot.data, data)
                self.assertEqual(self.path.read_bytes(), data)
                self.assertTrue(discard_runtime_metadata(snapshot))
                self.assertFalse(self.path.exists())

    def test_valid_json_null_has_no_parse_error(self) -> None:
        self.path.write_bytes(b"null")

        snapshot = self._snapshot()

        self.assertIsNone(snapshot.payload)
        self.assertIsNone(snapshot.parse_error)
        self.assertEqual(snapshot.data, b"null")

    def test_changed_read_stat_returns_no_snapshot(self) -> None:
        before = self.path.stat()
        self.path.write_bytes(self.data + b" ")
        after = self.path.stat()

        with patch(
            "pandrator_installer.runtime_metadata_files.os.fstat", side_effect=[before, after]
        ):
            self.assertIsNone(read_runtime_metadata(self.path))

        self.assertEqual(self.path.read_bytes(), self.data + b" ")

    def test_guard_releases_after_body_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "test body failed"):
            with runtime_metadata_guard(self.root):
                raise RuntimeError("test body failed")

        with runtime_metadata_guard(self.root, timeout=0):
            self.assertEqual(self.path.read_bytes(), self.data)

    def test_guard_rejects_invalid_timeouts_without_mutating_files(self) -> None:
        for timeout in (-1.0, float("inf"), float("-inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    with runtime_metadata_guard(self.root, timeout=timeout):
                        self.fail("Invalid timeout was accepted")
                self.assertEqual(self.path.read_bytes(), self.data)
                self.assertFalse((self.root / ".runtime-metadata.guard").exists())

    def test_thread_contention_timeout_preserves_file_then_reacquires(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def hold_guard() -> None:
            try:
                with runtime_metadata_guard(self.root):
                    entered.set()
                    if not release.wait(2.0):
                        raise TimeoutError("Test did not release the holder")
            except BaseException as error:
                failures.append(error)

        holder = threading.Thread(target=hold_guard, name="test-runtime-metadata-holder")
        holder.start()
        try:
            self.assertTrue(entered.wait(1.0), "Guard holder did not start")
            with self.assertRaises(TimeoutError):
                with runtime_metadata_guard(self.root, timeout=0.01):
                    self.fail("Contended guard was acquired")
            self.assertEqual(self.path.read_bytes(), self.data)
        finally:
            release.set()
            holder.join(timeout=1.0)

        self.assertFalse(holder.is_alive())
        self.assertEqual(failures, [])
        with runtime_metadata_guard(self.root, timeout=0):
            self.assertEqual(self.path.read_bytes(), self.data)
