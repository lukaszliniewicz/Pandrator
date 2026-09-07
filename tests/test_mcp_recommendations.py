import unittest
from types import SimpleNamespace

from pandrator_mcp.schemas import RecommendNextStepsInput
from pandrator_mcp.tools.recommendations import recommend_next_steps


class _Application:
    def __init__(self, plan):
        self.plan = plan

    def get_session(self, session_id):
        return {
            "id": session_id,
            "name": "Edit",
            "workflow_kind": "media_edit",
            "status": "idle",
            "revision": 1,
        }

    def get_workflow(self, _session_id):
        return {"stages": []}

    def get_media_edit(self, _session_id):
        return {"plan": self.plan}


class RecommendMediaEditTests(unittest.TestCase):
    def _recommend(self, plan):
        application = _Application(plan)
        runtime = SimpleNamespace(require_application=lambda: application)
        return recommend_next_steps(
            runtime,
            RecommendNextStepsInput(
                session_id="session-1",
                goal="Trim video setup chatter",
            ),
        )

    def test_ready_plan_recommendation_is_executable(self):
        result = self._recommend({"revision": 4})
        step = next(
            item
            for item in result["steps"]
            if item["tool"] == "pandrator_create_media_edit_dispatch_run"
        )
        self.assertEqual(4, step["arguments"]["revision"])
        self.assertEqual("Trim video setup chatter", step["arguments"]["instructions"])
        self.assertGreaterEqual(len(step["arguments"]["idempotency_key"]), 8)

    def test_unprepared_plan_recommends_prepare_then_inspect(self):
        result = self._recommend(None)
        tools = [item["tool"] for item in result["steps"]]
        prepare_index = tools.index("pandrator_prepare_media_edit")
        inspect_index = tools.index("pandrator_get_media_edit")
        self.assertLess(prepare_index, inspect_index)
        self.assertNotIn("pandrator_create_media_edit_dispatch_run", tools)


if __name__ == "__main__":
    unittest.main()
