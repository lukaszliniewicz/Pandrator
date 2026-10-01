"""Verify transport opt-in and runtime validation through the actual MCP SDK."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client

from pandrator_mcp.context import build_runtime
from pandrator_mcp.schemas.media_edit import GetMediaEditArguments
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.media_edit import get_media_edit


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_response_preserves_data_and_shortens_text(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(McpSettings(target_name="missing", configuration_path=Path(directory) / "missing.json"))
            data = {"session_id": "s", "rows": [{"text": "Full text remains intact. " * 100}]}
            with patch("pandrator_mcp.server.get_workflow", return_value=data):
                async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
                    standard = await client.call_tool("pandrator_get_workflow", {"session_id": "s"})
                    compact = await client.call_tool("pandrator_get_workflow", {"session_id": "s", "response_mode": "structured"})
                    self.assertEqual(data, standard.structured_content["result"])
                    self.assertEqual(data, compact.structured_content["result"])
                    self.assertLess(len(compact.content[0].text), len(standard.content[0].text) // 5)
                    self.assertEqual("structuredContent", json.loads(compact.content[0].text)["data"])

    async def test_nested_capsule_fields_are_discoverable_and_invalid_input_has_path(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(McpSettings(target_name="missing", configuration_path=Path(directory) / "missing.json"))
            async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                schema = tools["pandrator_create_dispatch_run"].input_schema
                self.assertIn("context_capsule", schema["properties"])
                serialized = json.dumps(schema)
                self.assertIn('"overview"', serialized)
                self.assertIn('"style_rules"', serialized)
                failure = await client.call_tool("pandrator_select_workflow_input", {
                    "session_id": "s", "consumer": "translation", "role": "translation", "artifact_id": "a",
                    "expected_outcome_revision": 0, "expected_selection_revision": 0,
                    "idempotency_key": "input-invalid-1",
                })
                self.assertTrue(failure.is_error)
                text = failure.content[0].text
                details, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
                self.assertEqual("validation_error", details["code"])
                self.assertIn("loc", details["details"]["errors"][0])


def test_media_edit_summary_is_bounded_and_full_is_unchanged():
    data = {"ready": True, "plan": {"revision": 3, "cues": [{"text": "sentinel"}] * 1000,
            "evidence": ["private"] * 1000, "instructions": "long", "keep_ranges": [{"start_ms": 10, "end_ms": 90}]}}
    runtime = SimpleNamespace(require_application=lambda: SimpleNamespace(get_media_edit=lambda _: data))
    summary = get_media_edit(runtime, GetMediaEditArguments(session_id="s"))
    assert summary["ready"] is True
    assert summary["plan"]["cue_count"] == 1000
    assert summary["plan"]["kept_duration_ms"] == 80
    assert "sentinel" not in json.dumps(summary)
    assert get_media_edit(runtime, GetMediaEditArguments(session_id="s", view="full")) == data
