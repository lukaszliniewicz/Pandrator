"""Transport contracts for reviewed generation-segment mutations."""

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

try:
    from mcp import Client
except ImportError:
    Client = None

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.context import build_runtime
from pandrator_mcp.errors import NextAction, PandratorMcpError
from pandrator_mcp.server import _tool_failure, build_server
from pandrator_mcp.settings import McpSettings


@unittest.skipIf(Client is None, "The standalone MCP SDK dependency is not installed.")
class GenerationBatchTransportContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_segment_update_schemas_are_discoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="unconfigured",
                    configuration_path=Path(directory) / "missing-targets.json",
                )
            )
            async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
                listed = await client.list_tools()

        tools = {tool.name: tool for tool in listed.tools}
        single = tools["pandrator_update_generation_segment"]
        batch = tools["pandrator_update_generation_segments"]
        self.assertIn("text", single.input_schema["properties"])
        self.assertIn("removed", single.input_schema["properties"])

        batch_schema = batch.input_schema
        updates_schema = batch_schema["properties"]["updates"]
        self.assertEqual(1, updates_schema["minItems"])
        self.assertEqual(100, updates_schema["maxItems"])
        Draft202012Validator.check_schema(batch_schema)
        Draft202012Validator(batch_schema).validate(
            {
                "session_id": "session-1",
                "idempotency_key": "generation-batch:1",
                "updates": [
                    {
                        "id": "segment-1",
                        "revision": 1,
                        "changes": {"removed": False},
                    }
                ],
            }
        )
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(batch_schema).validate(
                {
                    "session_id": "session-1",
                    "idempotency_key": "generation-batch:1",
                    "updates": [
                        {
                            "id": "segment-1",
                            "revision": 1,
                            "changes": {"unknown": True},
                        }
                    ],
                }
            )
        for invalid_changes in ({"text": " \t "}, {"removed": None}):
            with self.subTest(changes=invalid_changes), self.assertRaises(
                JsonSchemaValidationError
            ):
                Draft202012Validator(batch_schema).validate(
                    {
                        "session_id": "session-1",
                        "idempotency_key": "generation-batch:1",
                        "updates": [
                            {
                                "id": "segment-1",
                                "revision": 1,
                                "changes": invalid_changes,
                            }
                        ],
                    }
                )

        spec = ACTION_CATALOG.get("pandrator_update_generation_segments")
        self.assertEqual(RiskClass.WRITE, spec.risk)
        self.assertEqual("PATCH", spec.method)
        self.assertEqual(
            "/api/v1/sessions/{sessionId}/generation-segments", spec.path
        )
        self.assertTrue(spec.requires_idempotency)

    def test_tool_failure_keeps_timeout_details_and_follow_up_actions(self):
        error = PandratorMcpError(
            "application_response_timeout",
            "The Pandrator mutation timed out before a response; its outcome is unknown.",
            details={
                "timeout_seconds": 120.0,
                "operation_outcome": "unknown",
                "retry_policy": "same_request_and_idempotency_key",
            },
            retryable=True,
            next_actions=[
                NextAction(
                    tool="pandrator_get_speech_plan_status",
                    arguments={"session_id": "session-1"},
                    reason="Inspect the active plan.",
                ),
                NextAction(
                    tool="pandrator_revise_speech_block_plan_batch",
                    arguments={
                        "session_id": "session-1",
                        "expected_revision_id": "plan-revision-3",
                        "operations": [],
                        "idempotency_key": "topology:batch:1",
                    },
                    reason="Replay with the same key.",
                ),
            ],
        )
        tool_error = _tool_failure(error, "request-1")
        failure = json.loads(str(tool_error))
        self.assertEqual("application_response_timeout", failure["code"])
        self.assertEqual(error.details, failure["details"])
        self.assertEqual(
            [
                "pandrator_get_speech_plan_status",
                "pandrator_revise_speech_block_plan_batch",
            ],
            [action["tool"] for action in failure["next_actions"]],
        )


if __name__ == "__main__":
    unittest.main()
