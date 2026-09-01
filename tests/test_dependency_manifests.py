import tomllib
import unittest
from pathlib import Path

from scripts.generate_requirements import (
    TARGETS,
    _dependencies,
    _normalized_name,
    _render,
)


class DependencyManifestTests(unittest.TestCase):
    def test_default_pixi_runtime_installs_the_canonical_project(self):
        root = Path(__file__).resolve().parents[1]
        manifest = tomllib.loads(
            (root / "pixi.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            manifest["pypi-dependencies"]["pandrator"],
            {"path": ".", "editable": True, "extras": ["dev"]},
        )
        conda_dependencies = set(manifest["dependencies"])
        self.assertTrue(
            {"python", "ffmpeg"}.issubset(conda_dependencies),
        )
        project_dependencies = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"]
        duplicated_python_dependencies = conda_dependencies.intersection(
            _normalized_name(item) for item in project_dependencies
        )
        self.assertEqual(
            duplicated_python_dependencies,
            set(),
        )

    def test_project_distributions_use_the_canonical_mit_license(self):
        root = Path(__file__).resolve().parents[1]
        canonical = (root / "LICENSE").read_text(encoding="utf-8")
        manager = (root / "pandrator_manager" / "LICENSE").read_text(
            encoding="utf-8"
        )

        self.assertTrue(canonical.startswith("MIT License\n"))
        self.assertEqual(canonical, manager)
        for project in (root, root / "pandrator_manager"):
            with self.subTest(project=project.name):
                metadata = tomllib.loads(
                    (project / "pyproject.toml").read_text(encoding="utf-8")
                )["project"]
                self.assertEqual(metadata["license"], "MIT")
                self.assertEqual(metadata["license-files"], ["LICENSE"])

    def test_application_automation_extra_installs_the_managed_mcp_runtime(self):
        root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertEqual(
            ["pandrator-mcp[manager]>=0.3,<1"],
            metadata["optional-dependencies"]["automation"],
        )

    def test_requirements_are_current_projections_of_project_metadata(self):
        for destination, source, extras in TARGETS:
            with self.subTest(destination=destination.name):
                self.assertEqual(
                    _render(source, extras),
                    destination.read_text(encoding="utf-8"),
                )

    def test_generated_requirements_do_not_repeat_package_names(self):
        for destination, source, extras in TARGETS:
            with self.subTest(destination=destination.name):
                dependencies = _dependencies(source, extras)
                names = [
                    item.split(";", 1)[0]
                    .split("[", 1)[0]
                    .split("=", 1)[0]
                    .split("<", 1)[0]
                    .split(">", 1)[0]
                    .strip()
                    .lower()
                    .replace("_", "-")
                    for item in dependencies
                ]
                self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
