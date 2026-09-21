import unittest
from types import SimpleNamespace

try:
    from mcp import Client
except ImportError:
    Client = None

from pandrator_mcp.guide_registry import GuideRegistry
from pandrator_mcp.schemas import ExplainSystemInput
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.guidance import explain_system


class _CountingApplication:
    def __init__(self):
        self.health_calls = 0
        self.identity_calls = 0

    def health(self):
        self.health_calls += 1
        return {"status": "ok"}

    def identity(self):
        self.identity_calls += 1
        return {"application_version": "test"}


def _runtime(application=None):
    return SimpleNamespace(guides=GuideRegistry(), application=application)


class CompactGuidanceTests(unittest.TestCase):
    def test_schema_defaults_to_a_compact_non_live_summary(self):
        arguments = ExplainSystemInput()

        self.assertEqual("summary", arguments.detail)
        self.assertFalse(arguments.include_live_context)
        schema = ExplainSystemInput.model_json_schema()
        self.assertEqual("summary", schema["properties"]["detail"]["default"])
        self.assertFalse(
            schema["properties"]["include_live_context"]["default"]
        )

    def test_default_summary_uses_canonical_alias_without_live_calls(self):
        application = _CountingApplication()
        result = explain_system(
            _runtime(application),
            ExplainSystemInput(topic="durable workflows"),
        )

        self.assertEqual("durable-work", result["topic"])
        self.assertEqual("new_user", result["audience"])
        self.assertEqual(
            {
                "schema_version",
                "topic",
                "title",
                "summary",
                "audiences",
                "revision",
                "related_tools",
                "full_guide_arguments",
                "audience",
            },
            set(result),
        )
        self.assertNotIn("content", result)
        self.assertNotIn("live_context", result)
        self.assertEqual(
            {
                "topic": "durable-work",
                "detail": "full",
                "include_live_context": False,
            },
            result["full_guide_arguments"],
        )
        self.assertEqual(0, application.health_calls)
        self.assertEqual(0, application.identity_calls)

    def test_full_detail_preserves_registry_content_and_shape(self):
        registry = GuideRegistry()
        expected = registry.get("overview")
        result = explain_system(
            SimpleNamespace(guides=registry, application=None),
            ExplainSystemInput(detail="full"),
        )

        self.assertEqual(expected["content"], result["content"])
        self.assertEqual(expected["topic"], result["topic"])
        self.assertEqual(expected["summary"], result["summary"])
        self.assertNotIn("full_guide_arguments", result)
        self.assertEqual("new_user", result["audience"])

    def test_live_context_is_opt_in_and_unavailable_when_disconnected(self):
        application = _CountingApplication()
        result = explain_system(
            _runtime(application),
            ExplainSystemInput(include_live_context=True),
        )

        self.assertEqual(
            {
                "health": {"status": "ok"},
                "identity": {"application_version": "test"},
            },
            result["live_context"],
        )
        self.assertEqual(1, application.health_calls)
        self.assertEqual(1, application.identity_calls)

        disconnected = explain_system(
            _runtime(),
            ExplainSystemInput(include_live_context=True),
        )
        self.assertNotIn("live_context", disconnected)


@unittest.skipIf(Client is None, "The standalone MCP SDK dependency is not installed.")
class CompactGuidanceServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_tool_schema_and_forwarding_use_compact_defaults(self):
        application = _CountingApplication()
        async with Client(
            build_server(_runtime(application)), mode="auto", raise_exceptions=True
        ) as client:
            listed = await client.list_tools()
            tool = next(
                tool
                for tool in listed.tools
                if tool.name == "pandrator_explain_system"
            )
            self.assertEqual("summary", tool.input_schema["properties"]["detail"]["default"])
            self.assertFalse(
                tool.input_schema["properties"]["include_live_context"]["default"]
            )

            compact = await client.call_tool("pandrator_explain_system", {})
            self.assertFalse(compact.is_error)
            compact_result = compact.structured_content["result"]
            self.assertNotIn("content", compact_result)
            self.assertEqual(0, application.health_calls)
            self.assertEqual(0, application.identity_calls)

            full = await client.call_tool(
                "pandrator_explain_system",
                {"topic": "overview", "detail": "full"},
            )
            self.assertFalse(full.is_error)
            self.assertIn("content", full.structured_content["result"])

            live = await client.call_tool(
                "pandrator_explain_system",
                {"include_live_context": True},
            )
            self.assertFalse(live.is_error)
            self.assertIn("live_context", live.structured_content["result"])
            self.assertEqual(1, application.health_calls)
            self.assertEqual(1, application.identity_calls)


if __name__ == "__main__":
    unittest.main()
