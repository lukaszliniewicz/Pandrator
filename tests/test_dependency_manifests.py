import unittest

from scripts.generate_requirements import TARGETS, _dependencies, _render


class DependencyManifestTests(unittest.TestCase):
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
