"""Installation admission and cleanup across the real headless entry points."""

import contextlib
import io
import queue
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from pandrator_installer.cli import main as legacy_main
from pandrator_installer.lifecycle import main as lifecycle_main
from pandrator_installer.lifecycle_guard import LifecycleBusy, installation_lifecycle_guard
from pandrator_installer.models import InstallSelection
from pandrator_installer.service import HeadlessInstaller
from pandrator_installer.supervisor import InstanceLock
from pandrator_installer.workflows import WorkflowMixin


@contextlib.contextmanager
def runtime_lease(root: Path) -> Iterator[None]:
    ready, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def hold():
        try:
            with installation_lifecycle_guard(root, shared=True):
                ready.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("Fixture lease release timed out.")
        except BaseException as error:
            errors.append(error)
            ready.set()

    owner = threading.Thread(target=hold, name="fixture-runtime-lease")
    owner.start()
    try:
        if not ready.wait(timeout=5) or errors:
            raise RuntimeError(f"Fixture lease failed: {errors}")
        yield
    finally:
        release.set()
        owner.join(timeout=5)
        if owner.is_alive() or errors:
            raise RuntimeError(f"Fixture lease cleanup failed: {errors}")


def native_reader_admitted(root: Path) -> bool:
    result: queue.Queue[bool | BaseException] = queue.Queue()

    def inspect():
        try:
            with installation_lifecycle_guard(root, shared=True):
                result.put(True)
        except LifecycleBusy:
            result.put(False)
        except BaseException as error:
            result.put(error)

    probe = threading.Thread(target=inspect, name="fixture-admission-reader")
    probe.start()
    probe.join(timeout=5)
    if probe.is_alive():
        raise RuntimeError("Fixture admission probe did not finish.")
    outcome = result.get(timeout=5)
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


class InstallerInstallAdmissionTests(unittest.TestCase):
    def invoke(self, workspace: Path, route: str) -> tuple[int, str]:
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                if route == "install-process":
                    HeadlessInstaller(str(workspace)).install_process(
                        InstallSelection(pandrator=False)
                    )
                    code = 0
                elif route == "headless-service":
                    HeadlessInstaller(str(workspace)).run_headless_install(
                        set(), install_pandrator=False
                    )
                    code = 0
                elif route == "lifecycle-cli":
                    code = lifecycle_main(
                        ["install", "--workspace", str(workspace), "--skip-pandrator"]
                    )
                else:
                    code = legacy_main(
                        ["--headless-install", "--workspace", str(workspace), "--skip-pandrator"]
                    )
            except RuntimeError as failure:
                code = 2
                error.write(str(failure))
        return code, output.getvalue() + error.getvalue()

    def test_live_service_lease_refuses_all_entry_points_before_logging_or_mutation(self):
        for route in ("install-process", "headless-service", "lifecycle-cli", "legacy-cli"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                root = workspace / "Pandrator"
                with runtime_lease(root), patch.object(WorkflowMixin, "install_process") as body:
                    code, error = self.invoke(workspace, route)
                    self.assertNotEqual(code, 0, error)
                    self.assertIn("busy", error.lower())
                    body.assert_not_called()
                    self.assertFalse((root / "Logs").exists())

    def test_recorded_native_owner_refuses_direct_install_before_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "Pandrator"
            with InstanceLock(root / "pandrator.instance.lock") as owner:
                before = owner.path.read_bytes()
                with patch.object(WorkflowMixin, "install_process") as body:
                    code, error = self.invoke(workspace, "headless-service")
                self.assertNotEqual(code, 0, error)
                self.assertIn("running", error.lower())
                body.assert_not_called()
                self.assertFalse((root / "Logs").exists())
                self.assertEqual(owner.path.read_bytes(), before)
                self.assertTrue(owner.acquired)

    def test_installed_executable_inventory_and_inspection_failure_refuse_before_mutation(self):
        for inventory in ([{"pid": 123}], RuntimeError("inspection failed")):
            with self.subTest(inventory=inventory), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                root = workspace / "Pandrator"
                with (
                    patch.object(
                        HeadlessInstaller, "get_running_installation_processes"
                    ) as inventory_mock,
                    patch.object(WorkflowMixin, "install_process") as body,
                ):
                    if isinstance(inventory, RuntimeError):
                        inventory_mock.side_effect = inventory
                    else:
                        inventory_mock.return_value = inventory
                    code, _error = self.invoke(workspace, "headless-service")
                self.assertNotEqual(code, 0)
                body.assert_not_called()
                self.assertFalse((root / "Logs").exists())
                self.assertTrue(native_reader_admitted(root))

    def test_exclusive_admission_covers_direct_workflow_and_nested_headless_dispatch(self):
        for route in ("install-process", "headless-service", "lifecycle-cli", "legacy-cli"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                root = workspace / "Pandrator"
                observations = []

                def install(
                    _installer: HeadlessInstaller,
                    _selection: InstallSelection | None,
                    report: list[bool] = observations,
                    installation_root: Path = root,
                ):
                    report.append(native_reader_admitted(installation_root))

                with (
                    patch.object(
                        HeadlessInstaller, "get_running_installation_processes", return_value=[]
                    ),
                    patch.object(
                        WorkflowMixin, "install_process", autospec=True, side_effect=install
                    ),
                ):
                    code, error = self.invoke(workspace, route)
                self.assertEqual(code, 0, error)
                self.assertEqual(observations, [False])
                self.assertTrue(native_reader_admitted(root))

    def test_cli_failure_cleanup_remains_exclusive_then_releases_admission(self):
        for route in ("lifecycle-cli", "legacy-cli"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                root = workspace / "Pandrator"
                observations = []

                def cleanup(
                    _installer: HeadlessInstaller,
                    report: list[bool] = observations,
                    installation_root: Path = root,
                ):
                    report.append(native_reader_admitted(installation_root))

                with (
                    patch.object(
                        HeadlessInstaller, "get_running_installation_processes", return_value=[]
                    ),
                    patch.object(
                        WorkflowMixin, "install_process", side_effect=RuntimeError("install failed")
                    ),
                    patch.object(
                        HeadlessInstaller, "shutdown_apps", autospec=True, side_effect=cleanup
                    ),
                ):
                    code, _error = self.invoke(workspace, route)
                self.assertNotEqual(code, 0)
                self.assertEqual(observations, [False])
                self.assertTrue(native_reader_admitted(root))


if __name__ == "__main__":
    unittest.main()
