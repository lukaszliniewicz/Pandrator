"""Uninstall admission and preservation in disposable installation trees."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psutil

from pandrator_installer.lifecycle import main
from pandrator_installer.lifecycle_guard import LIFECYCLE_GUARD_NAME, installation_lifecycle_guard
from pandrator_installer.process_identity import capture_process_identity, identity_payload
from pandrator_installer.service import HeadlessInstaller


def _populate(workspace: Path) -> Path:
    root = workspace / "Pandrator"
    for relative, data in {
        "Pandrator/Outputs/witness.txt": b"dummy outputs",
        "Pandrator/config.json": b"{}",
        "Pandrator/pandrator_state.sqlite3": b"dummy database bytes",
        "envs/default/witness.txt": b"dummy environment",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _witness(root: Path) -> dict[str, tuple[bytes, int, int, int, int, int]]:
    files = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            files[str(path.relative_to(root))] = (
                path.read_bytes(),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
    return files


class InstallerUninstallTests(unittest.TestCase):
    def invoke(self, workspace: Path, *options: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["uninstall", "--workspace", str(workspace), "--json", *options])
        return code, output.getvalue(), error.getvalue()

    def test_live_or_uncertain_runtime_blocks_both_uninstall_modes_before_mutation(self):
        native_create_time = psutil.Process.create_time
        cases = (
            (options, variant)
            for options in (("--yes",), ("--yes", "--purge-data"))
            for variant in ("complete", "missing-lock", "malformed-state", "inspection-denied")
        )
        for options, variant in cases:
            with (
                self.subTest(options=options, variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                workspace = Path(directory)
                root = _populate(workspace)
                token = uuid.uuid4().hex
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)", token],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    owner = capture_process_identity(psutil.Process(child.pid), instance_id=token)
                    (root / "pandrator.instance.lock").write_text(
                        json.dumps(identity_payload(owner)), encoding="utf-8"
                    )
                    (root / "runtime-processes.json").write_text(
                        json.dumps(
                            {
                                "supervisor_pid": owner.pid,
                                "supervisor_create_time": owner.create_time,
                                "supervisor_executable": owner.executable,
                                "instance_id": token,
                                "processes": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                    if variant == "missing-lock":
                        (root / "pandrator.instance.lock").unlink()
                    elif variant == "malformed-state":
                        (root / "runtime-processes.json").write_bytes(b"{")
                    before = _witness(root)

                    def inspect_time(
                        process: psutil.Process,
                        *,
                        deny: bool = variant == "inspection-denied",
                        owned_pid: int = child.pid,
                    ) -> float:
                        if deny and process.pid == owned_pid:
                            raise psutil.AccessDenied(process.pid)
                        return native_create_time(process)

                    # Exercise durable ownership even when the executable is outside
                    # the installation and no host-wide process inventory is available.
                    with (
                        patch.object(psutil.Process, "create_time", inspect_time),
                        patch("psutil.process_iter", return_value=iter(())),
                    ):
                        code, _output, error = self.invoke(workspace, *options)
                    self.assertEqual(code, 2, error)
                    self.assertIn("before uninstalling", error)
                    self.assertIsNone(child.poll())
                    self.assertTrue(root.is_dir())
                    self.assertFalse((root / "preserved-data").exists())
                    self.assertEqual(_witness(root), before)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                    child.wait(timeout=5)

    def test_inventory_failure_refuses_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            before = _witness(root)
            with patch.object(
                HeadlessInstaller,
                "get_running_installation_processes",
                side_effect=RuntimeError("Could not inspect processes safely."),
            ):
                code, _output, error = self.invoke(workspace, "--yes", "--purge-data")
            self.assertEqual(code, 2)
            self.assertIn("Could not inspect processes safely", error)
            self.assertEqual(_witness(root), before)

    def test_dry_run_and_missing_confirmation_do_not_inspect_or_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            before = _witness(root)
            with (
                patch.object(HeadlessInstaller, "get_running_installation_processes") as inventory,
                patch("pandrator_installer.lifecycle.uninstall_runtime_may_be_active") as metadata,
            ):
                code, output, error = self.invoke(workspace, "--dry-run", "--purge-data")
                self.assertEqual(code, 0, error)
                self.assertTrue(json.loads(output)["dry_run"])
                code, _output, error = self.invoke(workspace)
                self.assertEqual(code, 2)
                self.assertIn("requires --yes", error)
                inventory.assert_not_called()
                metadata.assert_not_called()
            self.assertEqual(_witness(root), before)

    def test_stopped_installation_preserves_data_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            before = _witness(root)
            with patch("psutil.process_iter", return_value=iter(())):
                code, output, error = self.invoke(workspace, "--yes")
            self.assertEqual(code, 0, error)
            self.assertTrue(json.loads(output)["preserve_data"])
            self.assertFalse((root / "Pandrator").exists())
            self.assertFalse((root / "envs").exists())
            preserved = _witness(root / "preserved-data")
            for relative in ("Outputs/witness.txt", "config.json", "pandrator_state.sqlite3"):
                self.assertEqual(preserved[relative][0], before["Pandrator/" + relative][0])

    def test_stopped_installation_purges_only_selected_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            unrelated = workspace / "unrelated.txt"
            unrelated.write_bytes(b"preserve sibling")
            with patch("psutil.process_iter", return_value=iter(())):
                code, output, error = self.invoke(workspace, "--yes", "--purge-data")
            self.assertEqual(code, 0, error)
            self.assertFalse(json.loads(output)["preserve_data"])
            self.assertTrue(root.is_dir())
            self.assertEqual(
                {path.name: path.read_bytes() for path in root.iterdir()},
                {LIFECYCLE_GUARD_NAME: b"", ".runtime-metadata.guard": b""},
            )
            self.assertEqual(unrelated.read_bytes(), b"preserve sibling")

    def test_missing_root_is_a_noop_without_creating_coordination_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch("psutil.process_iter", return_value=iter(())):
                code, _output, error = self.invoke(workspace, "--yes", "--purge-data")
            self.assertEqual(code, 0, error)
            self.assertEqual(list(workspace.iterdir()), [])

    def test_shared_service_lease_refuses_uninstall_without_deleting_original_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            with installation_lifecycle_guard(root, shared=True):
                before = _witness(root)
                # Another thread/process would conflict natively; this same-thread
                # mixed-mode request must also refuse without upgrading its lease.
                with patch("psutil.process_iter", return_value=iter(())):
                    code, _output, error = self.invoke(workspace, "--yes", "--purge-data")
                self.assertEqual(code, 2, error)
                self.assertIn("busy", error)
                self.assertEqual(_witness(root), before)

    def test_rechecks_recorded_runtime_after_exclusive_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            before = _witness(root)
            with (
                patch("psutil.process_iter", return_value=iter(())),
                patch(
                    "pandrator_installer.lifecycle.uninstall_runtime_may_be_active",
                    side_effect=[False, True],
                ) as metadata,
            ):
                code, _output, error = self.invoke(workspace, "--yes", "--purge-data")
            self.assertEqual(code, 2, error)
            self.assertEqual(metadata.call_count, 2)
            # Admission may create empty guards, but all original data is exact.
            after = _witness(root)
            self.assertEqual({name: after[name] for name in before}, before)
            self.assertFalse((root / "preserved-data").exists())

    def test_native_supervisor_cannot_start_at_the_first_deletion_cut(self):
        child_code = textwrap.dedent("""
            import json, os, sys
            from pandrator_installer.lifecycle_guard import LifecycleBusy
            from pandrator_installer.supervisor import ProcessSupervisor
            supervisor = ProcessSupervisor(data_root=sys.argv[1], specs=[])
            try:
                supervisor.start_all()
            except LifecycleBusy:
                print(json.dumps({"token": sys.argv[2], "refused": True,
                                  "acquired": supervisor.lock.acquired}))
            else:
                supervisor.stop_all()
                print(json.dumps({"token": sys.argv[2], "refused": False}))
        """)
        import shutil

        native_rmtree = shutil.rmtree
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = _populate(workspace)
            token = uuid.uuid4().hex
            observed = []

            def delete_at_cut(path, *args, **kwargs):
                if not observed:
                    result = subprocess.run(
                        [sys.executable, "-c", child_code, str(root), token],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=True,
                    )
                    observed.append(json.loads(result.stdout))
                    self.assertFalse((root / "pandrator.instance.lock").exists())
                    self.assertFalse((root / "runtime-processes.json").exists())
                return native_rmtree(path, *args, **kwargs)

            with (
                patch("psutil.process_iter", return_value=iter(())),
                patch("pandrator_installer.lifecycle.shutil.rmtree", side_effect=delete_at_cut),
            ):
                code, _output, error = self.invoke(workspace, "--yes", "--purge-data")
            self.assertEqual(code, 0, error)
            self.assertEqual(observed, [{"token": token, "refused": True, "acquired": False}])
            self.assertEqual(
                {path.name: path.read_bytes() for path in root.iterdir()},
                {LIFECYCLE_GUARD_NAME: b"", ".runtime-metadata.guard": b""},
            )


if __name__ == "__main__":
    unittest.main()
