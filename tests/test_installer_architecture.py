import unittest
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from pandrator_installer.catalog import (
    COMPONENTS,
    LINUX_DEFERRED_INSTALL_COMPONENT_KEYS,
    PACKAGING_COMPONENT_PATHS,
    PACKAGING_CONFIG_FLAGS,
)
from pandrator_installer.cli import parse_launcher_cli_args, run_self_check
from pandrator_installer.models import InstallSelection, LaunchSelection, WorkspacePaths
from pandrator_installer import platforms
from pandrator_installer.reporting import HeadlessReporter
from pandrator_installer.service import HeadlessInstaller
from pandrator_installer.constants import NEMO_PYNINI_CONDA_SPEC, PANDRATOR_NUMPY_SPEC


class InstallerArchitectureTests(unittest.TestCase):
    def test_install_selection_resolves_dependencies(self):
        selection = InstallSelection.from_components(["xtts_finetuning"])
        self.assertEqual(
            set(selection.selected_components()),
            {"xtts", "whisperx", "xtts_finetuning"},
        )

    def test_install_selection_rejects_mutually_exclusive_variants(self):
        with self.assertRaisesRegex(ValueError, "Select either 'kokoro' or 'kokoro_cpu'"):
            InstallSelection.from_components(["kokoro", "kokoro_cpu"])
        with self.assertRaisesRegex(ValueError, "Select either 'rvc' or 'rvc_cpu'"):
            InstallSelection.from_components(["rvc", "rvc_cpu"])

    def test_launch_selection_preserves_backend_priority(self):
        selection = LaunchSelection(voxcpm=True, chatterbox=True, rvc=True)
        self.assertEqual(selection.selected_backend_keys(), ("voxcpm", "chatterbox"))
        self.assertTrue(selection.rvc)

    def test_workspace_paths_are_rooted_under_workspace(self):
        paths = WorkspacePaths.from_value(Path("workspace"))
        self.assertEqual(paths.install_root.name, "Pandrator")
        self.assertEqual(paths.pandrator_repo.name, "Pandrator")
        self.assertEqual(paths.subdub_repo.name, "Subdub")

    def test_headless_installer_does_not_require_widgets(self):
        installer = HeadlessInstaller(working_dir="workspace")
        self.assertTrue(installer.headless)
        self.assertIsInstance(installer.reporter, HeadlessReporter)
        self.assertFalse(hasattr(installer, "pandrator_checkbox"))

    def test_installer_launch_environment_forces_utf8_for_nemo_grammars(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            env = installer.get_pixi_subprocess_env(install_root)

        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertTrue(env["PADDLE_PDX_CACHE_HOME"].endswith(os.path.join("cache", "paddlex")))
        self.assertEqual(env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"], "True")

    def test_package_cache_cleanup_preserves_model_caches(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            pixi_cache = os.path.join(install_root, ".pixi-cache")
            disposable_dirs = [
                os.path.join(pixi_cache, "pip"),
                os.path.join(pixi_cache, "pkgs"),
                os.path.join(pixi_cache, "rattler"),
                os.path.join(pixi_cache, "repodata"),
                os.path.join(pixi_cache, "uv-cache"),
                os.path.join(pixi_cache, "tmp"),
            ]
            model_cache = os.path.join(
                install_root,
                "cache",
                "huggingface",
                "hub",
                "models--segment-any-text--sat-12l-sm",
            )

            for cache_dir in disposable_dirs + [model_cache]:
                os.makedirs(cache_dir)
                with open(os.path.join(cache_dir, "artifact"), "w", encoding="utf-8") as file:
                    file.write("cached")

            installer.cleanup_installer_package_caches(install_root)

            for cache_dir in disposable_dirs:
                self.assertTrue(os.path.isdir(cache_dir))
                self.assertEqual(os.listdir(cache_dir), [])
            self.assertTrue(os.path.exists(os.path.join(model_cache, "artifact")))

    def test_platform_helpers_preserve_windows_and_linux_pixi_defaults(self):
        self.assertEqual(platforms.pixi_binary_name("Windows"), "pixi.exe")
        self.assertEqual(platforms.pixi_binary_name("Linux"), "pixi")
        self.assertEqual(platforms.pixi_temp_suffix("Windows"), ".exe")
        self.assertEqual(platforms.pixi_temp_suffix("Linux"), "")
        self.assertEqual(platforms.pixi_manifest_platform("Windows", "AMD64"), "win-64")
        self.assertEqual(platforms.pixi_manifest_platform("Linux", "x86_64"), "linux-64")
        self.assertEqual(platforms.pixi_manifest_platform("Linux", "aarch64"), "linux-aarch64")
        self.assertIn("windows-msvc.exe", platforms.pixi_download_url("Windows", "AMD64"))
        self.assertIn("linux-musl", platforms.pixi_download_url("Linux", "x86_64"))

    def test_appimage_launcher_defaults_to_home_workspace(self):
        workspace = platforms.resolve_launcher_workspace(
            system="Linux",
            environ={"APPIMAGE": "/tmp/PandratorInstaller-x86_64.AppImage"},
            cwd="/tmp/desktop-launch-cwd",
            home="/home/tester",
        )
        self.assertEqual(workspace, os.path.abspath("/home/tester"))

    def test_explicit_launcher_workspace_overrides_appimage_default(self):
        workspace = platforms.resolve_launcher_workspace(
            value="/tmp/custom-pandrator",
            system="Linux",
            environ={"APPIMAGE": "/tmp/PandratorInstaller-x86_64.AppImage"},
            cwd="/tmp/desktop-launch-cwd",
            home="/home/tester",
        )
        self.assertEqual(workspace, os.path.abspath("/tmp/custom-pandrator"))

    def test_ensure_pixi_manifest_uses_platform_manifest_value(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            with patch("pandrator_installer.pixi.pixi_manifest_platform", return_value="linux-64"):
                manifest_path = installer.ensure_pixi_manifest(install_root, "test_env", "3.11")

            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_contents = f.read()

        self.assertIn('platforms = ["linux-64"]', manifest_contents)

    def test_ensure_pixi_manifest_updates_existing_platform_value(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            manifest_dir = os.path.join(install_root, "envs", "test_env")
            os.makedirs(manifest_dir)
            manifest_path = os.path.join(manifest_dir, "pixi.toml")
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(
                    "[workspace]\n"
                    "name = \"test_env\"\n"
                    "channels = [\"conda-forge\"]\n"
                    "platforms = [\"win-64\"]\n\n"
                    "[dependencies]\n"
                    "python = \"3.10.*\"\n"
                    "pip = \"*\"\n"
                )

            with patch("pandrator_installer.pixi.pixi_manifest_platform", return_value="linux-64"):
                installer.ensure_pixi_manifest(install_root, "test_env", "3.11")

            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_contents = f.read()

        self.assertIn('platforms = ["linux-64"]', manifest_contents)
        self.assertIn('python = "3.11.*"', manifest_contents)

    def test_program_detection_uses_shutil_which(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with patch("pandrator_installer.operations.shutil.which", return_value="/usr/bin/git") as mock_which:
            self.assertTrue(installer.check_program_installed("git"))

        mock_which.assert_called_once_with("git")

    def test_non_windows_dependency_setup_does_not_install_calibre(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with patch("pandrator_installer.operations.os.name", "posix"):
            with patch.object(installer, "check_calibre_available", return_value=False):
                with patch.object(installer, "install_calibre") as install_calibre:
                    self.assertTrue(installer.install_dependencies("install-root"))

        install_calibre.assert_not_called()

    def test_non_windows_backend_component_install_is_deferred(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["chatterbox_cpu"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "deferred"):
                installer.validate_platform_install_selection(selection)

    def test_non_windows_kokoro_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["kokoro_cpu"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_core_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components([], install_pandrator=True)

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_update_with_backend_config_is_deferred(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"chatterbox_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "deferred"):
                installer.validate_platform_update_config(config)

    def test_non_windows_update_with_kokoro_config_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"kokoro_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_update_config(config)

    def test_non_windows_update_with_core_config_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"kokoro_support": False, "whisperx_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_update_config(config)

    def test_kokoro_pythonpath_uses_os_pathsep(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            kokoro_repo = os.path.join(install_root, "Kokoro")
            env = installer.get_kokoro_runtime_env(install_root, kokoro_repo)

        self.assertEqual(
            env["PYTHONPATH"],
            os.pathsep.join([kokoro_repo, os.path.join(kokoro_repo, "api")]),
        )

    def test_kokoro_cpu_uses_explicit_pytorch_cpu_index(self):
        installer = HeadlessInstaller(working_dir="workspace")
        torch_spec, index_url = installer.get_kokoro_torch_install_options(use_gpu=False)

        self.assertEqual(torch_spec, "torch==2.8.0")
        self.assertEqual(index_url, "https://download.pytorch.org/whl/cpu")

    def test_bundled_pyopenjtalk_wheel_skips_windows_wheel_on_linux(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            wheels_dir = os.path.join(install_root, "Pandrator", "vendor", "wheels")
            os.makedirs(wheels_dir)
            with open(
                os.path.join(wheels_dir, "pyopenjtalk-0.4.1-cp311-cp311-win_amd64.whl"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write("")

            with patch("pandrator_installer.components.os.name", "posix"):
                with patch("pandrator_installer.components.platform.system", return_value="Linux"):
                    wheel_path, wheel_directory = installer.find_bundled_pyopenjtalk_wheel(install_root)

        self.assertEqual(wheel_path, "")
        self.assertEqual(wheel_directory, "")

    def test_bundled_pyopenjtalk_wheel_accepts_linux_wheel_on_linux(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            wheels_dir = os.path.join(install_root, "Pandrator", "vendor", "wheels")
            os.makedirs(wheels_dir)
            wheel_name = "pyopenjtalk-0.4.1-cp311-cp311-manylinux_2_28_x86_64.whl"
            wheel_path = os.path.join(wheels_dir, wheel_name)
            with open(wheel_path, "w", encoding="utf-8") as f:
                f.write("")

            with patch("pandrator_installer.components.os.name", "posix"):
                with patch("pandrator_installer.components.platform.system", return_value="Linux"):
                    resolved_path, wheel_directory = installer.find_bundled_pyopenjtalk_wheel(install_root)

        self.assertEqual(resolved_path, wheel_path)
        self.assertEqual(wheel_directory, wheels_dir)

    def test_linux_deferred_components_do_not_include_kokoro(self):
        self.assertNotIn("kokoro", LINUX_DEFERRED_INSTALL_COMPONENT_KEYS)
        self.assertNotIn("kokoro_cpu", LINUX_DEFERRED_INSTALL_COMPONENT_KEYS)
        self.assertIn("chatterbox", LINUX_DEFERRED_INSTALL_COMPONENT_KEYS)

    def test_non_windows_espeak_paths_use_host_environment(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as temp_dir:
            library_path = os.path.join(temp_dir, "libespeak-ng.so.1")
            data_path = os.path.join(temp_dir, "espeak-ng-data")
            with open(library_path, "w", encoding="utf-8") as f:
                f.write("")
            os.makedirs(data_path)

            with patch("pandrator_installer.operations.os.name", "posix"):
                with patch.dict(
                    os.environ,
                    {
                        "PHONEMIZER_ESPEAK_LIBRARY": library_path,
                        "ESPEAK_DATA_PATH": data_path,
                    },
                    clear=False,
                ):
                    resolved_library, resolved_data = installer.resolve_espeak_paths()

        self.assertEqual(resolved_library, library_path)
        self.assertEqual(resolved_data, data_path)

    def test_non_windows_espeak_setup_does_not_require_system_install(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with patch("pandrator_installer.operations.os.name", "posix"):
            with patch.object(installer, "resolve_espeak_paths", return_value=("", "")):
                self.assertTrue(installer.install_espeak_ng_direct("install-root"))

    def test_headless_entry_imports_without_pyqt(self):
        command = [
            sys.executable,
            "-c",
            (
                "import sys; import pandrator_installer_launcher; "
                "assert not any(name == 'PyQt6' or name.startswith('PyQt6.') for name in sys.modules)"
            ),
        ]
        subprocess.run(command, check=True)

    def test_catalog_drives_packaging_metadata(self):
        self.assertIn("chatterbox", COMPONENTS)
        self.assertEqual(COMPONENTS["rvc"].repo_dirname, "rvc-python")
        self.assertEqual(COMPONENTS["rvc_cpu"].variant_of, "rvc")
        self.assertEqual(COMPONENTS["rvc"].port, 8050)
        self.assertEqual(
            PACKAGING_COMPONENT_PATHS["chatterbox"],
            COMPONENTS["chatterbox"].paths,
        )
        self.assertIn("chatterbox_support", PACKAGING_CONFIG_FLAGS)
        self.assertEqual(NEMO_PYNINI_CONDA_SPEC, "pynini=2.1.6.post1")
        self.assertEqual(PANDRATOR_NUMPY_SPEC, "numpy==1.26.4")

    def test_self_check_cli_flag_and_execution(self):
        args = parse_launcher_cli_args(["--self-check"])
        self.assertTrue(args.self_check)
        with patch("builtins.print") as mock_print:
            self.assertEqual(run_self_check(), 0)
        printed = mock_print.call_args.args[0]
        self.assertIn("component definitions", printed)
        self.assertIn("pixi=", printed)
        self.assertIn("manifest=", printed)

    def test_gui_smoke_check_cli_flag(self):
        args = parse_launcher_cli_args(["--gui-smoke-check"])
        self.assertTrue(args.gui_smoke_check)


if __name__ == "__main__":
    unittest.main()
