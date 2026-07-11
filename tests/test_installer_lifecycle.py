import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pandrator_installer.cli import parse_launcher_cli_args
from pandrator_installer.lifecycle import _runtime_specs, main
from pandrator_installer.models import WorkspacePaths


class InstallerLifecycleTests(unittest.TestCase):
    def invoke(self, arguments):
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(arguments)
        return code, output.getvalue(), error.getvalue()

    def test_plan_is_machine_readable_and_does_not_create_workspace(self):
        with tempfile.TemporaryDirectory() as parent:
            workspace = Path(parent) / "not-created"
            code, output, error = self.invoke(
                ["plan", "--workspace", str(workspace), "--components", "whisperx", "--dry-run", "--json"]
            )
            self.assertEqual(code, 0, error)
            payload = json.loads(output)
            self.assertTrue(payload["dry_run"])
            self.assertIn("whisperx", payload["components"])
            self.assertFalse(workspace.exists())

    def test_uninstall_defaults_to_preserving_data_and_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as workspace:
            code, output, error = self.invoke(["uninstall", "--workspace", workspace, "--dry-run", "--json"])
            self.assertEqual(code, 0, error)
            payload = json.loads(output)
            self.assertTrue(payload["preserve_data"])
            code, _output, error = self.invoke(["uninstall", "--workspace", workspace])
            self.assertEqual(code, 2)
            self.assertIn("requires --yes", error)

    def test_runtime_manifest_uses_argument_arrays_and_one_time_bootstrap_environment(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type("Args", (), {"host": "127.0.0.1", "port": 8097})()
            specs = _runtime_specs(WorkspacePaths.from_value(workspace), args, "one-time-token")
            self.assertEqual([spec.key for spec in specs], ["api", "worker"])
            self.assertEqual(specs[0].env["PANDRATOR_BOOTSTRAP_TOKEN"], "one-time-token")
            self.assertIn("--no-open-browser", specs[0].command)
            self.assertIsInstance(specs[0].command, tuple)

    def test_legacy_headless_install_alias_remains_parseable(self):
        parsed = parse_launcher_cli_args(["--headless-install", "--workspace", "example", "--components", "whisperx"])
        self.assertTrue(parsed.headless_install)
        self.assertEqual(parsed.components, "whisperx")


if __name__ == "__main__":
    unittest.main()
