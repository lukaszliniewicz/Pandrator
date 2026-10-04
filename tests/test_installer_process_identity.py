"""Process doubles verify that malformed identities cannot authorize signals."""

import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from pandrator_installer.process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    ProcessIdentityMismatch,
    ProcessInspectionError,
    identity_from_mapping,
    validated_process,
)
from pandrator_installer.service import HeadlessInstaller


class InstallerProcessIdentityTests(unittest.TestCase):
    def test_nonfinite_metadata_creation_times_are_rejected_before_inspection(self):
        with patch("pandrator_installer.process_identity.psutil.Process") as process:
            for value in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf")):
                with self.subTest(value=value), self.assertRaises(ProcessIdentityError):
                    identity_from_mapping(
                        {"pid": 999999, "process_create_time": value, "executable": "/fake/python"}
                    )
            process.assert_not_called()

    def test_infinite_pid_is_reported_as_invalid_identity(self):
        with self.assertRaises(ProcessIdentityError):
            identity_from_mapping(
                {"pid": float("inf"), "process_create_time": 100.0, "executable": "/fake/python"}
            )

    def test_nonfinite_direct_or_observed_identity_never_matches(self):
        for recorded, observed in (
            (float("nan"), 100.0),
            (float("inf"), float("inf")),
            (100.0, float("nan")),
            (100.0, float("inf")),
        ):
            with self.subTest(recorded=recorded, observed=observed):
                process = Mock()
                process.create_time.return_value = observed
                process.exe.return_value = "/fake/python"
                with patch(
                    "pandrator_installer.process_identity.psutil.Process", return_value=process
                ):
                    with self.assertRaises(ProcessIdentityMismatch):
                        validated_process(ProcessIdentity(999999, recorded, "/fake/python"))
                process.terminate.assert_not_called()
                process.kill.assert_not_called()

    def test_finite_identity_preserves_normalized_path_and_time_tolerance(self):
        process = Mock()
        process.create_time.return_value = 100.005
        process.exe.return_value = "/fake/python"
        identity = identity_from_mapping(
            {"pid": "999999", "process_create_time": "100", "executable": "/fake/./python"}
        )
        with patch("pandrator_installer.process_identity.psutil.Process", return_value=process):
            self.assertIs(validated_process(identity), process)
            process.create_time.return_value = 100.02
            with self.assertRaises(ProcessIdentityMismatch):
                validated_process(identity)

    def test_disappearance_and_inspection_denial_keep_distinct_results(self):
        identity = ProcessIdentity(999999, 100.0, "/fake/python")
        with patch(
            "pandrator_installer.process_identity.psutil.Process",
            side_effect=psutil.NoSuchProcess(999999),
        ):
            self.assertIsNone(validated_process(identity))
        with patch(
            "pandrator_installer.process_identity.psutil.Process",
            side_effect=psutil.AccessDenied(999999),
        ):
            with self.assertRaises(ProcessInspectionError):
                validated_process(identity)

    def test_invalid_candidate_prevents_signalling_the_whole_batch(self):
        process = Mock()
        process.pid = 999998
        process.create_time.return_value = 100.0
        process.exe.return_value = "/fake/python"
        process.children.return_value = []
        candidates = [
            {"pid": 999998, "name": "python", "create_time": 100.0, "exe": "/fake/python"},
            {"pid": 999999, "name": "python", "create_time": "NaN", "exe": "/fake/python"},
        ]
        with tempfile.TemporaryDirectory() as root:
            installer = HeadlessInstaller(working_dir=root)
            with (
                patch.object(installer, "_protected_installer_process_ids", return_value=set()),
                patch("pandrator_installer.process_identity.psutil.Process", return_value=process),
                patch("pandrator_installer.components.psutil.wait_procs") as wait,
                patch.object(installer, "_remove_stale_runtime_metadata") as cleanup,
            ):
                with self.assertRaisesRegex(RuntimeError, "Refusing to stop process metadata"):
                    installer.stop_running_installation_processes(root, candidates)
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                wait.assert_not_called()
                cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
