"""Uninstall admission and preservation in disposable installation trees."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psutil

from pandrator_installer.lifecycle import main
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

    def test_live_external_supervisor_blocks_both_uninstall_modes_before_mutation(self):
        for options in (("--yes",), ("--yes", "--purge-data")):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
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
                    before = _witness(root)
                    # Exercise durable ownership even when the executable is outside
                    # the installation and no host-wide process inventory is available.
                    with patch("psutil.process_iter", return_value=iter(())):
                        code, _output, error = self.invoke(workspace, *options)
                    self.assertEqual(code, 2, error)
                    self.assertIn("still running", error)
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
            with patch.object(HeadlessInstaller, "get_running_installation_processes") as inventory:
                code, output, error = self.invoke(workspace, "--dry-run", "--purge-data")
                self.assertEqual(code, 0, error)
                self.assertTrue(json.loads(output)["dry_run"])
                code, _output, error = self.invoke(workspace)
                self.assertEqual(code, 2)
                self.assertIn("requires --yes", error)
                inventory.assert_not_called()
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
            self.assertFalse(root.exists())
            self.assertEqual(unrelated.read_bytes(), b"preserve sibling")


if __name__ == "__main__":
    unittest.main()
