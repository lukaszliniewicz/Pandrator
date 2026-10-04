import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Literal
from unittest import mock

import pytest

from pandrator_installer.catalog import COMPONENTS
from pandrator_installer.cli import main as launcher_main
from pandrator_installer.cli import (
    parse_launcher_cli_args,
    run_headless_install_from_cli,
)
from pandrator_installer.lifecycle import (
    SERVICE_HEALTH_URLS,
    _owned_service_processes,
    _runtime_specs,
    _service_selection,
    command_launch,
    command_service,
    main,
)
from pandrator_installer.lifecycle_guard import LifecycleBusy, installation_lifecycle_guard
from pandrator_installer.models import WorkspacePaths, normalize_password_scope
from pandrator_installer.supervisor import InstanceLock, ProcessSupervisor
from pandrator_installer.update import verify_release_manifest


@contextlib.contextmanager
def held_owner_inspection(
    root: Path, *, owners: int | None = 0, failure: str | None = None
) -> Iterator[tuple[Path, list[sqlite3.Connection], list[int]]]:
    paths = WorkspacePaths.from_value(root)
    paths.install_root.mkdir(parents=True)
    native_connect = sqlite3.connect
    with contextlib.closing(native_connect(paths.install_root / "pandrator.sqlite3")) as setup, setup:
        if owners is not None:
            setup.execute("CREATE TABLE owner_account(id INTEGER)")
            setup.executemany("INSERT INTO owner_account VALUES (?)", [(i,) for i in range(owners)])
    connections: list[sqlite3.Connection] = []
    closes: list[int] = []

    class HeldConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            if sql == "SELECT COUNT(*) FROM owner_account":
                if failure == "query":
                    raise sqlite3.OperationalError("query sentinel")
                if failure == "interrupt":
                    raise KeyboardInterrupt("inspection sentinel")
            return super().execute(sql, *args, **kwargs)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> Literal[False]:
            result = super().__exit__(exc_type, exc_value, traceback)
            if failure == "context":
                raise sqlite3.OperationalError("context sentinel")
            return result

        def close(self) -> None:
            closes.append(id(self))
            super().close()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if failure == "open":
            raise sqlite3.OperationalError("open sentinel")
        connection = native_connect(*args, factory=HeldConnection, **kwargs)
        connections.append(connection)
        return connection

    try:
        with mock.patch("pandrator_installer.lifecycle.sqlite3.connect", side_effect=connect):
            yield paths.install_root, connections, closes
    finally:
        # Hold strong references until every real-closed-handle assertion has run.
        for connection in connections:
            sqlite3.Connection.close(connection)


def invoke_launch_prepare(root: Path, *, password: str, scope: str = "local") -> int:
    args = argparse.Namespace(
        workspace=str(root), host="127.0.0.1", port=8097,
        password_scope=scope, no_browser=True, components=[],
    )
    with (
        mock.patch.dict(os.environ, {"PANDRATOR_OWNER_PASSWORD": password}),
        mock.patch("pandrator_installer.lifecycle.ProcessSupervisor") as supervisor,
        mock.patch("pandrator_installer.lifecycle._runtime_specs", return_value=[]),
    ):
        supervisor.return_value.run_foreground.side_effect = lambda *, prepare: prepare()
        return command_launch(args)


def assert_inspection_closed(connections: list[sqlite3.Connection], closes: list[int]) -> None:
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert closes == [id(connections[0])]


@pytest.mark.parametrize("owners", [0, 1, None])
def test_launch_owner_inspection_closes_before_authentication(
    tmp_path: Path, owners: int | None
) -> None:
    with held_owner_inspection(tmp_path, owners=owners) as (_installation, connections, closes):
        def authenticate(*_args: Any, **_kwargs: Any) -> mock.Mock:
            assert_inspection_closed(connections, closes)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("pandrator_installer.lifecycle.subprocess.run", side_effect=authenticate) as auth:
            assert invoke_launch_prepare(tmp_path, password="fixture-secure-password") == 0
        assert_inspection_closed(connections, closes)
        assert ("--replace" in auth.call_args.args[0]) == (owners == 1)


@pytest.mark.parametrize("failure", ["open", "query", "context", "interrupt"])
def test_launch_owner_inspection_failure_retires_acquired_native_connection(
    tmp_path: Path, failure: str
) -> None:
    with held_owner_inspection(tmp_path, failure=failure) as (_installation, connections, closes):
        with mock.patch("pandrator_installer.lifecycle.subprocess.run") as auth:
            if failure == "interrupt":
                with pytest.raises(KeyboardInterrupt, match="inspection sentinel"):
                    invoke_launch_prepare(tmp_path, password="", scope="none")
            else:
                assert invoke_launch_prepare(tmp_path, password="", scope="none") == 0
            auth.assert_not_called()
        if failure == "open":
            assert not connections and not closes
        else:
            assert_inspection_closed(connections, closes)


