import tomllib
import unittest
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

from pandrator.version import PANDRATOR_VERSION, resolve_application_version


class ApplicationVersionTests(unittest.TestCase):
    def test_runtime_version_matches_the_checkout_metadata(self):
        root = Path(__file__).resolve().parents[1]
        expected = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]

        self.assertEqual(expected, PANDRATOR_VERSION)

    @patch("pandrator.version.package_version", return_value="9.9.9")
    @patch("pandrator.version._source_tree_version", return_value="1.2.3")
    def test_checkout_version_takes_precedence(self, _source, distribution):
        self.assertEqual("1.2.3", resolve_application_version())
        distribution.assert_not_called()

    @patch("pandrator.version.package_version", return_value="9.9.9")
    @patch("pandrator.version._source_tree_version", return_value=None)
    def test_installed_distribution_is_the_fallback(self, _source, _distribution):
        self.assertEqual("9.9.9", resolve_application_version())

    @patch("pandrator.version.package_version", side_effect=PackageNotFoundError)
    @patch("pandrator.version._source_tree_version", return_value=None)
    def test_missing_metadata_has_an_explicit_unknown_version(
        self, _source, _distribution
    ):
        self.assertEqual("0+unknown", resolve_application_version())


if __name__ == "__main__":
    unittest.main()
