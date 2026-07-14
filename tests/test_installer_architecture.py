import unittest
import inspect
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
from pandrator_installer.cli import run_tls_self_check
from pandrator_installer.build_support import resolve_openssl_runtime_pair
from pandrator_installer.models import (
    InstallSelection,
    LaunchSelection,
    WorkspacePaths,
    qwen_effective_model_size,
    qwen_model_variants,
)
from pandrator_installer import platforms
from pandrator_installer.crispasr import detect_compute_backends, resolve_asset
from pandrator_installer.reporting import HeadlessReporter
from pandrator_installer.service import HeadlessInstaller
from pandrator_installer.workflows import WorkflowMixin
from pandrator_installer.constants import (
    NEMO_PYNINI_CONDA_SPEC,
    ONNX_ASR_INSTALL_SPEC,
    PANDRATOR_NUMPY_SPEC,
    PANDRATOR_REPO_BRANCH,
    PANDRATOR_URL_DOWNLOADER_CONDA_SPEC,
)


class InstallerArchitectureTests(unittest.TestCase):
    def test_silero_install_downloads_the_complete_catalogue(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = root / "silero-fastapi"
            service.mkdir()
            (service / "pyproject.toml").touch()
            (service / "pixi.lock").touch()
            with patch.object(installer, "run_command") as run_command:
                installer.install_silero_api_server(
                    str(service),
                    pandrator_path=str(root),
                    pixi_path="pixi",
                )

        self.assertEqual(run_command.call_count, 2)
        self.assertEqual(run_command.call_args_list[1].args[0][-1], "download-all")

    def test_pandrator_environment_installs_url_downloader_with_pixi(self):
        installer = HeadlessInstaller(working_dir="workspace")

        with patch.object(installer, "add_pixi_conda_package") as add_package:
            installer.ensure_pandrator_environment_conda_packages("C:/Pandrator")

        self.assertEqual(
            [call.args[2] for call in add_package.call_args_list],
            ["ffmpeg", PANDRATOR_URL_DOWNLOADER_CONDA_SPEC, NEMO_PYNINI_CONDA_SPEC],
        )
        self.assertIn(
            "yt-dlp",
            installer.get_optional_requirement_exclusions("pandrator_installer"),
        )

    def test_installer_does_not_manage_legacy_pycroppdf(self):
        install_source = inspect.getsource(WorkflowMixin.install_process)
        update_source = inspect.getsource(WorkflowMixin.update_process)

        self.assertNotIn("PyCropPDF", install_source)
        self.assertNotIn("PyCropPDF", update_source)
        self.assertFalse(hasattr(HeadlessInstaller, "install_pycroppdf_requirements"))

    def test_crispasr_auto_prefers_cuda_then_vulkan(self):
        cuda_asset, cuda_backend = resolve_asset(
            "auto",
            system="Windows",
            machine="AMD64",
            detected={
                "cuda": {"available": True},
                "metal": {"available": False},
                "vulkan": {"available": True},
                "cpu": {"available": True},
            },
        )
        vulkan_asset, vulkan_backend = resolve_asset(
            "auto",
            system="Windows",
            machine="AMD64",
            detected={
                "cuda": {"available": False},
                "metal": {"available": False},
                "vulkan": {"available": True},
                "cpu": {"available": True},
            },
        )

        self.assertEqual(cuda_backend, "cuda")
        self.assertIn("cuda", cuda_asset.compiled_backends)
        self.assertEqual(vulkan_backend, "vulkan")
        self.assertIn("vulkan", vulkan_asset.compiled_backends)

    def test_crispasr_backend_detection_supports_rx480_via_vulkan_loader(self):
        statuses = detect_compute_backends(
            system="Windows",
            machine="AMD64",
            environ={"SystemRoot": r"C:\Windows"},
            find_executable=lambda _name: None,
            path_exists=lambda path: str(path).lower().endswith("vulkan-1.dll"),
            find_library=lambda _name: None,
        )

        self.assertTrue(statuses["vulkan"]["available"])
        self.assertFalse(statuses["cuda"]["available"])

    def test_crispasr_apple_silicon_auto_selects_metal(self):
        asset, backend = resolve_asset(
            "auto",
            system="Darwin",
            machine="arm64",
            detected={
                "cuda": {"available": False},
                "metal": {"available": True},
                "vulkan": {"available": False},
                "cpu": {"available": True},
            },
        )

        self.assertEqual(backend, "metal")
        self.assertEqual(asset.runtime_variant, "metal")

    def test_release_branch_is_main(self):
        self.assertEqual(PANDRATOR_REPO_BRANCH, "main")

    def test_clone_repo_checks_out_requested_branch(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as workspace:
            target = os.path.join(workspace, "checkout")
            with patch.object(installer, "configure_tls_certificates"), \
                 patch.object(installer, "run_git_command") as run_git, \
                 patch.object(installer, "pull_repo"):
                installer.clone_repo(
                    "https://example.invalid/Pandrator.git",
                    target,
                    branch=PANDRATOR_REPO_BRANCH,
                )

        self.assertEqual(
            run_git.call_args_list[0].args[0],
            [
                "clone",
                "--branch",
                PANDRATOR_REPO_BRANCH,
                "--single-branch",
                "https://example.invalid/Pandrator.git",
                target,
            ],
        )

    def test_install_selection_resolves_dependencies(self):
        selection = InstallSelection.from_components(["xtts_finetuning"])
        self.assertEqual(
            set(selection.selected_components()),
            {"xtts", "xtts_finetuning"},
        )

    def test_install_selection_accepts_parakeet_onnx_component(self):
        selection = InstallSelection.from_components(["parakeet-onnx"])

        self.assertEqual(selection.selected_components(), ("crispasr",))

    def test_install_selection_rejects_mutually_exclusive_variants(self):
        with self.assertRaisesRegex(ValueError, "Select either 'kokoro' or 'kokoro_cpu'"):
            InstallSelection.from_components(["kokoro", "kokoro_cpu"])
        with self.assertRaisesRegex(ValueError, "Select either 'rvc' or 'rvc_cpu'"):
            InstallSelection.from_components(["rvc", "rvc_cpu"])
        with self.assertRaisesRegex(ValueError, "Select either 'kobold_qwen' or 'kobold_qwen_cpu'"):
            InstallSelection.from_components(["kobold_qwen", "kobold_qwen_cpu"])

    def test_qwen_defaults_to_fp16_and_supports_both_1_7b_variants(self):
        default_selection = InstallSelection.from_components(["kobold_qwen"])
        both_selection = InstallSelection.from_components(
            ["kobold_qwen"],
            kobold_qwen_model_size="1.7b",
            kobold_qwen_initial_model="both",
        )

        self.assertEqual(default_selection.kobold_qwen_quantization, "f16")
        self.assertEqual(
            qwen_model_variants(both_selection.kobold_qwen_initial_model),
            ("base", "customvoice"),
        )
        self.assertEqual(qwen_effective_model_size("both", "0.6b"), "1.7b")
        self.assertEqual(qwen_effective_model_size("customvoice", "0.6b"), "1.7b")
        self.assertEqual(qwen_effective_model_size("base", "0.6b"), "0.6b")
        with self.assertRaisesRegex(ValueError, "only for the 1.7B"):
            InstallSelection.from_components(
                ["kobold_qwen"],
                kobold_qwen_model_size="0.6b",
                kobold_qwen_initial_model="both",
            )

        cli_defaults = parse_launcher_cli_args([])
        cli_both = parse_launcher_cli_args(
            ["--qwen-model-size", "1.7b", "--qwen-initial-model", "both"]
        )
        self.assertEqual(cli_defaults.qwen_quantization, "f16")
        self.assertEqual(cli_both.qwen_initial_model, "both")

    def test_external_subprocess_environment_restores_pre_bundle_library_path(self):
        installer = HeadlessInstaller(working_dir="workspace")
        bundle_environment = {
            "LD_LIBRARY_PATH": "/tmp/appimage/_internal:/usr/local/lib",
            "LD_LIBRARY_PATH_ORIG": "/usr/local/lib",
        }

        with patch("pandrator_installer.operations.sys.platform", "linux"):
            restored = installer.get_external_subprocess_env(bundle_environment)

        self.assertEqual(restored["LD_LIBRARY_PATH"], "/usr/local/lib")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", restored)

    def test_appimage_openssl_pair_supports_lib64_and_rejects_split_pairs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lib = root / "lib"
            lib64 = root / "lib64"
            lib.mkdir()
            lib64.mkdir()
            (lib64 / "libssl.so.3").write_bytes(b"ssl")
            (lib64 / "libcrypto.so.3").write_bytes(b"crypto")

            ssl_library, crypto_library = resolve_openssl_runtime_pair((lib, lib64))

            self.assertEqual(ssl_library.parent, lib64)
            self.assertEqual(crypto_library.parent, lib64)

            (lib64 / "libcrypto.so.3").unlink()
            (lib / "libcrypto.so.3").write_bytes(b"different-runtime")
            with self.assertRaisesRegex(RuntimeError, "matched libssl.so.3/libcrypto.so.3 pair"):
                resolve_openssl_runtime_pair((lib, lib64))

    def test_launch_selection_preserves_backend_priority(self):
        selection = LaunchSelection(voxcpm=True, chatterbox=True, rvc=True)
        self.assertEqual(selection.selected_backend_keys(), ("voxcpm", "chatterbox"))
        self.assertTrue(selection.rvc)

    def test_workspace_paths_are_rooted_under_workspace(self):
        paths = WorkspacePaths.from_value(Path("workspace"))
        self.assertEqual(paths.install_root.name, "Pandrator")
        self.assertEqual(paths.pandrator_repo.name, "Pandrator")
        self.assertFalse(hasattr(paths, "subdub_repo"))

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
        self.assertEqual(env["HF_HUB_DISABLE_XET"], "1")
        self.assertTrue(env["PADDLE_PDX_CACHE_HOME"].endswith(os.path.join("cache", "paddlex")))
        self.assertEqual(env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"], "True")

    def test_installer_environment_respects_explicit_xet_opt_in(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            with patch.dict(os.environ, {"HF_HUB_DISABLE_XET": "0"}):
                env = installer.get_pixi_subprocess_env(install_root)

        self.assertEqual(env["HF_HUB_DISABLE_XET"], "0")

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

    def test_package_cache_cleanup_handles_missing_cache_root(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as install_root:
            installer.cleanup_installer_package_caches(install_root)

            pixi_cache = os.path.join(install_root, ".pixi-cache")
            self.assertTrue(os.path.isdir(os.path.join(pixi_cache, "pip")))
            self.assertTrue(os.path.isdir(os.path.join(pixi_cache, "rattler")))
            self.assertTrue(os.path.isdir(os.path.join(pixi_cache, "tmp")))

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

    def test_launcher_workspace_infers_installed_repo_cwd(self):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = Path(workspace) / "Pandrator"
            repo_root = install_root / "Pandrator"
            repo_root.mkdir(parents=True)
            (install_root / "config.json").write_text("{}", encoding="utf-8")
            (repo_root / "pyproject.toml").write_text("", encoding="utf-8")
            web_index = repo_root / "pandrator" / "web" / "static" / "index.html"
            web_index.parent.mkdir(parents=True)
            web_index.write_text("", encoding="utf-8")

            resolved = platforms.resolve_launcher_workspace(
                system="Windows",
                environ={},
                cwd=str(repo_root),
            )

        self.assertEqual(resolved, os.path.abspath(workspace))

    def test_launcher_workspace_infers_install_root_cwd(self):
        with tempfile.TemporaryDirectory() as workspace:
            install_root = Path(workspace) / "Pandrator"
            repo_root = install_root / "Pandrator"
            repo_root.mkdir(parents=True)
            (install_root / "config.json").write_text("{}", encoding="utf-8")
            (repo_root / "pyproject.toml").write_text("", encoding="utf-8")
            web_index = repo_root / "pandrator" / "web" / "static" / "index.html"
            web_index.parent.mkdir(parents=True)
            web_index.write_text("", encoding="utf-8")

            resolved = platforms.resolve_launcher_workspace(
                system="Windows",
                environ={},
                cwd=str(install_root),
            )

        self.assertEqual(resolved, os.path.abspath(workspace))

    def test_launcher_workspace_keeps_uninstalled_repo_cwd(self):
        with tempfile.TemporaryDirectory() as repo_root:
            Path(repo_root, "pyproject.toml").write_text("", encoding="utf-8")

            resolved = platforms.resolve_launcher_workspace(
                system="Windows",
                environ={},
                cwd=repo_root,
            )

        self.assertEqual(resolved, os.path.abspath(repo_root))

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

    def test_non_windows_first_party_silero_install_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["silero"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_magpie_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["magpie_cpu"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_chatterbox_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["chatterbox_cpu"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_kokoro_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["kokoro_cpu"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_kobold_qwen_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components(["kobold_qwen_cpu"])

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_core_install_selection_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        selection = InstallSelection.from_components([], install_pandrator=True)

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_install_selection(selection)

    def test_non_windows_update_with_first_party_silero_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"silero_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_update_config(config)

    def test_non_windows_update_with_magpie_config_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"magpie_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_update_config(config)

    def test_non_windows_update_with_chatterbox_config_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"chatterbox_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_update_config(config)

    def test_non_windows_update_with_kokoro_config_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"kokoro_support": True}

        with patch("pandrator_installer.workflows.is_windows", return_value=False):
            installer.validate_platform_update_config(config)

    def test_non_windows_update_with_kobold_qwen_config_is_allowed(self):
        installer = HeadlessInstaller(working_dir="workspace")
        config = {"kobold_qwen_support": True}

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

    @unittest.skipIf(os.name == "nt", "The short eSpeak symlink is a Linux/macOS runtime workaround.")
    def test_kokoro_uses_short_site_packages_alias_for_deep_linux_install(self):
        installer = HeadlessInstaller(working_dir="workspace")
        with tempfile.TemporaryDirectory() as directory:
            install_root = os.path.join(directory, "nested-" + "x" * 90)
            manifest = Path(installer.get_pixi_manifest_path(install_root, "kokoro_api_server_installer"))
            site_packages = manifest.parent / ".pixi" / "envs" / "default" / "lib" / "python3.11" / "site-packages"
            data = site_packages / "espeakng_loader" / "espeak-ng-data"
            data.mkdir(parents=True)
            (data / "phontab").write_bytes(b"fixture")
            kokoro_repo = os.path.join(install_root, "Kokoro")

            with patch("pandrator_installer.components.os.name", "posix"):
                env = installer.get_kokoro_runtime_env(install_root, kokoro_repo)

            first_path = Path(env["PYTHONPATH"].split(os.pathsep)[0])
            self.assertTrue((first_path / "espeakng_loader" / "espeak-ng-data" / "phontab").is_file())

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

    def test_linux_deferred_components_only_contain_unqualified_tools(self):
        for component in (
            "xtts",
            "xtts_cpu",
            "voxcpm",
            "fishs2",
            "fishs2_cpu",
            "voxtral",
            "kokoro",
            "kokoro_cpu",
            "silero",
            "chatterbox",
            "chatterbox_cpu",
            "kobold_qwen",
            "kobold_qwen_cpu",
            "magpie",
            "magpie_cpu",
            "rvc",
            "rvc_cpu",
        ):
            self.assertNotIn(component, LINUX_DEFERRED_INSTALL_COMPONENT_KEYS)

        self.assertIn("xtts_finetuning", LINUX_DEFERRED_INSTALL_COMPONENT_KEYS)

    def test_xtts_and_voxcpm_launchers_use_cross_platform_python_bootstrappers(self):
        installer = HeadlessInstaller(working_dir="workspace")

        with patch("pandrator_installer.components.is_windows", return_value=False):
            xtts_command = installer.build_xtts_launcher_command(
                pixi_path="/home/user/Pandrator/bin/pixi",
            )
            voxcpm_command = installer.build_voxcpm_launcher_command(
                pixi_path="/home/user/Pandrator/bin/pixi",
            )

        self.assertEqual(
            xtts_command,
            [
                "/home/user/Pandrator/bin/pixi",
                "run",
                "python",
                "run.py",
                "--backend",
                "auto",
                "--pixi-path",
                "/home/user/Pandrator/bin/pixi",
            ],
        )
        self.assertEqual(
            voxcpm_command,
            [
                "/home/user/Pandrator/bin/pixi",
                "run",
                "python",
                "run.py",
                "--pixi-path",
                "/home/user/Pandrator/bin/pixi",
            ],
        )

    def test_magpie_launcher_uses_cross_platform_python_bootstrapper(self):
        installer = HeadlessInstaller(working_dir="workspace")

        with patch("pandrator_installer.components.is_windows", return_value=False):
            command = installer.build_magpie_launcher_command(
                use_cpu=True,
                pixi_path="/home/user/Pandrator/bin/pixi",
            )

        self.assertEqual(
            command,
            [
                "/home/user/Pandrator/bin/pixi",
                "run",
                "python",
                "run.py",
                "--device",
                "cpu",
                "--pixi-path",
                "/home/user/Pandrator/bin/pixi",
            ],
        )

    def test_silero_launcher_uses_locked_repo_and_persistent_model_directory(self):
        installer = HeadlessInstaller(working_dir="workspace")
        command = installer.build_silero_launcher_command(
            "/srv/Pandrator",
            pixi_path="/srv/Pandrator/bin/pixi",
        )

        self.assertEqual(
            command,
            [
                "/srv/Pandrator/bin/pixi",
                "run",
                "--locked",
                "silero-fastapi",
                "--data-dir",
                os.path.join("/srv/Pandrator", "models", "silero"),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--device",
                "cpu",
            ],
        )

    def test_voxtral_launcher_uses_linux_shell_bootstrapper(self):
        installer = HeadlessInstaller(working_dir="workspace")

        with patch("pandrator_installer.components.is_windows", return_value=False):
            run_command = installer.build_voxtral_launcher_command("/srv/voxtral")
            prepare_command = installer.build_voxtral_launcher_command(
                "/srv/voxtral",
                prepare_only=True,
            )

        self.assertEqual(run_command[0], "bash")
        self.assertEqual(run_command[1].replace("\\", "/"), "/srv/voxtral/run.sh")
        self.assertEqual(
            run_command[2:],
            [
                "--project-root",
                "/srv/voxtral",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--model",
                "gguf",
            ],
        )
        self.assertEqual(prepare_command[-1], "--no-start")

    def test_chatterbox_launcher_uses_platform_launcher(self):
        installer = HeadlessInstaller(working_dir="workspace")

        with patch("pandrator_installer.components.is_windows", return_value=True):
            windows_command = installer.build_chatterbox_launcher_command(
                use_cpu=True,
                pixi_path="C:/Pandrator/bin/pixi.exe",
            )

        with patch("pandrator_installer.components.is_windows", return_value=False):
            linux_command = installer.build_chatterbox_launcher_command(
                use_cpu=True,
                pixi_path="/home/user/Pandrator/bin/pixi",
            )

        self.assertEqual(windows_command[:4], ["cmd", "/c", "run.bat", "--backend"])
        self.assertIn("cpu", windows_command)
        self.assertIn("--pixi-path", windows_command)
        self.assertEqual(
            linux_command,
            ["/home/user/Pandrator/bin/pixi", "run", "python", "run.py", "--backend", "cpu"],
        )

    def test_kobold_qwen_launcher_uses_auto_backend_and_port(self):
        installer = HeadlessInstaller(working_dir="workspace")

        with patch("pandrator_installer.components.is_windows", return_value=True):
            windows_command = installer.build_kobold_qwen_launcher_command(
                use_cpu=False,
                pixi_path="C:/Pandrator/bin/pixi.exe",
            )

        with patch("pandrator_installer.components.is_windows", return_value=False):
            linux_command = installer.build_kobold_qwen_launcher_command(
                use_cpu=True,
                pixi_path="/home/user/Pandrator/bin/pixi",
            )

        self.assertEqual(windows_command[:3], ["cmd", "/c", "run.bat"])
        self.assertIn("--backend", windows_command)
        self.assertIn("auto", windows_command)
        self.assertIn("--port", windows_command)
        self.assertIn("8042", windows_command)
        self.assertIn("--pixi-path", windows_command)
        self.assertEqual(
            linux_command,
            [
                "/home/user/Pandrator/bin/pixi",
                "run",
                "python",
                "run.py",
                "--backend",
                "cpu",
                "--port",
                "8042",
                "--model-size",
                "0.6b",
                "--quantization",
                "f16",
                "--initial-model",
                "base",
            ],
        )

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
        self.assertEqual(
            PACKAGING_COMPONENT_PATHS["crispasr"],
            COMPONENTS["crispasr"].paths,
        )
        self.assertIn("chatterbox_support", PACKAGING_CONFIG_FLAGS)
        self.assertIn("crispasr_support", PACKAGING_CONFIG_FLAGS)
        self.assertEqual(NEMO_PYNINI_CONDA_SPEC, "pynini=2.1.6.post1")
        self.assertEqual(PANDRATOR_NUMPY_SPEC, "numpy==1.26.4")
        self.assertEqual(ONNX_ASR_INSTALL_SPEC, "onnx-asr[cpu,hub]==0.11.0")

    def test_self_check_cli_flag_and_execution(self):
        args = parse_launcher_cli_args(["--self-check"])
        self.assertTrue(args.self_check)
        with patch("builtins.print") as mock_print:
            self.assertEqual(run_self_check(), 0)
        printed = mock_print.call_args.args[0]
        self.assertIn("component definitions", printed)
        self.assertIn("pixi=", printed)
        self.assertIn("manifest=", printed)
        self.assertIn("openssl=", printed)

    def test_tls_self_check_uses_certifi_and_a_head_request(self):
        class Response:
            status = 204

            def getcode(self):
                return self.status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            with patch("builtins.print") as printed:
                self.assertEqual(run_tls_self_check("https://example.test/health"), 0)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/health")
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 20)
        self.assertIn("TLS self-check passed", printed.call_args.args[0])

    def test_tls_self_check_cli_flag(self):
        args = parse_launcher_cli_args(["--tls-self-check"])
        self.assertTrue(args.tls_self_check)

    def test_gui_smoke_check_cli_flag(self):
        args = parse_launcher_cli_args(["--gui-smoke-check"])
        self.assertTrue(args.gui_smoke_check)


if __name__ == "__main__":
    unittest.main()
