"""Update intent ownership and native admission in disposable installations."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import IO
from unittest.mock import Mock, patch

from pandrator_installer.lifecycle import main
from pandrator_installer.models import WorkspacePaths
from pandrator_installer.runtime_metadata_files import runtime_metadata_guard
from pandrator_installer.update_operation import UpdateOperation
from tests.test_installer_install_admission import native_reader_admitted, runtime_lease
from tests.test_installer_update_environment import signed_release


def exclusive_admitted(root: Path) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, sys
from pandrator_installer.lifecycle_guard import installation_lifecycle_guard, LifecycleBusy
try:
    with installation_lifecycle_guard(sys.argv[1], shared=False):
        print(json.dumps(True))
except LifecycleBusy:
    print(json.dumps(False))
""",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return json.loads(result.stdout)


class InstallerUpdateAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.paths = WorkspacePaths.from_value(self.scratch.name)
        self.root = self.paths.install_root
        self.root.mkdir(parents=True)
        wheel = self.paths.workspace / "pandrator-0.49.0-py3-none-any.whl"
        wheel.write_bytes(b"signed admission fixture; package activation is stubbed")
        manifest, public = signed_release(self.paths.workspace, wheel)
        self.arguments = [
            "update",
            "--workspace",
            str(self.paths.workspace),
            "--wheel",
            str(wheel),
            "--manifest",
            str(manifest),
            "--public-key",
            str(public),
        ]
        self.marker = self.root / "maintenance.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in (
            "validate_update_environment",
            "validate_update_package",
            "snapshot_sqlite",
            "run_migrations",
            "health_check",
            "restore_database",
            "restore_installed_package",
        ):
            self.stack.enter_context(patch(f"pandrator_installer.lifecycle.{name}"))
        self.snapshot = self.stack.enter_context(
            patch(
                "pandrator_installer.lifecycle.snapshot_installed_package", return_value=self.root
            )
        )
        self.install = self.stack.enter_context(
            patch("pandrator_installer.lifecycle.install_wheel")
        )
        self.restart = Mock()
        subprocess_proxy = Mock(wraps=subprocess)
        subprocess_proxy.Popen = self.restart
        subprocess_proxy.DEVNULL = subprocess.DEVNULL
        subprocess_proxy.CREATE_NEW_PROCESS_GROUP = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        subprocess_proxy.CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.stack.enter_context(
            patch("pandrator_installer.lifecycle.subprocess", subprocess_proxy)
        )
        # Only the installer executable inventory is stubbed. Runtime metadata
        # and native lifecycle/metadata locks remain real.
        self.stack.enter_context(
            patch(
                "pandrator_installer.lifecycle.HeadlessInstaller.get_running_installation_processes",
                return_value=[],
            )
        )

    def invoke(self) -> tuple[int, str]:
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(self.arguments)
        return code, output.getvalue() + error.getvalue()

    def stopped_supervisor(self) -> Mock:
        (self.root / "runtime-processes.json").write_text("{}")
        supervisor = Mock()
        supervisor.cmdline.return_value = ["fixture-launcher"]
        supervisor.cwd.return_value = str(self.paths.workspace)
        supervisor.is_running.return_value = False
        self.stack.enter_context(
            patch(
                "pandrator_installer.lifecycle._validated_supervisor_process",
                return_value=supervisor,
            )
        )
        return supervisor

    def test_runtime_lease_refuses_activation_before_backups(self) -> None:
        with runtime_lease(self.root):
            code, message = self.invoke()
            self.assertEqual(code, 2, message)
            self.assertIn("busy", message.lower())
            self.snapshot.assert_not_called()
            self.install.assert_not_called()
            self.restart.assert_not_called()
            self.assertFalse((self.root / "backups").exists())
            self.assertFalse(self.marker.exists())
        self.assertTrue(exclusive_admitted(self.root))

    def test_preexisting_marker_is_preserved_before_stop(self) -> None:
        supervisor = self.stopped_supervisor()
        original = b'{"reason":"update","operation_id":"other"}'
        self.marker.write_bytes(original)
        code, message = self.invoke()
        self.assertEqual(code, 2, message)
        self.assertIn("already exists", message)
        self.assertEqual(self.marker.read_bytes(), original)
        supervisor.terminate.assert_not_called()
        self.snapshot.assert_not_called()
        self.assertTrue(exclusive_admitted(self.root))

    def test_second_update_cannot_replace_or_clear_first_intent(self) -> None:
        observations: list[tuple[int, str]] = []

        def during_stop() -> None:
            before = self.marker.read_bytes()
            self.assertFalse(exclusive_admitted(self.root))
            observations.append(self.invoke())
            self.assertEqual(self.marker.read_bytes(), before)

        supervisor = self.stopped_supervisor()
        supervisor.wait.side_effect = lambda **_kwargs: during_stop()
        code, message = self.invoke()
        self.assertEqual(code, 0, message)
        self.assertEqual(observations[0][0], 2)
        self.assertIn("already exists", observations[0][1])
        supervisor.terminate.assert_called_once()
        self.install.assert_called_once()
        self.assertFalse(self.marker.exists())

    def test_stop_shared_owner_then_exclusive_activation_and_unlocked_restart(self) -> None:
        with contextlib.ExitStack() as old_runtime:
            old_runtime.enter_context(runtime_lease(self.root))
            supervisor = self.stopped_supervisor()

            def stop(**_kwargs: object) -> None:
                self.assertTrue(native_reader_admitted(self.root))
                self.assertFalse(exclusive_admitted(self.root))
                old_runtime.close()

            supervisor.wait.side_effect = stop
            phases: list[str] = []

            def check_phase(name: str) -> None:
                self.assertFalse(native_reader_admitted(self.root), name)
                self.assertTrue(self.marker.exists(), name)
                phases.append(name)

            self.snapshot.side_effect = lambda *_args: (check_phase("snapshot"), self.root)[1]
            self.install.side_effect = lambda *_args: check_phase("install")
            with (
                patch(
                    "pandrator_installer.lifecycle.run_migrations",
                    side_effect=lambda *_args: check_phase("migrate"),
                ),
                patch(
                    "pandrator_installer.lifecycle.health_check",
                    side_effect=lambda *_args: check_phase("health"),
                ),
            ):
                self.restart.side_effect = lambda *_args, **_kwargs: self.assertTrue(
                    native_reader_admitted(self.root)
                )
                code, message = self.invoke()
            self.assertEqual(code, 0, message)
            self.assertEqual(phases, ["snapshot", "install", "migrate", "health"])
            self.restart.assert_called_once()
            self.assertFalse(self.marker.exists())
            self.assertTrue(exclusive_admitted(self.root))

    def test_rollback_retains_exclusive_admission_and_original_error(self) -> None:
        self.install.side_effect = RuntimeError("activation sentinel")
        phases: list[str] = []

        def check(name: str) -> None:
            self.assertFalse(native_reader_admitted(self.root))
            phases.append(name)

        with (
            patch(
                "pandrator_installer.lifecycle.restore_installed_package",
                side_effect=lambda *_args: check("package"),
            ),
            patch(
                "pandrator_installer.lifecycle.restore_database",
                side_effect=lambda *_args: check("database"),
            ),
            patch(
                "pandrator_installer.lifecycle.health_check",
                side_effect=lambda *_args: check("health"),
            ),
        ):
            code, message = self.invoke()
        self.assertEqual(code, 2, message)
        self.assertIn("activation sentinel", message)
        self.assertEqual(phases, ["package", "database", "health"])
        self.assertFalse(self.marker.exists())
        self.assertTrue(exclusive_admitted(self.root))

    def test_late_runtime_lease_wins_after_stop_without_duplicate_restart(self) -> None:
        with contextlib.ExitStack() as late_runtime:
            supervisor = self.stopped_supervisor()
            supervisor.wait.side_effect = lambda **_kwargs: late_runtime.enter_context(
                runtime_lease(self.root)
            )
            code, message = self.invoke()
            self.assertEqual(code, 2, message)
            self.assertIn("busy", message.lower())
            self.snapshot.assert_not_called()
            self.restart.assert_not_called()
            self.assertFalse(self.marker.exists())
        self.assertTrue(exclusive_admitted(self.root))

    def test_replacement_maintenance_survives_activation_refusal(self) -> None:
        supervisor = self.stopped_supervisor()
        replacement = b'{"reason":"replacement","operation_id":"new-owner"}'

        def replace(**_kwargs: object) -> None:
            temporary = self.marker.with_suffix(".replacement")
            temporary.write_bytes(replacement)
            os.replace(temporary, self.marker)

        supervisor.wait.side_effect = replace
        code, message = self.invoke()
        self.assertEqual(code, 2, message)
        self.assertIn("ownership changed", message)
        self.assertEqual(self.marker.read_bytes(), replacement)
        self.snapshot.assert_not_called()
        self.restart.assert_not_called()
        self.assertTrue(exclusive_admitted(self.root))

    def test_independent_live_runtime_metadata_refuses_before_backup(self) -> None:
        supervisor = self.stopped_supervisor()
        state = self.root / "runtime-processes.json"
        supervisor.wait.side_effect = lambda **_kwargs: state.write_text(
            json.dumps({"instance_id": "new", "supervisor_pid": os.getpid()})
        )
        code, message = self.invoke()
        self.assertEqual(code, 2, message)
        self.assertIn("runtime metadata", message)
        self.assertEqual(json.loads(state.read_text())["instance_id"], "new")
        self.snapshot.assert_not_called()
        self.restart.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_failed_marker_write_removes_only_its_own_inode_and_releases_admission(self) -> None:
        original_open = Path.open
        replacement = b'{"operation_id":"replacement"}'

        class FailedWriter:
            def __init__(self, handle: IO[bytes], marker: Path, replace: bool) -> None:
                self.handle = handle
                self.marker = marker
                self.replace = replace

            def __enter__(self) -> "FailedWriter":
                return self

            def __exit__(self, *_args: object) -> None:
                self.handle.close()

            def fileno(self) -> int:
                return self.handle.fileno()

            def write(self, data: bytes) -> int:
                self.handle.write(data[:4])
                self.handle.flush()
                if self.replace:
                    temporary = self.marker.with_suffix(".replacement")
                    temporary.write_bytes(replacement)
                    os.replace(temporary, self.marker)
                raise KeyboardInterrupt("write sentinel")

            def flush(self) -> None:
                self.handle.flush()

        for replace in (False, True):
            with self.subTest(replace=replace):
                self.marker.unlink(missing_ok=True)

                def open_marker(path: Path, mode: str = "r", *args, _replace=replace, **kwargs):
                    handle = original_open(path, mode, *args, **kwargs)
                    if path == self.marker and mode == "xb":
                        return FailedWriter(handle, self.marker, _replace)
                    return handle

                with patch.object(Path, "open", open_marker):
                    with self.assertRaisesRegex(KeyboardInterrupt, "write sentinel"):
                        with UpdateOperation(self.root, "fixture"):
                            self.fail("A failed maintenance write was admitted.")
                if replace:
                    self.assertEqual(self.marker.read_bytes(), replacement)
                else:
                    self.assertFalse(self.marker.exists())
                self.assertTrue(exclusive_admitted(self.root))

    def test_cleanup_failure_preserves_active_error_and_releases_exclusive_admission(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "activation sentinel"):
            with patch(
                "pandrator_installer.update_operation.discard_runtime_metadata",
                side_effect=OSError("cleanup sentinel"),
            ):
                with UpdateOperation(self.root, "fixture") as operation:
                    operation.activate()
                    raise RuntimeError("activation sentinel")
        self.assertTrue(self.marker.exists())
        self.assertTrue(exclusive_admitted(self.root))

    def test_guard_exit_failure_cleans_owned_marker_and_preserves_replacement(self) -> None:
        replacement = b'{"operation_id":"replacement"}'
        for replace in (False, True):
            with self.subTest(replace=replace):
                self.marker.unlink(missing_ok=True)
                calls = 0

                @contextlib.contextmanager
                def fail_first_exit(root: Path, _replace=replace, **kwargs):
                    nonlocal calls
                    calls += 1
                    first = calls == 1
                    with runtime_metadata_guard(root, **kwargs):
                        yield
                    if first:
                        if _replace:
                            temporary = self.marker.with_suffix(".replacement")
                            temporary.write_bytes(replacement)
                            os.replace(temporary, self.marker)
                        raise OSError("guard exit sentinel")

                with patch(
                    "pandrator_installer.update_operation.runtime_metadata_guard", fail_first_exit
                ):
                    with self.assertRaisesRegex(OSError, "guard exit sentinel"):
                        with UpdateOperation(self.root, "fixture"):
                            self.fail("A failing entry guard was admitted.")
                if replace:
                    self.assertEqual(self.marker.read_bytes(), replacement)
                else:
                    self.assertFalse(self.marker.exists())
                self.assertTrue(exclusive_admitted(self.root))

    def test_cli_cleanup_baseexceptions_preserve_activation_error(self) -> None:
        self.install.side_effect = RuntimeError("activation sentinel")
        for exception_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception_type=exception_type):
                self.marker.unlink(missing_ok=True)
                with patch(
                    "pandrator_installer.update_operation.discard_runtime_metadata",
                    side_effect=exception_type("cleanup sentinel"),
                ):
                    try:
                        code, message = self.invoke()
                    except BaseException as error:
                        self.fail(
                            f"Cleanup replaced activation failure: {type(error).__name__}: {error}"
                        )
                self.assertEqual(code, 2, message)
                self.assertIn("activation sentinel", message)
                self.assertTrue(self.marker.exists())
                self.assertTrue(exclusive_admitted(self.root))


if __name__ == "__main__":
    unittest.main()
