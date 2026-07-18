import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import base64
import hashlib
from unittest import mock
from pathlib import Path

from pandrator_installer.cli import main as launcher_main, parse_launcher_cli_args, run_headless_install_from_cli
from pandrator_installer.lifecycle import SERVICE_HEALTH_URLS, _owned_service_processes, _runtime_specs, main
from pandrator_installer.models import WorkspacePaths, normalize_password_scope
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

    def test_console_entrypoint_uses_process_arguments_when_argv_is_omitted(self):
        output = io.StringIO()
        error = io.StringIO()
        with mock.patch.object(sys, "argv", ["pandrator-installer", "list", "--json"]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main()
        self.assertEqual(code, 0, error.getvalue())
        self.assertIsInstance(json.loads(output.getvalue()), list)

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

    def test_runtime_manifest_omits_bootstrap_secret_for_password_login(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type("Args", (), {"host": "127.0.0.1", "port": 8097})()
            api = _runtime_specs(WorkspacePaths.from_value(workspace), args)[0]
            self.assertNotIn("PANDRATOR_BOOTSTRAP_TOKEN", api.env)

    def test_password_scope_normalization_never_leaves_lan_unprotected(self):
        self.assertEqual(normalize_password_scope("none", network_access=True), "remote")
        self.assertEqual(normalize_password_scope("local", network_access=True), "all")
        self.assertEqual(normalize_password_scope("remote", network_access=False), "none")
        self.assertEqual(normalize_password_scope("all", network_access=False), "local")

    def test_local_password_launch_initializes_owner_and_omits_bootstrap(self):
        with tempfile.TemporaryDirectory() as workspace:
            secret = Path(workspace, "Pandrator", ".flask-secret")
            secret.parent.mkdir(parents=True)
            secret.write_text("old-session-secret", encoding="utf-8")
            with mock.patch.dict(
                "os.environ", {"PANDRATOR_OWNER_PASSWORD": "a-secure-password"}
            ), mock.patch(
                "pandrator_installer.lifecycle.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ) as auth_init, mock.patch(
                "pandrator_installer.lifecycle.ProcessSupervisor"
            ) as supervisor, mock.patch(
                "pandrator_installer.lifecycle._open_browser"
            ) as open_browser:
                code, _output, error = self.invoke(
                    ["launch", "--workspace", workspace, "--password-scope", "local"]
                )
                supervisor.call_args.kwargs["ready_callback"]()
        self.assertEqual(code, 0, error)
        auth_command = auth_init.call_args.args[0]
        self.assertEqual(auth_command[-2:], ["auth", "init"])
        api = supervisor.call_args.kwargs["specs"][0]
        self.assertNotIn("PANDRATOR_BOOTSTRAP_TOKEN", api.env)
        self.assertFalse(secret.exists())
        open_browser.assert_called_once_with("http://127.0.0.1:8097/")

    def test_remote_launch_rejects_an_explicit_passwordless_policy(self):
        with tempfile.TemporaryDirectory() as workspace:
            code, _output, error = self.invoke(
                [
                    "launch", "--workspace", workspace, "--host", "0.0.0.0",
                    "--allow-insecure-remote", "--password-scope", "none", "--no-browser",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("requires password protection", error)

    def test_selected_speech_services_are_owned_by_the_shared_supervisor(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type("Args", (), {"host": "127.0.0.1", "port": 8097, "components": ["rvc_cpu,kokoro"]})()
            specs = _runtime_specs(WorkspacePaths.from_value(workspace), args, "token")
            self.assertEqual([spec.key for spec in specs], ["service-rvc_cpu", "service-kokoro", "api", "worker"])
            self.assertEqual(specs[0].health_url, "http://127.0.0.1:8050/health")
            self.assertEqual(specs[0].startup_timeout_seconds, 600)
            self.assertEqual(specs[1].health_url, "http://127.0.0.1:8880/health")
            self.assertEqual(specs[1].startup_timeout_seconds, 180)
            self.assertIn("service", specs[0].command)

    def test_supervised_services_use_dedicated_readiness_endpoints(self):
        self.assertEqual(
            SERVICE_HEALTH_URLS,
            {
                "xtts": "http://127.0.0.1:8020/health",
                "xtts_cpu": "http://127.0.0.1:8020/health",
                "voxcpm": "http://127.0.0.1:8020/health",
                "fishs2": "http://127.0.0.1:8020/health",
                "fishs2_cpu": "http://127.0.0.1:8020/health",
                "voxtral": "http://127.0.0.1:8000/health",
                "silero": "http://127.0.0.1:8001/ready",
                "kokoro": "http://127.0.0.1:8880/health",
                "kokoro_cpu": "http://127.0.0.1:8880/health",
                "chatterbox": "http://127.0.0.1:8040/health",
                "chatterbox_cpu": "http://127.0.0.1:8040/health",
                "kobold_qwen": "http://127.0.0.1:8042/health",
                "kobold_qwen_cpu": "http://127.0.0.1:8042/health",
                "magpie": "http://127.0.0.1:8030/health",
                "magpie_cpu": "http://127.0.0.1:8030/health",
                "rvc": "http://127.0.0.1:8050/health",
                "rvc_cpu": "http://127.0.0.1:8050/health",
            },
        )

    def test_remote_runtime_passes_security_flags_and_trusted_hosts(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type(
                "Args",
                (),
                {
                    "host": "0.0.0.0",
                    "port": 8123,
                    "components": [],
                    "allow_insecure_remote": True,
                    "trusted_host": ["studio.local"],
                },
            )()
            api = _runtime_specs(WorkspacePaths.from_value(workspace), args, "token")[0]
            self.assertIn("--allow-insecure-remote", api.command)
            trusted_hosts = [api.command[index + 1] for index, value in enumerate(api.command) if value == "--trusted-host"]
            self.assertIn("studio.local", trusted_hosts)
            self.assertIn("127.0.0.1", trusted_hosts)
            self.assertIn("localhost", trusted_hosts)
            self.assertEqual(api.command[api.command.index("--host") + 1], "0.0.0.0")

    def test_remote_runtime_discovers_local_trusted_hosts_when_none_are_supplied(self):
        with tempfile.TemporaryDirectory() as workspace, mock.patch(
            "pandrator_installer.lifecycle.socket.gethostname", return_value="fedora-temp"
        ), mock.patch(
            "pandrator_installer.lifecycle.socket.gethostbyname_ex",
            return_value=("fedora-temp", [], ["192.168.1.42"]),
        ):
            args = type(
                "Args",
                (),
                {
                    "host": "0.0.0.0",
                    "port": 8123,
                    "components": [],
                    "allow_insecure_remote": True,
                    "trusted_host": [],
                },
            )()
            api = _runtime_specs(WorkspacePaths.from_value(workspace), args, "token")[0]
        trusted_hosts = [api.command[index + 1] for index, value in enumerate(api.command) if value == "--trusted-host"]
        self.assertIn("fedora-temp", trusted_hosts)
        self.assertIn("192.168.1.42", trusted_hosts)

    def test_service_process_collection_uses_runtime_tuple_contract(self):
        first = object()
        installer = mock.Mock()
        installer._collect_running_backends.return_value = [("kokoro", "Kokoro", first)]
        self.assertEqual([first], _owned_service_processes(installer))

    def test_legacy_headless_install_alias_remains_parseable(self):
        parsed = parse_launcher_cli_args(["--headless-install", "--workspace", "example", "--components", "whisperx"])
        self.assertTrue(parsed.headless_install)
        self.assertEqual(parsed.components, "whisperx")

    def test_successful_legacy_headless_alias_does_not_repeat_process_shutdown(self):
        with tempfile.TemporaryDirectory() as workspace, mock.patch("pandrator_installer.cli.HeadlessInstaller") as factory:
            args = parse_launcher_cli_args([
                "--headless-install", "--workspace", workspace, "--components", "kokoro_cpu"
            ])
            run_headless_install_from_cli(args)
        factory.return_value.run_headless_install.assert_called_once()
        factory.return_value.shutdown_apps.assert_not_called()

    def test_frozen_launcher_dispatches_hidden_service_command(self):
        arguments = ["service", "--workspace", "example", "--component", "kokoro_cpu"]
        with mock.patch("pandrator_installer.lifecycle.main", return_value=0) as lifecycle_main:
            self.assertEqual(launcher_main(arguments), 0)
        lifecycle_main.assert_called_once_with(arguments)

    def test_successful_install_does_not_repeat_broad_process_shutdown(self):
        with tempfile.TemporaryDirectory() as workspace, mock.patch("pandrator_installer.lifecycle.HeadlessInstaller") as factory:
            code, output, error = self.invoke([
                "install", "--workspace", workspace, "--components", "crispasr", "--crispasr-backend", "vulkan",
                "--crispasr-engine", "parakeet-tdt-0.6b-v3", "--crispasr-model-quantization", "q4_k", "--json"
            ])
        self.assertEqual(0, code, error)
        self.assertEqual("installed", json.loads(output)["status"])
        factory.return_value.run_headless_install.assert_called_once()
        call = factory.return_value.run_headless_install.call_args.kwargs
        self.assertEqual(call["crispasr_engine"], "parakeet-tdt-0.6b-v3")
        self.assertEqual(call["crispasr_model_quantization"], "q4_k")
        factory.return_value.shutdown_apps.assert_not_called()

    def test_failed_install_still_cleans_up_owned_processes(self):
        with tempfile.TemporaryDirectory() as workspace, mock.patch("pandrator_installer.lifecycle.HeadlessInstaller") as factory:
            factory.return_value.run_headless_install.side_effect = RuntimeError("bootstrap failed")
            code, _output, error = self.invoke(["install", "--workspace", workspace, "--components", "crispasr"])
        self.assertEqual(2, code)
        self.assertIn("bootstrap failed", error)
        factory.return_value.shutdown_apps.assert_called_once()

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

    def test_update_clears_maintenance_marker_when_runtime_state_is_malformed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "pandrator-0.49.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            manifest, public = self._signed_release(root, wheel)
            data_root = root / "Pandrator"
            data_root.mkdir()
            (data_root / "runtime-processes.json").write_text("{malformed", encoding="utf-8")
            code, _output, error = self.invoke([
                "update", "--workspace", str(root), "--wheel", str(wheel),
                "--manifest", str(manifest), "--public-key", str(public),
            ])
            self.assertEqual(code, 2)
            self.assertIn("Runtime state is unreadable", error)
            self.assertFalse((data_root / "maintenance.json").exists())

    def test_update_restarts_stopped_supervisor_when_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "pandrator-0.49.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            manifest, public = self._signed_release(root, wheel)
            data_root = root / "Pandrator"
            data_root.mkdir()
            (data_root / "runtime-processes.json").write_text("{}", encoding="utf-8")
            supervisor = mock.Mock(pid=4321)
            supervisor.cmdline.return_value = ["PandratorInstaller", "launch", "--workspace", str(root)]
            supervisor.cwd.return_value = str(root)
            supervisor.is_running.return_value = False
            with mock.patch(
                "pandrator_installer.lifecycle._validated_supervisor_process", return_value=supervisor
            ), mock.patch(
                "pandrator_installer.lifecycle.snapshot_installed_package",
                side_effect=RuntimeError("snapshot failed"),
            ), mock.patch("pandrator_installer.lifecycle.subprocess.Popen") as restart:
                code, _output, error = self.invoke([
                    "update", "--workspace", str(root), "--wheel", str(wheel),
                    "--manifest", str(manifest), "--public-key", str(public),
                ])
            self.assertEqual(code, 2)
            self.assertIn("snapshot failed", error)
            supervisor.terminate.assert_called_once()
            supervisor.wait.assert_called_once_with(timeout=40)
            restart.assert_called_once()
            self.assertEqual(restart.call_args.args[0], supervisor.cmdline.return_value)
            self.assertFalse((data_root / "maintenance.json").exists())

    def test_stop_refuses_a_reused_supervisor_pid(self):
        with tempfile.TemporaryDirectory() as workspace:
            data_root = Path(workspace) / "Pandrator"
            data_root.mkdir()
            state = {
                "instance_id": "old-instance",
                "supervisor_pid": 4321,
                "supervisor_create_time": 100.0,
                "supervisor_executable": sys.executable,
            }
            (data_root / "runtime-processes.json").write_text(json.dumps(state), encoding="utf-8")
            reused_process = mock.Mock(pid=4321)
            reused_process.create_time.return_value = 200.0
            reused_process.exe.return_value = sys.executable
            with mock.patch("pandrator_installer.lifecycle.psutil.Process", return_value=reused_process):
                code, _output, error = self.invoke(["stop", "--workspace", workspace])
            self.assertEqual(code, 2)
            self.assertIn("different process", error)
            reused_process.terminate.assert_not_called()

    def test_stop_terminates_a_matching_supervisor_identity(self):
        with tempfile.TemporaryDirectory() as workspace:
            data_root = Path(workspace) / "Pandrator"
            data_root.mkdir()
            state = {
                "instance_id": "current-instance",
                "supervisor_pid": 4321,
                "supervisor_create_time": 100.0,
                "supervisor_executable": sys.executable,
            }
            (data_root / "runtime-processes.json").write_text(json.dumps(state), encoding="utf-8")
            (data_root / "pandrator.instance.lock").write_text(
                json.dumps({
                    "instance_id": "current-instance",
                    "pid": 4321,
                    "process_create_time": 100.0,
                }),
                encoding="utf-8",
            )
            supervisor = mock.Mock(pid=4321)
            supervisor.create_time.return_value = 100.0
            supervisor.exe.return_value = sys.executable
            with mock.patch("pandrator_installer.lifecycle.psutil.Process", return_value=supervisor):
                code, output, error = self.invoke(["stop", "--workspace", workspace, "--json"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "stop_requested")
            supervisor.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