@pytest.mark.parametrize("failure", ["short_password", "auth_child"])
def test_launch_authentication_failure_follows_connection_retirement(
    tmp_path: Path, failure: str
) -> None:
    with held_owner_inspection(tmp_path) as (_installation, connections, closes):
        def authenticate(*_args: Any, **_kwargs: Any) -> mock.Mock:
            assert_inspection_closed(connections, closes)
            return mock.Mock(returncode=1, stdout="", stderr="auth child sentinel")

        with mock.patch("pandrator_installer.lifecycle.subprocess.run", side_effect=authenticate) as auth:
            with pytest.raises(RuntimeError, match="at least 10" if failure == "short_password" else "auth child sentinel"):
                invoke_launch_prepare(
                    tmp_path, password="short" if failure == "short_password" else "fixture-secure-password"
                )
            if failure == "short_password":
                auth.assert_not_called()
            else:
                auth.assert_called_once()
        assert_inspection_closed(connections, closes)


@pytest.mark.parametrize("component", list(SERVICE_HEALTH_URLS))
def test_service_selection_preserves_backend_flags_and_launch_defaults(
    tmp_path: Path, component: str
) -> None:
    args = argparse.Namespace(host="127.0.0.1", port=8097, components=[component])
    specs = _runtime_specs(WorkspacePaths.from_value(tmp_path), args)
    port = COMPONENTS[component].port
    assert specs[0].ports == ((port,) if port else ())
    assert [spec.key for spec in specs] == [f"service-{component}", "api", "worker"]
    selection = _service_selection(component)
    base = component.removesuffix("_cpu")
    backend_fields = ("xtts", "voxcpm", "fishs2", "voxtral", "silero", "kokoro", "chatterbox", "kobold_qwen", "magpie", "rvc")
    for backend in backend_fields:
        assert getattr(selection, backend) is (backend == base)
    for backend in ("xtts", "fishs2", "kokoro", "chatterbox", "kobold_qwen", "magpie", "rvc"):
        assert getattr(selection, f"{backend}_cpu") is (component == f"{backend}_cpu")
    assert selection.selected_backend_keys() == (() if base == "rvc" else (base,))
    assert selection.pandrator is False
    assert selection.pandrator_network_access is False
    assert selection.pandrator_password_scope == "none"
    assert selection.pandrator_port == 8097
    assert selection.disable_deepspeed is False


