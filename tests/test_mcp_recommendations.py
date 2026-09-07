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
    def _recommend(self, plan, goal="Trim video setup chatter"):
        application = _Application(plan)
        runtime = SimpleNamespace(require_application=lambda: application)
        return recommend_next_steps(
            runtime,
            RecommendNextStepsInput(
                session_id="session-1",
                goal=goal,
            ),
        )

    def test_ready_plan_recommendation_is_executable(self):
        result = self._recommend({"revision": 4})
        step = next(
            item
            for item in result["steps"]
            if item["tool"] == "pandrator_plan_media_edit_workflow"
        )
        self.assertEqual("session-1", step["arguments"]["session_id"])
        self.assertEqual("Trim video setup chatter", step["arguments"]["instructions"])
        self.assertNotIn(
            "pandrator_plan_workflow", [item["tool"] for item in result["steps"]]
        )

    def test_unprepared_plan_recommends_prepare_then_inspect(self):
        result = self._recommend(None)
        tools = [item["tool"] for item in result["steps"]]
        self.assertIn("pandrator_plan_media_edit_workflow", tools)
        self.assertNotIn("pandrator_prepare_media_edit", tools)
        self.assertNotIn("pandrator_create_media_edit_dispatch_run", tools)

    def test_every_media_edit_goal_uses_the_media_edit_planner(self):
        result = self._recommend(None, goal="Remove the guest introduction")
        tools = [item["tool"] for item in result["steps"]]
        self.assertIn("pandrator_plan_media_edit_workflow", tools)
        self.assertNotIn("pandrator_plan_workflow", tools)
        self.assertNotIn("pandrator_create_dispatch_run", tools)

    def test_blank_media_edit_goal_stays_in_the_media_edit_surface(self):
        result = self._recommend(None, goal=None)
        tools = [item["tool"] for item in result["steps"]]
        self.assertIn("pandrator_get_media_edit", tools)
        self.assertNotIn("pandrator_plan_workflow", tools)

    def test_sessionless_media_edit_goal_routes_to_workflow_guide(self):
        runtime = SimpleNamespace(require_application=lambda: None)
        result = recommend_next_steps(
            runtime,
            RecommendNextStepsInput(goal="Cut video using the Zoom captions"),
        )
        explain = next(
            item
            for item in result["steps"]
            if item["tool"] == "pandrator_explain_system"
        )
        self.assertEqual("workflows", explain["arguments"]["topic"])


if __name__ == "__main__":
    unittest.main()
