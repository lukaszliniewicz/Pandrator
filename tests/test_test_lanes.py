import contextlib
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import test_lanes


class TestLaneManifestTests(unittest.TestCase):
    def test_manifest_covers_every_current_test_file_exactly_once(self):
        test_lanes.validate_manifest()

    def test_validation_reports_unknown_missing_and_duplicate_paths(self):
        manifest = {
            "first": ("tests/test_duplicate.py", "tests/test_unknown.py"),
            "second": ("tests/test_duplicate.py",),
        }
        with self.assertRaises(test_lanes.TestLaneManifestError) as raised:
            test_lanes.validate_manifest(
                manifest,
                discovered={"tests/test_duplicate.py", "tests/test_missing.py"},
            )

        message = str(raised.exception)
        self.assertIn("unknown paths: tests/test_unknown.py", message)
        self.assertIn("missing paths: tests/test_missing.py", message)
        self.assertIn(
            "duplicate paths: tests/test_duplicate.py (first, second)",
            message,
        )

    def test_payload_is_manifest_ordered_and_json_serializable(self):
        manifest = {
            "second": ("tests/test_second.py",),
            "first": ("tests/test_first.py",),
        }

        payload = test_lanes.lane_payload(manifest)

        self.assertEqual(
            payload,
            {
                "lanes": [
                    {"name": "second", "files": ["tests/test_second.py"]},
                    {"name": "first", "files": ["tests/test_first.py"]},
                ]
            },
        )
        self.assertEqual(json.loads(json.dumps(payload, sort_keys=True)), payload)

    def test_project_and_pixi_configuration_keep_lane_contracts_explicit(self):
        project = tomllib.loads(
            (test_lanes.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        dev_dependencies = project["project"]["optional-dependencies"]["dev"]
        self.assertIn("build>=1.2,<2", dev_dependencies)
        self.assertIn("pytest-xdist==3.8.0", dev_dependencies)
        pytest_options = project["tool"]["pytest"]["ini_options"]
        self.assertIn("--strict-markers", pytest_options["addopts"])

        pixi = tomllib.loads(
            (test_lanes.REPO_ROOT / "pixi.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pixi["environments"]["default"], ["dev"])
        tasks = pixi["tasks"]
        self.assertEqual(
            tasks["check-test-lanes"],
            "python scripts/test_lanes.py check",
        )
        self.assertEqual(
            tasks["test-fast"],
            {
                "cmd": "python scripts/test_lanes.py run fast-xdist",
                "depends-on": ["check-test-lanes"],
            },
        )
        for task_name in ("test-full", "test-profile"):
            with self.subTest(task=task_name):
                self.assertEqual(tasks[task_name]["depends-on"], ["check-test-lanes"])
                self.assertNotIn("-n", tasks[task_name]["cmd"])
        self.assertIn(
            "--durations=30 --durations-min=0.5",
            tasks["test-profile"]["cmd"],
        )


class TestLaneCommandTests(unittest.TestCase):
    def test_list_json_is_stable(self):
        outputs: list[str] = []
        for _ in range(2):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(0, test_lanes.main(["list", "--json"]))
            outputs.append(stream.getvalue())

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(test_lanes.lane_payload(), json.loads(outputs[0]))

    def test_serial_command_uses_explicit_deduplicated_files(self):
        command = test_lanes.build_pytest_command(
            ["installer-serial", "web-01-serial", "installer-serial"],
            Path("artifacts/results.xml"),
        )

        self.assertEqual(
            command[:6],
            [
                test_lanes.sys.executable,
                "-m",
                "pytest",
                "-q",
                "--junitxml",
                str(Path("artifacts/results.xml")),
            ],
        )
        self.assertEqual(
            command[6:],
            [
                *test_lanes.TEST_LANES["installer-serial"],
                *test_lanes.TEST_LANES["web-01-serial"],
            ],
        )
        self.assertNotIn("-n", command)
        self.assertNotIn("--dist=loadfile", command)

    def test_fast_command_adds_only_the_vetted_xdist_options(self):
        command = test_lanes.build_pytest_command(["fast-xdist"])

        self.assertEqual(
            command[:7],
            [
                test_lanes.sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                "2",
                "--dist=loadfile",
            ],
        )
        self.assertEqual(command[7:], list(test_lanes.TEST_LANES["fast-xdist"]))

    def test_fast_lane_cannot_share_an_invocation_with_serial_lanes(self):
        with self.assertRaisesRegex(
            test_lanes.TestLaneUsageError,
            "cannot be combined with serial test lanes",
        ):
            test_lanes.build_pytest_command(["fast-xdist", "installer-serial"])

    def test_unknown_lane_is_rejected_before_pytest_is_started(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(1, test_lanes.main(["run", "not-a-lane"]))

        self.assertIn("Unknown test lane: not-a-lane", stderr.getvalue())

    def test_run_propagates_pytest_exit_code_and_creates_junit_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            junitxml = Path(directory) / "nested" / "results.xml"
            with (
                patch.object(test_lanes, "validate_manifest") as validate,
                patch.object(
                    test_lanes.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=7),
                ) as run,
            ):
                self.assertEqual(
                    7,
                    test_lanes.run_lanes(["installer-serial"], junitxml),
                )

            validate.assert_called_once_with()
            self.assertTrue(junitxml.parent.is_dir())
            self.assertEqual(
                run.call_args.args[0],
                test_lanes.build_pytest_command(["installer-serial"], junitxml),
            )
            self.assertEqual(run.call_args.kwargs, {"check": False})


if __name__ == "__main__":
    unittest.main()
