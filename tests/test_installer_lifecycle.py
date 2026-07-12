import contextlib
import io
import json
import tempfile
import unittest
import base64
import hashlib
from unittest import mock
from pathlib import Path

from pandrator_installer.cli import parse_launcher_cli_args
from pandrator_installer.lifecycle import _runtime_specs, main
from pandrator_installer.models import WorkspacePaths
from pandrator_installer.update import verify_release_manifest


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
            self.assertIn("crispasr", payload["components"])
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

    def test_selected_speech_services_are_owned_by_the_shared_supervisor(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type("Args", (), {"host": "127.0.0.1", "port": 8097, "components": ["rvc_cpu,kokoro"]})()
            specs = _runtime_specs(WorkspacePaths.from_value(workspace), args, "token")
            self.assertEqual([spec.key for spec in specs], ["service-rvc_cpu", "service-kokoro", "api", "worker"])
            self.assertEqual(specs[0].health_url, "http://127.0.0.1:8050/health")
            self.assertIn("service", specs[0].command)

    def test_legacy_headless_install_alias_remains_parseable(self):
        parsed = parse_launcher_cli_args(["--headless-install", "--workspace", "example", "--components", "whisperx"])
        self.assertTrue(parsed.headless_install)
        self.assertEqual(parsed.components, "whisperx")

    def _signed_release(self, root: Path, wheel: Path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        public = root / "release-public.pem"
        public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        signed = {"version": "0.49.0", "wheel": {"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}}
        canonical = json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest = root / "release.json"
        manifest.write_text(json.dumps({"signed": signed, "signature": base64.b64encode(private.sign(canonical)).decode("ascii")}), encoding="utf-8")
        return manifest, public

    def test_release_manifest_verifies_exact_wheel_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "pandrator-0.49.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            manifest, public = self._signed_release(root, wheel)
            verified = verify_release_manifest(manifest, public)
            self.assertEqual(verified.version, "0.49.0")
            self.assertEqual(verified.wheel_name, wheel.name)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["signed"]["version"] = "9.9.9"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "signature"):
                verify_release_manifest(manifest, public)

    def test_live_update_activates_only_after_signature_snapshot_migration_and_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "pandrator-0.49.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            manifest, public = self._signed_release(root, wheel)
            site_packages = root / "site-packages"
            site_packages.mkdir()
            with mock.patch("pandrator_installer.lifecycle.snapshot_installed_package", return_value=site_packages) as snapshot, mock.patch("pandrator_installer.lifecycle.install_wheel") as install, mock.patch("pandrator_installer.lifecycle.run_migrations") as migrate, mock.patch("pandrator_installer.lifecycle.health_check") as health:
                code, output, error = self.invoke(["update", "--workspace", str(root), "--wheel", str(wheel), "--manifest", str(manifest), "--public-key", str(public), "--json"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "updated")
            snapshot.assert_called_once()
            install.assert_called_once()
            migrate.assert_called_once()
            health.assert_called_once()


if __name__ == "__main__":
    unittest.main()
