"""Compact Manager status detail and transport contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.catalog import ACTION_CATALOG
from pandrator_mcp.context import McpRuntime, build_runtime
from pandrator_mcp.schemas import TOOL_INPUT_MODELS
from pandrator_mcp.schemas.manager import ManagerStatusInput
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.manager import manager_status


def _component_inventory() -> list[dict]:
    items = []
    for component_index in range(12):
        models = [
            {
                "id": f"model_{component_index}_{model_index}",
                "label": f"Known model {component_index} {model_index}",
                "license_name": "Example License",
                "license_url": "https://example.test/license",
                "usage_note": "Managed catalogue metadata for status inspection.",
                "estimated_download_bytes": 10_000_000,
            }
            for model_index in range(6)
        ]
        model_ids = [model["id"] for model in models]
        items.append(
            {
                "definition": {
                    "id": "audio_cpp" if component_index == 0 else f"component_{component_index}",
                    "models": models,
                },
                "inspection": {"installed_model_ids": model_ids},
                "desired": {"options": {"models": model_ids}},
            }
        )
    return items


def _status_payload() -> dict:
    return {
        "available": True,
        "gateway": "application_manager",
        "status": {
            "manager_version": "2.4.1",
            "api_versions": ["v1"],
            "instance_id": "manager-instance-1",
            "configuration_revision": 17,
            "ready": True,
            "capabilities": ["components", "operations"],
            "active_operation_id": "operation-1",
        },
        "health": {"status": "healthy", "protocol_version": "1"},
    }


class ManagerStatusCompactTests(unittest.TestCase):
    def _runtime(self):
        class Manager:
            status_calls = 0
            component_calls = 0

            @classmethod
            def status(cls):
                cls.status_calls += 1
                return _status_payload()

            @classmethod
            def components(cls):
                cls.component_calls += 1
                return {"items": _component_inventory()}

        return cast(McpRuntime, SimpleNamespace(manager=Manager)), Manager

    def test_legacy_default_remains_full_and_matches_explicit_full(self):
        runtime, manager = self._runtime()

        legacy = manager_status(runtime)
        explicit_full = manager_status(runtime, ManagerStatusInput(detail="full"))

        self.assertEqual(legacy, explicit_full)
        self.assertEqual(12, len(legacy["model_inventory"]))
        self.assertEqual(6, len(legacy["model_inventory"][0]["known_models"]))
        self.assertEqual("Example License", legacy["model_inventory"][0]["known_models"][0]["license_name"])
        self.assertEqual(2, manager.status_calls)
        self.assertEqual(2, manager.component_calls)
        self.assertEqual("full", ManagerStatusInput().detail)

    def test_summary_preserves_status_and_compacts_each_inventory_entry(self):
        runtime, manager = self._runtime()

        summary = manager_status(runtime, ManagerStatusInput(detail="summary"))

        self.assertTrue(summary["available"])
        self.assertTrue(summary["ready"])
        self.assertEqual("manager-instance-1", summary["manager_instance_id"])
        self.assertEqual(17, summary["configuration_revision"])
        self.assertEqual("operation-1", summary["active_operation_id"])
        inventory = summary["model_inventory"]
        self.assertEqual(12, len(inventory))
        for item in inventory:
            self.assertNotIn("known_models", item)
            self.assertEqual(
                {
                    "component_id",
                    "known_model_count",
                    "known_models_truncated",
                    "catalogue_tool",
                    "installed_model_ids",
                    "desired_model_ids",
                },
                set(item),
            )
            self.assertEqual(6, item["known_model_count"])
            self.assertFalse(item["known_models_truncated"])
        self.assertEqual("pandrator_get_audio_cpp_catalogue", inventory[0]["catalogue_tool"])
        self.assertEqual(inventory[0]["installed_model_ids"], inventory[0]["desired_model_ids"])
        self.assertLess(len(json.dumps(summary, separators=(",", ":")).encode()), 4 * 1024)
        self.assertEqual(1, manager.status_calls)
        self.assertEqual(1, manager.component_calls)

    def test_catalogue_schema_registry_names_manager_status_input(self):
        action = ACTION_CATALOG.get("pandrator_manager_status")
        self.assertEqual("ManagerStatusInput", action.input_model)
        self.assertIn(ManagerStatusInput, TOOL_INPUT_MODELS)


class ManagerStatusTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_summary_and_structured_transport_use_compact_text(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="missing",
                    configuration_path=Path(directory) / "missing-targets.json",
                )
            )
            data = {
                **_status_payload(),
                "model_inventory": [
                    {
                        "component_id": f"component_{index}",
                        "known_models": [{"id": f"model_{index}_{item}"} for item in range(6)],
                        "known_model_count": 6,
                        "known_models_truncated": False,
                        "catalogue_tool": None,
                        "installed_model_ids": [f"model_{index}_{item}" for item in range(6)],
                        "desired_model_ids": [f"model_{index}_{item}" for item in range(6)],
                    }
                    for index in range(12)
                ],
            }
            calls = []

            def status_handler(_runtime, arguments):
                calls.append(arguments)
                return data

            with patch("pandrator_mcp.server.manager_status", side_effect=status_handler):
                async with Client(
                    build_server(runtime), mode="auto", raise_exceptions=True
                ) as client:
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    schema = tools["pandrator_manager_status"].input_schema
                    self.assertEqual(
                        ["summary", "full"], schema["properties"]["detail"]["enum"]
                    )
                    self.assertEqual("full", schema["properties"]["detail"]["default"])
                    self.assertEqual(
                        ["standard", "structured"],
                        schema["properties"]["response_mode"]["enum"],
                    )
                    self.assertEqual(
                        "standard", schema["properties"]["response_mode"]["default"]
                    )

                    legacy_full = await client.call_tool("pandrator_manager_status", {})
                    standard = await client.call_tool(
                        "pandrator_manager_status", {"detail": "summary"}
                    )
                    structured = await client.call_tool(
                        "pandrator_manager_status",
                        {"detail": "summary", "response_mode": "structured"},
                    )

            self.assertEqual(data, legacy_full.structured_content["result"])
            self.assertEqual(data, standard.structured_content["result"])
            self.assertEqual(data, structured.structured_content["result"])
            text_content = structured.content[0]
            self.assertIsInstance(text_content, TextContent)
            assert isinstance(text_content, TextContent)
            marker = json.loads(text_content.text)
            self.assertEqual("structuredContent", marker["data"])
            self.assertNotIn("known_models", text_content.text)
            self.assertLess(len(text_content.text), 500)
            self.assertEqual(
                ["full", "summary", "summary"], [call.detail for call in calls]
            )


if __name__ == "__main__":
    unittest.main()
