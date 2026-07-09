import json
import unittest

from pandrator.logic.state_db_handler import merge_dubbing_llm_usage


class DubbingRunUsageTests(unittest.TestCase):
    def test_merge_dubbing_llm_usage_accumulates_stage_and_run_totals(self):
        first = merge_dubbing_llm_usage(
            None,
            "correct",
            cost=0.025,
            response_count=1,
            metadata={"model": "claude-sonnet-4-6"},
        )
        second = merge_dubbing_llm_usage(
            first["usage"],
            "translate",
            cost=0.05,
            response_count=2,
        )
        third = merge_dubbing_llm_usage(
            json.dumps(second["usage"]),
            "correct",
            cost=0.01,
            response_count=1,
        )

        self.assertAlmostEqual(third["total_cost"], 0.085)
        self.assertEqual(third["response_count"], 4)
        self.assertAlmostEqual(third["usage"]["correct"]["cost"], 0.035)
        self.assertEqual(third["usage"]["correct"]["response_count"], 2)
        self.assertEqual(third["usage"]["correct"]["events"][0]["metadata"]["model"], "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
