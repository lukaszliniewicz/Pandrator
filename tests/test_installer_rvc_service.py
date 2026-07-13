import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pandrator_installer.models import LaunchSelection
from pandrator_installer.service import HeadlessInstaller


class InstallerRVCServiceTests(unittest.TestCase):
    @patch("pandrator_installer.service.HeadlessInstaller.run_pixi_in_env")
    @patch("pandrator_installer.service.HeadlessInstaller.get_pixi_manifest_path")
    def test_migration_removes_legacy_in_process_rvc_packages(self, get_manifest, run_pixi):
        installer = HeadlessInstaller(working_dir="workspace")

        with tempfile.NamedTemporaryFile() as manifest:
            get_manifest.return_value = manifest.name
            installer.remove_legacy_rvc_from_pandrator_env("C:/Pandrator")

        command = run_pixi.call_args.args[2]
        self.assertEqual(command[:5], ["python", "-m", "pip", "uninstall", "--yes"])
        self.assertIn("rvc-python", command)
        self.assertIn("fairseq", command)

    def test_rvc_launcher_command_uses_shared_pixi_and_models_directory(self):
        installer = HeadlessInstaller(working_dir="workspace")

        command = installer.build_rvc_launcher_command(
            pixi_path="C:/Pandrator/bin/pixi.exe",
            models_dir="C:/Pandrator/Pandrator/rvc_models",
        )

        self.assertEqual(command[:3], ["cmd", "/c", "run.bat"])
        self.assertIn("--backend", command)
        self.assertIn("cuda", command)
        self.assertIn("--pixi-path", command)
        self.assertIn("--models-dir", command)

    def test_rvc_cpu_launcher_command_selects_cpu_backend(self):
        installer = HeadlessInstaller(working_dir="workspace")

        command = installer.build_rvc_launcher_command(use_cpu=True, prepare_only=True)

        self.assertIn("--backend", command)
        self.assertIn("cpu", command)
        self.assertIn("--prepare-only", command)

    @patch("pandrator_installer.components.is_windows", return_value=False)
    def test_linux_rvc_launcher_uses_pixi_and_run_py(self, _is_windows):
        installer = HeadlessInstaller(working_dir="workspace")

        command = installer.build_rvc_launcher_command(
            use_cpu=True,
            pixi_path="/opt/pandrator/bin/pixi",
            models_dir="/srv/pandrator/rvc_models",
        )

        self.assertEqual(
            command[:7],
            ["/opt/pandrator/bin/pixi", "run", "--environment", "cpu", "python", "run.py", "--backend"],
        )
        self.assertEqual(command[7], "cpu")
        self.assertNotIn("cmd", command)
        self.assertNotIn("--pixi-path", command)
        self.assertEqual(command[-2:], ["--models-dir", "/srv/pandrator/rvc_models"])

    @patch("pandrator_installer.components.normalized_machine", return_value="aarch64")
    def test_linux_rvc_reports_unsupported_architecture(self, _machine):
        installer = HeadlessInstaller(working_dir="workspace")
        with self.assertRaisesRegex(RuntimeError, "requires x86_64"):
            installer._enable_rvc_linux_platform("/missing")

    @patch("pandrator_installer.components.subprocess.run")
    @patch("pandrator_installer.components.normalized_machine", return_value="x86_64")
    @patch("pandrator_installer.components.is_windows", return_value=False)
    def test_linux_rvc_preparation_enables_platform_and_installs_pinned_fairseq(
        self,
        _is_windows,
        _machine,
        run,
    ):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = Path(workspace) / "Pandrator"
            repo = install_root / "rvc-python"
            repo.mkdir(parents=True)
            (repo / "run.py").write_text("print('rvc')\n", encoding="utf-8")
            manifest = repo / "pyproject.toml"
            manifest.write_text(
                '[tool.pixi.workspace]\nplatforms = ["win-64"]\n',
                encoding="utf-8",
            )
            installer = HeadlessInstaller(working_dir=workspace)
            with patch.object(installer, "get_pixi_subprocess_env", return_value={}):
                installer.install_rvc_api_server(
                    str(repo),
                    use_cpu=True,
                    pixi_path="/opt/pandrator/bin/pixi",
                )

            self.assertIn('"linux-64"', manifest.read_text(encoding="utf-8"))
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                commands[0],
                ["/opt/pandrator/bin/pixi", "install", "--environment", "cpu"],
            )
            self.assertIn("hydra-core>=1.3.2", commands[1])
            self.assertEqual(commands[2][3], "install")
            self.assertIn("#sha256=81b5af", commands[2][-1])
            self.assertEqual(commands[3][:4], ["/opt/pandrator/bin/pixi", "run", "--environment", "cpu"])

    @patch("pandrator_installer.runtime.subprocess.Popen")
    @patch("pandrator_installer.components.is_windows", return_value=False)
    @patch("pandrator_installer.runtime.is_windows", return_value=False)
    def test_linux_rvc_server_uses_shared_managed_model_directory(
        self,
        _runtime_windows,
        _component_windows,
        popen,
    ):
        with tempfile.TemporaryDirectory() as workspace:
            repo = Path(workspace) / "Pandrator" / "rvc-python"
            repo.mkdir(parents=True)
            (repo / "run.py").write_text("print('rvc')\n", encoding="utf-8")
            models = Path(workspace) / "Pandrator" / "models" / "rvc"
            installer = HeadlessInstaller(working_dir=workspace)
            process = MagicMock()
            popen.return_value = process
            with patch.object(installer, "is_port_in_use", return_value=False), patch.object(
                installer,
                "get_pixi_subprocess_env",
                return_value={},
            ):
                result = installer.run_rvc_api_server(
                    str(repo),
                    str(models),
                    use_cpu=True,
                    pixi_path="/opt/pandrator/bin/pixi",
                )

            self.assertIs(result, process)
            command = popen.call_args.args[0]
            self.assertEqual(command[:4], ["/opt/pandrator/bin/pixi", "run", "--environment", "cpu"])
            self.assertEqual(command[-2:], ["--models-dir", str(models)])
            installer._close_process_log_handle(result)

    @patch("pandrator_installer.service.HeadlessInstaller.check_rvc_server_online")
    @patch("pandrator_installer.service.HeadlessInstaller.run_rvc_api_server")
    @patch("pandrator_installer.service.HeadlessInstaller.ensure_pandrator_runtime")
    @patch("pandrator_installer.service.HeadlessInstaller.is_port_in_use", return_value=False)
    @patch("pandrator_installer.service.HeadlessInstaller.check_pixi", return_value=True)
    @patch("pandrator_installer.service.HeadlessInstaller.get_pixi_executable", return_value="pixi.exe")
    @patch(
        "pandrator_installer.service.HeadlessInstaller.load_install_config",
        return_value={"rvc_support": True, "rvc_gpu_support": True},
    )
    def test_launch_process_starts_rvc_as_auxiliary_service(
        self,
        _load_config,
        _get_pixi,
        _check_pixi,
        _is_port_in_use,
        _ensure_pandrator,
        run_rvc,
        check_rvc,
    ):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = os.path.join(workspace, "Pandrator")
            os.makedirs(os.path.join(install_root, "rvc-python"))
            installer = HeadlessInstaller(working_dir=workspace)
            process = MagicMock()
            process.poll.return_value = None
            run_rvc.return_value = process
            check_rvc.return_value = True

            installer.launch_process(LaunchSelection(pandrator=False, rvc=True))

            run_rvc.assert_called_once()
            self.assertFalse(run_rvc.call_args.kwargs["use_cpu"])
            self.assertFalse(installer._selected_launch_backend_keys())

    @patch("pandrator_installer.service.HeadlessInstaller.check_rvc_server_online")
    @patch("pandrator_installer.service.HeadlessInstaller.run_rvc_api_server")
    @patch("pandrator_installer.service.HeadlessInstaller.ensure_pandrator_runtime")
    @patch("pandrator_installer.service.HeadlessInstaller.is_port_in_use", return_value=False)
    @patch("pandrator_installer.service.HeadlessInstaller.check_pixi", return_value=True)
    @patch("pandrator_installer.service.HeadlessInstaller.get_pixi_executable", return_value="pixi.exe")
    @patch(
        "pandrator_installer.service.HeadlessInstaller.load_install_config",
        return_value={"rvc_support": True, "rvc_gpu_support": True},
    )
    def test_launch_process_can_force_rvc_cpu(
        self,
        _load_config,
        _get_pixi,
        _check_pixi,
        _is_port_in_use,
        _ensure_pandrator,
        run_rvc,
        check_rvc,
    ):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = os.path.join(workspace, "Pandrator")
            os.makedirs(os.path.join(install_root, "rvc-python"))
            installer = HeadlessInstaller(working_dir=workspace)
            process = MagicMock()
            process.poll.return_value = None
            run_rvc.return_value = process
            check_rvc.return_value = True

            installer.launch_process(LaunchSelection(pandrator=False, rvc=True, rvc_cpu=True))

            self.assertTrue(run_rvc.call_args.kwargs["use_cpu"])


if __name__ == "__main__":
    unittest.main()
