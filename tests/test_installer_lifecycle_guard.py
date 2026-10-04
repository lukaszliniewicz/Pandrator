"""Native admission leases and startup cleanup in disposable roots."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator_installer.lifecycle_guard import (
    LIFECYCLE_GUARD_NAME,
    LifecycleBusy,
    installation_lifecycle_guard,
)
from pandrator_installer.supervisor import InstanceLock, ProcessSupervisor


class InstallerLifecycleGuardTests(unittest.TestCase):
    def probe(self, root: Path, *, shared: bool) -> dict[str, bool]:
        code = textwrap.dedent("""
            import json, sys
            from pandrator_installer.lifecycle_guard import (
                installation_lifecycle_guard, LifecycleBusy,
            )
            try:
                with installation_lifecycle_guard(sys.argv[1], shared=sys.argv[2] == '1'):
                    print(json.dumps({'admitted': True}))
            except LifecycleBusy:
                print(json.dumps({'admitted': False}))
        """)
        result = subprocess.run(
            [sys.executable, "-c", code, str(root), str(int(shared))],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return json.loads(result.stdout)

    def test_native_process_readers_coexist_and_exclusive_admission_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with installation_lifecycle_guard(root, shared=True):
                self.assertEqual(self.probe(root, shared=True), {"admitted": True})
                self.assertEqual(self.probe(root, shared=False), {"admitted": False})
            with installation_lifecycle_guard(root, shared=False):
                self.assertEqual(self.probe(root, shared=True), {"admitted": False})
                self.assertEqual(self.probe(root, shared=False), {"admitted": False})
            self.assertEqual(self.probe(root, shared=False), {"admitted": True})
            self.assertEqual((root / LIFECYCLE_GUARD_NAME).read_bytes(), b"")

    def test_native_thread_contention_and_independent_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "one"
            sibling = Path(directory) / "two"
            errors = []
            outcomes = []

            def inspect():
                try:
                    with self.assertRaises(LifecycleBusy):
                        with installation_lifecycle_guard(root, shared=False):
                            self.fail("Exclusive admission overlapped a reader.")
                    with installation_lifecycle_guard(root, shared=True):
                        outcomes.append("reader")
                    with installation_lifecycle_guard(sibling, shared=False):
                        outcomes.append("sibling")
                except BaseException as error:
                    errors.append(error)

            with installation_lifecycle_guard(root, shared=True):
                thread = threading.Thread(target=inspect)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(outcomes, ["reader", "sibling"])

    def test_nested_admission_preserves_outer_lease_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for shared in (False, True):
                with self.subTest(shared=shared):
                    with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                        with installation_lifecycle_guard(root, shared=shared):
                            with installation_lifecycle_guard(root / ".", shared=shared):
                                pass
                            self.assertEqual(self.probe(root, shared=False), {"admitted": False})
                            with self.assertRaises(LifecycleBusy):
                                with installation_lifecycle_guard(root, shared=not shared):
                                    self.fail("Mixed-mode nesting was admitted.")
                            raise RuntimeError("fixture failure")
                    self.assertEqual(self.probe(root, shared=False), {"admitted": True})

    @unittest.skipIf(os.name == "nt", "POSIX parent-directory permission fixture")
    def test_existing_root_does_not_require_writable_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "existing"
            root.mkdir()
            parent.chmod(0o555)
            try:
                with installation_lifecycle_guard(root, shared=False):
                    self.assertEqual((root / LIFECYCLE_GUARD_NAME).read_bytes(), b"")
            finally:
                parent.chmod(0o700)

    def test_native_supervisor_preparation_runs_only_after_instance_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = ProcessSupervisor(data_root=root, specs=[])
            observations = []

            def prepare():
                self.assertTrue(supervisor.lock.acquired)
                self.assertTrue((root / "pandrator.instance.lock").is_file())
                self.assertFalse((root / "runtime-processes.json").exists())
                observations.append(self.probe(root, shared=False))

            supervisor.start_all(prepare=prepare)
            try:
                self.assertTrue(supervisor.ready)
                self.assertEqual(observations, [{"admitted": True}])
                # The short admission lease is released; durable native ownership
                # remains independently visible to uninstall's recheck.
            finally:
                supervisor.stop_all()
            self.assertFalse((root / "pandrator.instance.lock").exists())

    def test_owned_preparation_baseexceptions_clean_up_and_restore_signal_handlers(self):
        for exception_type in (KeyboardInterrupt, SystemExit, RuntimeError):
            with (
                self.subTest(exception_type=exception_type),
                tempfile.TemporaryDirectory() as directory,
            ):
                supervisor = ProcessSupervisor(data_root=directory, specs=[])
                previous = {
                    signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
                }

                def prepare(
                    owned: ProcessSupervisor = supervisor,
                    failure: type[BaseException] = exception_type,
                ):
                    self.assertTrue(owned.lock.acquired)
                    raise failure("fixture failure")

                with self.assertRaises(exception_type):
                    supervisor.run_foreground(prepare=prepare)
                self.assertFalse(supervisor.lock.acquired)
                self.assertFalse(supervisor.lock.path.exists())
                self.assertFalse(supervisor.runtime_state.exists())
                self.assertEqual(supervisor.processes, {})
                self.assertEqual(
                    {signum: signal.getsignal(signum) for signum in previous}, previous
                )

    def test_failed_acquisition_keeps_foreign_control_and_restores_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = InstanceLock(root / "pandrator.instance.lock")
            first.acquire()
            try:
                control = root / "runtime-control.json"
                control.write_bytes(b'{"foreign":true}')
                before = control.read_bytes()
                previous = {
                    signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
                }
                supervisor = ProcessSupervisor(data_root=root, specs=[])
                with (
                    patch.object(supervisor, "stop_all") as stop,
                    patch.object(supervisor, "ready_callback") as ready,
                ):
                    with self.assertRaisesRegex(RuntimeError, "already supervised"):
                        supervisor.run_foreground()
                    stop.assert_not_called()
                    ready.assert_not_called()
                self.assertEqual(control.read_bytes(), before)
                self.assertEqual(
                    {signum: signal.getsignal(signum) for signum in previous}, previous
                )
            finally:
                first.release()


if __name__ == "__main__":
    unittest.main()