def test_invalid_direct_service_selection_refuses() -> None:
    with pytest.raises(ValueError, match="Unsupported supervised service: pandrator_password_scope"):
        _service_selection("pandrator_password_scope")


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
                supervisor.return_value.run_foreground.call_args.kwargs["prepare"]()
                supervisor.call_args.kwargs["ready_callback"]()
        self.assertEqual(code, 0, error)
        auth_command = auth_init.call_args.args[0]
        self.assertEqual(auth_command[-2:], ["auth", "init"])
        api = supervisor.call_args.kwargs["specs"][0]
        self.assertNotIn("PANDRATOR_BOOTSTRAP_TOKEN", api.env)
        self.assertFalse(secret.exists())
        open_browser.assert_called_once_with("http://127.0.0.1:8097/")

    def test_duplicate_launch_cannot_change_authentication_or_consume_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pandrator"
            owner = InstanceLock(root / "pandrator.instance.lock")
            owner.acquire()
            secret = root / ".flask-secret"
            secret.write_bytes(b"original signing secret")
            before = secret.stat()
            try:
                with (
                    mock.patch.dict(os.environ, {"PANDRATOR_OWNER_PASSWORD": "fixture-password"}),
                    mock.patch("pandrator_installer.lifecycle._runtime_specs", return_value=[]),
                    mock.patch("pandrator_installer.lifecycle.subprocess.run") as auth,
                ):
                    code, _output, error = self.invoke([
                        "launch", "--workspace", directory, "--password-scope", "local", "--no-browser",
                    ])
                    self.assertEqual(code, 2, error)
                    self.assertIn("already supervised", error)
                    auth.assert_not_called()
                    self.assertEqual(os.environ.get("PANDRATOR_OWNER_PASSWORD"), "fixture-password")
                self.assertEqual(secret.read_bytes(), b"original signing secret")
                self.assertEqual(secret.stat(), before)
                self.assertTrue(owner.acquired)
            finally:
                owner.release()

    def test_failed_authentication_releases_new_supervisor_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pandrator"
            with (
                mock.patch.dict(os.environ, {"PANDRATOR_OWNER_PASSWORD": "short"}),
                mock.patch("pandrator_installer.lifecycle._runtime_specs", return_value=[]),
            ):
                code, _output, error = self.invoke([
                    "launch", "--workspace", directory, "--password-scope", "local", "--no-browser",
                ])
            self.assertEqual(code, 2, error)
            self.assertIn("at least 10", error)
            self.assertFalse((root / "pandrator.instance.lock").exists())
            self.assertFalse((root / "runtime-processes.json").exists())
            with installation_lifecycle_guard(root, shared=False):
                pass
            with InstanceLock(root / "pandrator.instance.lock"):
                pass

    def test_service_holds_shared_admission_through_backend_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pandrator"
            installer = mock.Mock()
            observations = []
            errors = []

            def check_lease(stage):
                def inspect():
                    try:
                        with installation_lifecycle_guard(root, shared=False):
                            errors.append("exclusive overlapped " + stage)
                    except LifecycleBusy:
                        observations.append(stage)
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=inspect)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            def launch(_selection):
                check_lease("launch")
                handler = signal.getsignal(signal.SIGTERM)
                self.assertTrue(callable(handler))
                if callable(handler):
                    handler(signal.SIGTERM, None)

            installer.launch_process.side_effect = launch
            installer.shutdown_apps.side_effect = lambda: check_lease("cleanup")
            args = type("Args", (), {"workspace": directory, "component": "rvc"})()
            with (
                mock.patch("pandrator_installer.lifecycle.HeadlessInstaller", return_value=installer),
                mock.patch("pandrator_installer.lifecycle._owned_service_processes", return_value=[mock.Mock()]),
            ):
                self.assertEqual(command_service(args), 0)
            self.assertEqual(errors, [])
            self.assertEqual(observations, ["launch", "cleanup"])
            with installation_lifecycle_guard(root, shared=False):
                pass

    def test_service_cleanup_failure_still_restores_handlers_and_releases_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pandrator"
            installer = mock.Mock()
            installer.launch_process.side_effect = RuntimeError("startup failed")
            installer.shutdown_apps.side_effect = RuntimeError("cleanup failed")
            previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
            args = type("Args", (), {"workspace": directory, "component": "rvc"})()
            with mock.patch("pandrator_installer.lifecycle.HeadlessInstaller", return_value=installer):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    command_service(args)
            installer.shutdown_logging.assert_called_once_with()
            self.assertEqual({signum: signal.getsignal(signum) for signum in previous}, previous)
            with installation_lifecycle_guard(root, shared=False):
                pass

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

    def test_runtime_specs_deduplicate_repeated_components(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type(
                "Args",
                (),
                {
                    "host": "127.0.0.1",
                    "port": 8097,
                    "components": ["rvc_cpu,rvc_cpu"],
                },
            )()
            specs = _runtime_specs(WorkspacePaths.from_value(workspace), args)
            self.assertEqual(
                [spec.key for spec in specs],
                ["service-rvc_cpu", "api", "worker"],
            )

    def test_runtime_supervisor_rejects_services_that_share_a_port(self):
        with tempfile.TemporaryDirectory() as workspace:
            args = type(
                "Args",
                (),
                {
                    "host": "127.0.0.1",
                    "port": 8097,
                    "components": ["xtts,fishs2"],
                },
            )()
            specs = _runtime_specs(WorkspacePaths.from_value(workspace), args)
            with self.assertRaisesRegex(ValueError, "port 8020"):
                ProcessSupervisor(
                    data_root=Path(workspace) / "Pandrator",
                    specs=specs,
                )

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

    def test_legacy_compatibility_entrypoint_requires_an_explicit_command(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = launcher_main([])

        self.assertEqual(code, 2)
        self.assertIn("--headless-install", output.getvalue())
        self.assertIn("pandrator-installer --help", output.getvalue())

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

    @mock.patch("pandrator_installer.lifecycle.validate_update_environment")
    @mock.patch("pandrator_installer.lifecycle.validate_update_package")
    def test_live_update_activates_only_after_signature_snapshot_migration_and_health(self, _validate_package, _validate_environment):
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

    @mock.patch("pandrator_installer.lifecycle.validate_update_environment")
    @mock.patch("pandrator_installer.lifecycle.validate_update_package")
    def test_update_clears_maintenance_marker_when_runtime_state_is_malformed(self, _validate_package, _validate_environment):
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

    @mock.patch("pandrator_installer.lifecycle.validate_update_environment")
    @mock.patch("pandrator_installer.lifecycle.validate_update_package")
    def test_update_restarts_stopped_supervisor_when_snapshot_fails(self, _validate_package, _validate_environment):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "pandrator-0.49.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            manifest, public = self._signed_release(root, wheel)
            data_root = root / "Pandrator"
            data_root.mkdir()
            state = {
                "instance_id": "stopped-fixture",
                "supervisor_pid": 2_000_000_001,
                "supervisor_create_time": 1.0,
                "supervisor_executable": sys.executable,
                "processes": {},
            }
            (data_root / "runtime-processes.json").write_text(json.dumps(state), encoding="utf-8")
            supervisor = mock.Mock(pid=2_000_000_001)
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
            with mock.patch(
                "pandrator_installer.process_identity.psutil.Process",
                return_value=reused_process,
            ):
                code, _output, error = self.invoke(["stop", "--workspace", workspace])
            self.assertEqual(code, 2)
            self.assertIn("unrelated PID", error)
            reused_process.terminate.assert_not_called()

    @mock.patch("pandrator_installer.lifecycle.validate_update_environment")
    @mock.patch("pandrator_installer.lifecycle.validate_update_package")
    def test_update_preserves_replacement_state_at_each_stop_cleanup_boundary(self, _validate_package, _validate_environment):
        import os

        import psutil

        from pandrator_installer.runtime_metadata_files import runtime_metadata_guard

        for boundary in ("stale", "disappeared", "waited"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wheel = root / "pandrator-0.49.0-py3-none-any.whl"
                wheel.write_bytes(b"wheel")
                manifest, public = self._signed_release(root, wheel)
                data_root = root / "Pandrator"
                data_root.mkdir()
                state_path = data_root / "runtime-processes.json"
                state_path.write_text('{"instance_id":"old"}', encoding="utf-8")
                replacement = b'{"instance_id":"new","supervisor_pid":2000000002}'
                supervisor = mock.Mock(pid=2000000001)
                supervisor.cmdline.return_value = ["fixture-launcher"]
                supervisor.cwd.return_value = directory

                def replace_state(
                    *_args, _root=data_root, _state=state_path, _data=replacement, **_kwargs
                ):
                    # A cooperative publisher must be able to run during wait.
                    with runtime_metadata_guard(_root, timeout=0.2):
                        temporary = _state.with_suffix(".replacement")
                        temporary.write_bytes(_data)
                        os.replace(temporary, _state)

                def inspect(
                    _paths, _payload, _boundary=boundary, _supervisor=supervisor, _replace=replace_state
                ):
                    if _boundary == "stale":
                        _replace()
                        return None
                    return _supervisor

                def disappear(_replace=replace_state):
                    _replace()
                    raise psutil.NoSuchProcess(2000000001)

                if boundary == "disappeared":
                    supervisor.terminate.side_effect = disappear
                if boundary == "waited":
                    supervisor.wait.side_effect = replace_state
                with (
                    mock.patch("pandrator_installer.lifecycle._validated_supervisor_process", side_effect=inspect),
                    mock.patch(
                        "pandrator_installer.lifecycle.snapshot_installed_package",
                        side_effect=RuntimeError("snapshot sentinel"),
                    ) as snapshot,
                    mock.patch("pandrator_installer.lifecycle.subprocess.Popen") as restart,
                ):
                    code, _output, error = self.invoke([
                        "update", "--workspace", directory, "--wheel", str(wheel),
                        "--manifest", str(manifest), "--public-key", str(public),
                    ])
                self.assertEqual(code, 2)
                self.assertIn("snapshot sentinel", error)
                snapshot.assert_called_once()
                self.assertEqual(state_path.read_bytes(), replacement)
                self.assertFalse((data_root / "maintenance.json").exists())
                if boundary == "waited":
                    supervisor.wait.assert_called_once_with(timeout=40)
                    restart.assert_called_once()
                else:
                    restart.assert_not_called()

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
                    "executable": sys.executable,
                }),
                encoding="utf-8",
            )
            supervisor = mock.Mock(pid=4321)
            supervisor.create_time.return_value = 100.0
            supervisor.exe.return_value = sys.executable
            with mock.patch(
                "pandrator_installer.process_identity.psutil.Process",
                return_value=supervisor,
            ):
                code, output, error = self.invoke(["stop", "--workspace", workspace, "--json"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "stop_requested")
            supervisor.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
