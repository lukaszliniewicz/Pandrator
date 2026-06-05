import sys
import unittest
from pathlib import Path

# Add the repository root and scripts directory to sys.path to allow importing build_release_packages
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "scripts"))

import build_release_packages


class BuildReleasePackagesTests(unittest.TestCase):
    def test_resolve_dependencies_single_no_deps(self):
        # A component like kokoro has no dependencies
        resolved = build_release_packages.resolve_dependencies(["kokoro"])
        self.assertEqual(resolved, ("kokoro",))

    def test_resolve_dependencies_with_deps(self):
        # xtts_finetuning depends on whisperx and xtts
        resolved = build_release_packages.resolve_dependencies(["xtts_finetuning"])
        # Should be resolved in topological/dependency-first order
        self.assertEqual(resolved, ("whisperx", "xtts", "xtts_finetuning"))

    def test_resolve_dependencies_multiple_mixed(self):
        # Resolve mixed list including dependency overlap
        resolved = build_release_packages.resolve_dependencies(["kokoro", "xtts_finetuning", "whisperx"])
        # Order should be valid and unique: kokoro and whisperx/xtts dependencies resolved
        self.assertEqual(set(resolved), {"kokoro", "whisperx", "xtts", "xtts_finetuning"})

    def test_resolve_dependencies_unknown(self):
        with self.assertRaises(RuntimeError) as context:
            build_release_packages.resolve_dependencies(["unknown_engine"])
        self.assertIn("Unknown module", str(context.exception))

    def test_parse_selected_modules_all(self):
        # 'all' should resolve all modules
        resolved = build_release_packages.parse_selected_modules("all")
        self.assertEqual(set(resolved), set(build_release_packages.ALL_MODULE_KEYS))

    def test_parse_selected_modules_preset(self):
        # 'stack' preset maps to ('xtts_finetuning', 'rvc'), which expands to dependencies
        resolved = build_release_packages.parse_selected_modules("stack")
        self.assertEqual(set(resolved), {"whisperx", "xtts", "xtts_finetuning", "rvc"})

    def test_parse_selected_modules_custom_list(self):
        # Comma-separated list with mixed case and dashes
        resolved = build_release_packages.parse_selected_modules("Kokoro, RVC, whisperx")
        self.assertEqual(set(resolved), {"kokoro", "rvc", "whisperx"})

    def test_parse_selected_modules_kokoro_cpu(self):
        resolved = build_release_packages.parse_selected_modules("kokoro_cpu")
        self.assertEqual(resolved, ("kokoro_cpu",))

    def test_default_config_flags_include_kokoro_gpu_support(self):
        self.assertIn("kokoro_gpu_support", build_release_packages.DEFAULT_CONFIG_FLAGS)

    def test_parse_selected_modules_invalid(self):
        with self.assertRaises(RuntimeError):
            build_release_packages.parse_selected_modules("invalid_module")

    def test_get_tailored_workspace_name_stable(self):
        # Check that different orderings of the same component list result in the same workspace name
        name1 = build_release_packages.get_tailored_workspace_name(["kokoro", "rvc", "whisperx"])
        name2 = build_release_packages.get_tailored_workspace_name(["whisperx", "kokoro", "rvc"])
        self.assertEqual(name1, name2)
        self.assertTrue(name1.startswith("workspace_"))
        self.assertEqual(len(name1), 10 + 12)  # workspace_ + 12 hex chars

    def test_should_exclude_file(self):
        self.assertTrue(build_release_packages.should_exclude_file("__pycache__"))
        self.assertTrue(build_release_packages.should_exclude_file("main.pyc"))
        self.assertTrue(build_release_packages.should_exclude_file("test.log"))
        self.assertTrue(build_release_packages.should_exclude_file("pandrator_state.sqlite3"))
        self.assertTrue(build_release_packages.should_exclude_file("pandrator_state.sqlite3-journal"))
        self.assertFalse(build_release_packages.should_exclude_file("main.py"))
        self.assertFalse(build_release_packages.should_exclude_file("config.json"))


if __name__ == "__main__":
    unittest.main()
