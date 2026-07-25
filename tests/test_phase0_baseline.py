import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PhaseZeroBaselineTests(unittest.TestCase):
    def test_small_disposable_baseline_writes_structured_json(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "baseline.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "phase0_baseline.py"),
                    "--output",
                    str(output),
                    "--claim-trials",
                    "1",
                    "--claim-contenders",
                    "2",
                    "--resource-trials",
                    "1",
                    "--history-artifacts",
                    "5",
                    "--history-jobs",
                    "5",
                    "--assembly-segments",
                    "2",
                    "--audio-segments",
                    "2",
                    "--skip-capabilities",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["schema_version"])
            observations = payload["observations"]
            self.assertEqual(
                {
                    "job_claim",
                    "resource_acquisition",
                    "workflow_snapshot",
                    "generation_assembly",
                    "audio_composition",
                },
                set(observations),
            )
            self.assertEqual(2, observations["job_claim"]["contenders"])
            self.assertEqual(2, observations["generation_assembly"]["fixture"]["segments"])
            self.assertIn("target_budgets", payload)


if __name__ == "__main__":
    unittest.main()
