import unittest
from types import SimpleNamespace
from typing import cast

from pandrator_mcp.context import McpRuntime
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
    def _recommend(self, plan, goal: str | None = "Trim video setup chatter"):
        application = _Application(plan)
        runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
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
        runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: None))
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


def test_team_onboarding_recommends_packaged_collaboration_guide_without_transport():
    for goal in ('Create a shared workspace', 'Invite a user', 'Enable collaboration'):
        result = recommend_next_steps(
            cast(McpRuntime, SimpleNamespace(application=None)), RecommendNextStepsInput(goal=goal),
        )
        assert result['basis'] == 'static_guidance'
        assert result['steps'][0]['tool'] == 'pandrator_explain_system'
        assert result['steps'][0]['arguments'] == {'topic': 'collaboration', 'detail': 'full'}
        assert result['steps'][1]['tool'] == 'pandrator_get_target_status'


FIELD_TEST_GOAL = (
    "Media edit the webinar, polish the English subtitles, translate them into "
    "four languages and make a Vertex TTS voiceover for each"
)


def _sessionless(goal: str) -> dict:
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: None))
    return recommend_next_steps(runtime, RecommendNextStepsInput(goal=goal))


def test_a_voiceover_goal_does_not_start_with_voice_casting():
    tools = [step["tool"] for step in _sessionless(FIELD_TEST_GOAL)["steps"]]
    # "voiceover" contains "voice"; a single-voice dub needs no casting first.
    assert "pandrator_get_voice_capabilities" not in tools
    assert "pandrator_get_voice_catalog" not in tools
    casting = [step["tool"] for step in _sessionless("Design a Scottish voice")["steps"]]
    assert "pandrator_get_voice_capabilities" in casting


def test_a_multilingual_goal_points_to_the_translation_project_route():
    steps = _sessionless(FIELD_TEST_GOAL)["steps"]
    guides = [
        step["arguments"]
        for step in steps
        if step["tool"] == "pandrator_explain_system"
    ]
    # The media edit still picks the workflows guide; the languages section follows.
    assert guides == [
        {"topic": "workflows"},
        {"topic": "voiceover-and-dubbing", "detail": "full"},
    ]
    dubbing = _sessionless("Dub this lecture into three languages")["steps"]
    assert [
        step["arguments"]
        for step in dubbing
        if step["tool"] == "pandrator_explain_system"
    ] == [{"topic": "voiceover-and-dubbing", "detail": "full"}]


def test_a_multilingual_session_goal_creates_a_project_not_an_in_session_translation():
    class _Voiceover(_Application):
        def get_session(self, session_id):
            return {**super().get_session(session_id), "workflow_kind": "voiceover"}

    application = _Voiceover(None)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    result = recommend_next_steps(
        runtime,
        RecommendNextStepsInput(
            session_id="session-1",
            goal="Correct the subtitles and translate them into four languages",
        ),
    )
    steps = result["steps"]
    tools = [step["tool"] for step in steps]
    assert "pandrator_create_translation_project" in tools
    assert {"session_id": "session-1", "kind": "correction"} in [
        step.get("arguments") for step in steps
    ]
    assert not any(
        step.get("arguments", {}).get("kind") == "translation" for step in steps
    )
    # Generation and export happen per branch, so no source-session plan step.
    assert "pandrator_plan_workflow" not in tools


def _branch_recommendation(goal: str, session, project=None) -> dict:
    class _Branch(_Application):
        def get_session(self, session_id):
            return {
                **super().get_session(session_id),
                "workflow_kind": "voiceover",
                **session,
            }

        def get_translation_project(self, _session_id):
            return project if project is not None else {"project": None}

    runtime = cast(
        McpRuntime,
        SimpleNamespace(require_application=lambda: _Branch(None)),
    )
    return recommend_next_steps(
        runtime,
        RecommendNextStepsInput(session_id="session-1", goal=goal),
    )


def test_a_translation_branch_plans_its_export_instead_of_a_new_project():
    result = _branch_recommendation(
        "Export the French branch of my multilingual project",
        {
            "translation_project": {
                "id": "project-1",
                "role": "branch",
                "target_language": "fr",
            }
        },
        {"project": {"id": "project-1", "name": "Four languages"}},
    )

    tools = [step["tool"] for step in result["steps"]]
    assert "pandrator_create_translation_project" not in tools
    export = next(
        step
        for step in result["steps"]
        if step["tool"] == "pandrator_plan_export_variant"
    )
    assert export["arguments"] == {"session_id": "session-1"}


def test_a_session_outside_a_project_still_creates_one():
    result = _branch_recommendation(
        "Export the French branch of my multilingual project",
        {"translation_project": None},
    )

    tools = [step["tool"] for step in result["steps"]]
    assert "pandrator_create_translation_project" in tools
    assert "pandrator_plan_export_variant" not in tools


def test_a_session_read_without_membership_asks_the_project_link():
    result = _branch_recommendation(
        "Export the French branch of my multilingual project",
        {},
        {"project": {"id": "project-1", "name": "Four languages"}},
    )

    tools = [step["tool"] for step in result["steps"]]
    assert "pandrator_create_translation_project" not in tools
    assert "pandrator_plan_export_variant" in tools
