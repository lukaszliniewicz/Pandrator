import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandrator_manager.autostart import LinuxSystemdAutostart, WindowsAutostart
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.errors import ManagerError
from pandrator_manager.launcher import (
    LauncherRuntime,
    _installed_launcher_workspace,
    deployment_endpoint,
    external_cleanup_runtime,
    install_stable_launcher,
    installed_launcher,
    launcher_metadata_path,
    main,
    resolve_launcher_workspace,
    runtime_command,
    stable_launcher_path,
    stage_cleanup_launcher,
)
from pandrator_manager.lifecycle import external_cleanup_runtime_available
from pandrator_manager.network import AccessMode, EndpointExposure
from pandrator_manager.workspace_selection import (
    WorkspaceSelectionUnavailable,
    _select_windows_directory,
    default_workspace,
    launcher_settings_path,
    legacy_launcher_settings_path,
    load_remembered_workspace,
    remember_workspace,
    select_workspace_directory,
)


class StableLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.layout = WorkspaceLayout.from_value(root / "workspace")
        self.layout.workspace.mkdir()
        suffix = ".exe" if os.name == "nt" else ""
        self.source = root / f"bootstrap{suffix}"
        self.source.write_bytes(b"pandrator native bootstrap fixture")

    def test_install_is_atomic_digest_verified_and_enables_native_cleanup(self):
        runtime = install_stable_launcher(
            self.layout,
            source=self.source,
        )

        self.assertEqual(runtime.mode, "native_launcher")
        self.assertEqual(runtime.executable, stable_launcher_path(self.layout))
        self.assertTrue(external_cleanup_runtime_available(self.layout))
        self.assertEqual(
            external_cleanup_runtime(self.layout),
            installed_launcher(self.layout, strict=True),
        )
        metadata = json.loads(
            launcher_metadata_path(self.layout).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["sha256"], runtime.sha256)
        self.assertEqual(metadata["workspace"], str(self.layout.workspace))
        self.assertEqual(
            self.layout.workspace,
            _installed_launcher_workspace(runtime.executable),
        )
        self.assertEqual(
            metadata["size_bytes"],
            stable_launcher_path(self.layout).stat().st_size,
        )
        if os.name != "nt":
            self.assertTrue(os.access(runtime.executable, os.X_OK))

    def test_corrupt_installed_launcher_fails_closed(self):
        install_stable_launcher(self.layout, source=self.source)
        stable_launcher_path(self.layout).write_bytes(b"tampered")

        with self.assertRaises(ManagerError) as raised:
            installed_launcher(self.layout, strict=True)
        self.assertEqual(raised.exception.code, "invalid_stable_launcher")

    def test_cleanup_copy_and_commands_are_operation_specific(self):
        installed = install_stable_launcher(
            self.layout,
            source=self.source,
        )
        external = self.layout.workspace / "control" / (
            "operation.cleanup.exe"
            if os.name == "nt"
            else "operation.cleanup"
        )
        staged = stage_cleanup_launcher(installed, external)

        self.assertEqual(staged.sha256, installed.sha256)
        self.assertEqual(external.read_bytes(), self.source.read_bytes())
        self.assertEqual(
            runtime_command(
                staged,
                action="uninstall",
                workspace=self.layout.workspace,
                operation_id="operation",
            ),
            [
                str(external.resolve()),
                "uninstall",
                "--workspace",
                str(self.layout.workspace),
                "--operation-id",
                "operation",
            ],
        )
        python = LauncherRuntime(
            mode="python",
            executable=Path("/qualified/python"),
        )
        self.assertEqual(
            runtime_command(
                python,
                action="handoff",
                workspace=self.layout.workspace,
                operation_id="operation",
            )[1:3],
            ["-m", "pandrator_manager.releases.handoff"],
        )

    def test_autostart_targets_the_stable_launcher_when_installed(self):
        installed = install_stable_launcher(
            self.layout,
            source=self.source,
        )
        integrations = Path(self.temporary.name) / "integrations"
        windows = WindowsAutostart(
            self.layout,
            startup_directory=integrations / "startup",
        )
        windows.install()
        windows_text = windows.path.read_text(encoding="utf-8")
        self.assertIn(str(installed.executable), windows_text)
        self.assertIn('"start"', windows_text)
        self.assertNotIn("pandrator_manager.cli", windows_text)

        linux = LinuxSystemdAutostart(
            self.layout,
            unit_directory=integrations / "systemd",
            systemctl="systemctl",
        )
        linux.install(activate=False)
        linux_text = linux.path.read_text(encoding="utf-8")
        self.assertIn(
            str(installed.executable).replace("\\", "\\\\"),
            linux_text,
        )
        self.assertIn('"start"', linux_text)
        self.assertNotIn("pandrator_manager.cli", linux_text)

    def test_remote_setup_profiles_are_explicit_and_proxy_aware(self):
        local = EndpointExposure(port=8097)
        private = deployment_endpoint(
            "http://gpu-box.local:8097",
            local,
            bind_host="0.0.0.0",
            configured_port=None,
            default_port=8097,
            trusted_proxy_hops=1,
            allow_insecure_private_network=True,
        )
        self.assertEqual(AccessMode.PRIVATE_NETWORK, private.mode)
        self.assertEqual(8097, private.port)
        self.assertEqual(0, private.proxy_hops)

        proxied = deployment_endpoint(
            "https://pandrator.example",
            local,
            bind_host=None,
            configured_port=18097,
            default_port=8097,
            trusted_proxy_hops=2,
            allow_insecure_private_network=False,
        )
        self.assertEqual(AccessMode.HTTPS_PROXY, proxied.mode)
        self.assertEqual("127.0.0.1", proxied.bind_host)
        self.assertEqual(18097, proxied.port)
        self.assertEqual(2, proxied.proxy_hops)

        with self.assertRaisesRegex(ValueError, "requires"):
            deployment_endpoint(
                "http://gpu-box.local:8097",
                local,
                bind_host="0.0.0.0",
                configured_port=None,
                default_port=8097,
                trusted_proxy_hops=1,
                allow_insecure_private_network=False,
            )
        with self.assertRaisesRegex(ValueError, "must match"):
            deployment_endpoint(
                "http://gpu-box.local:8097",
                local,
                bind_host="0.0.0.0",
                configured_port=8098,
                default_port=8097,
                trusted_proxy_hops=1,
                allow_insecure_private_network=True,
            )

    def test_headless_setup_prints_a_recovery_link_without_opening_browser(self):
        client = mock.Mock()
        client.recovery_url.return_value = (
            "https://setup.example/recovery#token=one-use"
        )
        runtime = LauncherRuntime(
            mode="native_launcher",
            executable=self.source,
            sha256="a" * 64,
        )
        with (
            mock.patch(
                "pandrator_manager.launcher.install_stable_launcher",
                return_value=runtime,
            ),
            mock.patch(
                "pandrator_manager.client.ManagerClient.ensure_running",
                return_value=client,
            ),
            mock.patch(
                "pandrator_manager.launcher.remember_workspace",
                return_value=Path(self.temporary.name) / "manager-launcher.json",
            ) as remember,
            mock.patch("pandrator_manager.launcher.webbrowser.open") as browser,
        ):
            result = main(
                [
                    "setup",
                    "--workspace",
                    str(self.layout.workspace),
                    "--no-open",
                ]
            )

        self.assertEqual(0, result)
        client.recovery_url.assert_called_once_with()
        browser.assert_not_called()
        remember.assert_called_once_with(self.layout.workspace)


class WorkspaceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "config"
        self.environ = {
            "LOCALAPPDATA": str(self.config),
            "XDG_CONFIG_HOME": str(self.config),
        }
        self.workspace = self.root / "selected"
        self.workspace.mkdir()

    def test_workspace_preference_is_atomic_and_shared_by_future_defaults(self):
        with mock.patch(
            "pandrator_manager.workspace_selection.protect_path"
        ):
            path = remember_workspace(
                self.workspace,
                system="windows",
                environ=self.environ,
                home=self.home,
            )

        self.assertEqual(
            path,
            launcher_settings_path(
                system="windows",
                environ=self.environ,
                home=self.home,
            ),
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(str(self.workspace.resolve()), payload["workspace"])
        self.assertEqual(
            self.workspace.resolve(),
            load_remembered_workspace(
                system="windows",
                environ=self.environ,
                home=self.home,
            ),
        )
        self.assertEqual(
            self.workspace.resolve(),
            default_workspace(environ=self.environ, home=self.home),
        )
        self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_legacy_qt_workspace_preference_remains_discoverable(self):
        legacy = legacy_launcher_settings_path(
            system="linux",
            environ=self.environ,
            home=self.home,
        )
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            json.dumps({"workspace": str(self.workspace)}),
            encoding="utf-8",
        )

        self.assertEqual(
            self.workspace.resolve(),
            load_remembered_workspace(
                system="linux",
                environ=self.environ,
                home=self.home,
            ),
        )

    def test_invalid_current_preference_never_falls_back_to_stale_legacy_path(self):
        current = launcher_settings_path(
            system="linux",
            environ=self.environ,
            home=self.home,
        )
        legacy = legacy_launcher_settings_path(
            system="linux",
            environ=self.environ,
            home=self.home,
        )
        current.parent.mkdir(parents=True)
        current.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace": str(self.root / "missing"),
                }
            ),
            encoding="utf-8",
        )
        legacy.write_text(
            json.dumps({"workspace": str(self.workspace)}),
            encoding="utf-8",
        )

        self.assertIsNone(
            load_remembered_workspace(
                system="linux",
                environ=self.environ,
                home=self.home,
            )
        )

    def test_resolution_precedence_prevents_workspace_drift(self):
        remembered = self.root / "remembered"
        remembered.mkdir()
        environment = self.root / "environment"
        environment.mkdir()
        explicit = self.root / "explicit"
        explicit.mkdir()

        with (
            mock.patch(
                "pandrator_manager.launcher.load_remembered_workspace",
                return_value=remembered,
            ),
            mock.patch(
                "pandrator_manager.launcher._installed_launcher_workspace",
                return_value=self.workspace,
            ),
        ):
            environment_result = resolve_launcher_workspace(
                None,
                command="start",
                environ={"PANDRATOR_WORKSPACE": str(environment)},
                home=self.home,
            )
            installed_result = resolve_launcher_workspace(
                None,
                command="start",
                environ={},
                home=self.home,
            )
            explicit_result = resolve_launcher_workspace(
                explicit,
                command="setup",
                environ={"PANDRATOR_WORKSPACE": str(environment)},
                home=self.home,
            )

        self.assertEqual(environment.resolve(), environment_result.workspace)
        self.assertEqual("environment", environment_result.source)
        self.assertEqual(self.workspace.resolve(), installed_result.workspace)
        self.assertEqual("installed_launcher", installed_result.source)
        self.assertEqual(explicit.resolve(), explicit_result.workspace)
        self.assertEqual("command_line", explicit_result.source)

    def test_first_setup_selects_a_folder_and_cancellation_has_no_fallback(self):
        with (
            mock.patch(
                "pandrator_manager.launcher.load_remembered_workspace",
                return_value=None,
            ),
            mock.patch(
                "pandrator_manager.launcher._installed_launcher_workspace",
                return_value=None,
            ),
            mock.patch(
                "pandrator_manager.launcher.select_workspace_directory",
                side_effect=[self.workspace, None],
            ),
        ):
            selected = resolve_launcher_workspace(
                None,
                command="setup",
                environ={},
                home=self.home,
            )
            cancelled = resolve_launcher_workspace(
                None,
                command="setup",
                environ={},
                home=self.home,
            )

        self.assertEqual(self.workspace.resolve(), selected.workspace)
        self.assertEqual("folder_chooser", selected.source)
        self.assertTrue(cancelled.cancelled)
        self.assertEqual("cancelled", cancelled.source)

    def test_forced_picker_reports_unavailable_instead_of_changing_location(self):
        with (
            mock.patch(
                "pandrator_manager.launcher.load_remembered_workspace",
                return_value=self.workspace,
            ),
            mock.patch(
                "pandrator_manager.launcher._installed_launcher_workspace",
                return_value=None,
            ),
            mock.patch(
                "pandrator_manager.launcher.select_workspace_directory",
                side_effect=WorkspaceSelectionUnavailable("no desktop"),
            ),
            self.assertRaises(ManagerError) as raised,
        ):
            resolve_launcher_workspace(
                None,
                command="setup",
                choose_workspace=True,
                environ={},
                home=self.home,
            )

        self.assertEqual(
            "workspace_selection_unavailable",
            raised.exception.code,
        )

    def test_headless_linux_never_invokes_a_desktop_chooser(self):
        with self.assertRaises(WorkspaceSelectionUnavailable) as raised:
            select_workspace_directory(
                self.home,
                system="linux",
                environ={},
            )

        self.assertIn("graphical", str(raised.exception))

    @unittest.skipUnless(
        os.name == "nt",
        "Windows shell chooser construction requires Win32.",
    )
    def test_windows_native_chooser_cancellation_is_side_effect_free(self):
        import ctypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        with (
            mock.patch.object(
                shell32,
                "SHBrowseForFolderW",
                return_value=None,
            ),
            mock.patch.object(
                ole32,
                "CoInitializeEx",
                return_value=0,
            ),
            mock.patch.object(ole32, "CoUninitialize"),
        ):
            selected = _select_windows_directory(self.home)

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
