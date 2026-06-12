import unittest
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from pandrator_installer.catalog import (
    COMPONENTS,
    PACKAGING_COMPONENT_PATHS,
    PACKAGING_CONFIG_FLAGS,
)
from pandrator_installer.cli import parse_launcher_cli_args, run_self_check
from pandrator_installer.models import InstallSelection, LaunchSelection, WorkspacePaths
from pandrator_installer.reporting import HeadlessReporter
from pandrator_installer.service import HeadlessInstaller
from pandrator_installer.constants import NEMO_PYNINI_CONDA_SPEC


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
        self.assertEqual(COMPONENTS["rvc"].port, 8050)
        self.assertEqual(
            PACKAGING_COMPONENT_PATHS["chatterbox"],
            COMPONENTS["chatterbox"].paths,
        )
        self.assertIn("chatterbox_support", PACKAGING_CONFIG_FLAGS)
        self.assertEqual(NEMO_PYNINI_CONDA_SPEC, "pynini=2.1.6.post1")

    def test_self_check_cli_flag_and_execution(self):
        args = parse_launcher_cli_args(["--self-check"])
        self.assertTrue(args.self_check)
        with patch("builtins.print"):
            self.assertEqual(run_self_check(), 0)


if __name__ == "__main__":
    unittest.main()
