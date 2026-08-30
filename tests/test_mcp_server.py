import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import Client, ClientSession, StdioServerParameters, stdio_client
except ImportError:
    Client = None
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

from pandrator_mcp import __version__
from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.context import build_runtime
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings


@unittest.skipIf(Client is None, "The standalone MCP SDK dependency is not installed.")
class McpServerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_tools_and_static_guidance_over_in_memory_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="unconfigured",
                    configuration_path=Path(directory) / "missing-targets.json",
                )
            )
            server = build_server(runtime)
            async with Client(server, mode="auto", raise_exceptions=True) as client:
                self.assertEqual("2026-07-28", client.protocol_version)
                self.assertEqual(__version__, client.server_info.version)
                self.assertEqual(
                    ["2026-07-28"],
                    client.session.discover_result.supported_versions,
                )
                listed = await client.list_tools()
                self.assertEqual(0, listed.ttl_ms)
                self.assertEqual("private", listed.cache_scope)
                self.assertEqual(
                    {"name": "Pandrator", "version": __version__},
                    listed.meta["io.modelcontextprotocol/serverInfo"],
                )
                names = sorted(tool.name for tool in listed.tools)
                self.assertEqual(
                    [
                        "pandrator_attach_existing_source",
                        "pandrator_cancel_work",
                        "pandrator_claim_dispatch_batch",
                        "pandrator_claim_source_cleaning_dispatch_batch",
                        "pandrator_control_runtime",
                        "pandrator_create_dispatch_run",
                        "pandrator_create_session",
                        "pandrator_create_source_cleaning_dispatch_run",
                        "pandrator_execute_component_plan",
                        "pandrator_execute_workflow_plan",
                        "pandrator_explain_system",
                        "pandrator_get_capabilities",
                        "pandrator_get_dispatch_run",
                        "pandrator_get_provider_status",
                        "pandrator_get_session",
                        "pandrator_get_session_settings",
                        "pandrator_get_source_cleaning_dispatch_run",
                        "pandrator_get_system_status",
                        "pandrator_get_target_status",
                        "pandrator_get_voice_catalog",
                        "pandrator_get_work",
                        "pandrator_get_work_log",
                        "pandrator_get_workflow",
                        "pandrator_inspect_source_cleaning_dispatch_extraction",
                        "pandrator_list_artifacts",
                        "pandrator_list_dispatch_runs",
                        "pandrator_list_sessions",
                        "pandrator_list_source_cleaning_dispatch_runs",
                        "pandrator_list_sources",
                        "pandrator_list_work",
                        "pandrator_manager_doctor",
                        "pandrator_manager_status",
                        "pandrator_plan_component_change",
                        "pandrator_plan_workflow",
                        "pandrator_recommend_next_steps",
                        "pandrator_release_dispatch_batch",
                        "pandrator_release_source_cleaning_dispatch_batch",
                        "pandrator_renew_dispatch_batch",
                        "pandrator_renew_source_cleaning_dispatch_batch",
                        "pandrator_submit_dispatch_batch",
                        "pandrator_submit_source_cleaning_dispatch_batch",
                        "pandrator_update_session",
                        "pandrator_update_session_settings",
                    ],
                    names,
                )
                self.assertEqual(
                    {action.name for action in ACTION_CATALOG.list() if action.enabled},
                    set(names),
                )
                for tool in listed.tools:
                    properties = set(tool.input_schema.get("properties", {}))
                    self.assertTrue(
                        properties.isdisjoint(
                            {
                                "credential",
                                "origin",
                                "proxy",
                                "target",
                                "token",
                                "url",
                            }
                        )
                    )
                    spec = ACTION_CATALOG.get(tool.name)
                    self.assertEqual(
                        spec.risk == RiskClass.READ,
                        tool.annotations.read_only_hint,
                    )
                dispatch_tools = {
                    tool.name: tool
                    for tool in listed.tools
                    if "dispatch" in tool.name
                }
                self.assertEqual(
                    {
                        "pandrator_claim_dispatch_batch",
                        "pandrator_claim_source_cleaning_dispatch_batch",
                        "pandrator_create_dispatch_run",
                        "pandrator_create_source_cleaning_dispatch_run",
                        "pandrator_get_dispatch_run",
                        "pandrator_get_source_cleaning_dispatch_run",
                        "pandrator_inspect_source_cleaning_dispatch_extraction",
                        "pandrator_list_dispatch_runs",
                        "pandrator_list_source_cleaning_dispatch_runs",
                        "pandrator_release_dispatch_batch",
                        "pandrator_release_source_cleaning_dispatch_batch",
                        "pandrator_renew_dispatch_batch",
                        "pandrator_renew_source_cleaning_dispatch_batch",
                        "pandrator_submit_dispatch_batch",
                        "pandrator_submit_source_cleaning_dispatch_batch",
                    },
                    set(dispatch_tools),
                )
                for name in (
                    "pandrator_list_dispatch_runs",
                    "pandrator_get_dispatch_run",
                    "pandrator_list_source_cleaning_dispatch_runs",
                    "pandrator_get_source_cleaning_dispatch_run",
                ):
                    self.assertTrue(dispatch_tools[name].annotations.read_only_hint)
                for name in set(dispatch_tools) - {
                    "pandrator_list_dispatch_runs",
                    "pandrator_get_dispatch_run",
                    "pandrator_list_source_cleaning_dispatch_runs",
                    "pandrator_get_source_cleaning_dispatch_run",
                }:
                    self.assertFalse(dispatch_tools[name].annotations.read_only_hint)
                for name in (
                    "pandrator_renew_dispatch_batch",
                    "pandrator_release_dispatch_batch",
                    "pandrator_submit_dispatch_batch",
                    "pandrator_renew_source_cleaning_dispatch_batch",
                    "pandrator_release_source_cleaning_dispatch_batch",
                    "pandrator_submit_source_cleaning_dispatch_batch",
                    "pandrator_inspect_source_cleaning_dispatch_extraction",
                ):
                    self.assertIn(
                        "lease_token",
                        dispatch_tools[name].input_schema["properties"],
                    )
                    self.assertNotIn(
                        "token",
                        dispatch_tools[name].input_schema["properties"],
                    )
                explain_schema = next(
                    tool.input_schema
                    for tool in listed.tools
                    if tool.name == "pandrator_explain_system"
                )
                self.assertEqual(
                    [
                        "artifacts-and-revisions",
                        "audiobooks",
                        "durable-work",
                        "manager-and-recovery",
                        "overview",
                        "providers-and-voices",
                        "remote-targets",
                        "security-boundaries",
                        "subtitles",
                        "voiceover-and-dubbing",
                        "workflows",
                    ],
                    explain_schema["properties"]["topic"]["enum"],
                )

                result = await client.call_tool(
                    "pandrator_explain_system",
                    {
                        "topic": "overview",
                        "include_live_context": False,
                    },
                )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    result.structured_content,
                    json.loads(result.content[0].text),
                )
                self.assertEqual(
                    "overview",
                    result.structured_content["result"]["topic"],
                )
                alias_result = await client.call_tool(
                    "pandrator_explain_system",
                    {
                        "topic": "durable workflows",
                        "include_live_context": False,
                    },
                )
                self.assertFalse(alias_result.is_error)
                self.assertEqual(
                    "durable-work",
                    alias_result.structured_content["result"]["topic"],
                )

                resources = await client.list_resources()
                self.assertIn(
                    "pandrator://guide/index",
                    {str(resource.uri) for resource in resources.resources},
                )
                templates = await client.list_resource_templates()
                template_uris = {
                    template.uri_template for template in templates.resource_templates
                }
                self.assertIn("pandrator://guide/{topic}", template_uris)
                self.assertIn(
                    "pandrator://sessions/{session_id}/workflow",
                    template_uris,
                )

                prompts = await client.list_prompts()
                self.assertEqual(
                    [
                        "diagnose_failed_work",
                        "dub_media",
                        "produce_subtitles",
                        "repair_pandrator_instance",
                        "start_audiobook",
                    ],
                    sorted(prompt.name for prompt in prompts.prompts),
                )

    async def test_maintained_legacy_protocol_mode_negotiates(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="unconfigured",
                    configuration_path=Path(directory) / "missing-targets.json",
                )
            )
            async with Client(
                build_server(runtime),
                mode="legacy",
                raise_exceptions=True,
            ) as client:
                listed = await client.list_tools()
                self.assertIn(
                    "pandrator_explain_system",
                    {tool.name for tool in listed.tools},
                )

    async def test_stdio_framing_survives_handler_prints_and_warnings(self):
        script = (
            "import warnings\n"
            "import pandrator_mcp.server as adapter\n"
            "from pathlib import Path\n"
            "from pandrator_mcp.context import build_runtime\n"
            "from pandrator_mcp.settings import McpSettings\n"
            "original = adapter.explain_system\n"
            "def noisy(*args):\n"
            "    print('deliberate dependency stdout noise')\n"
            "    warnings.warn('deliberate dependency warning')\n"
            "    return original(*args)\n"
            "adapter.explain_system = noisy\n"
            "runtime = build_runtime(McpSettings("
            "target_name='missing', configuration_path=Path('missing.json')))\n"
            "original_index = runtime.guides.index\n"
            "def noisy_index():\n"
            "    print('deliberate resource stdout noise')\n"
            "    warnings.warn('deliberate resource warning')\n"
            "    return original_index()\n"
            "runtime.guides.index = noisy_index\n"
            "adapter.build_server(runtime).run()\n"
        )
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as errors:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", script],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={"PYTHONWARNINGS": "default"},
            )
            async with (
                stdio_client(parameters, errlog=errors) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "pandrator_explain_system",
                    {
                        "topic": "overview",
                        "include_live_context": False,
                    },
                )
                self.assertFalse(result.is_error)
                resource = await session.read_resource("pandrator://guide/index")
                self.assertTrue(resource.contents)
            errors.seek(0)
            diagnostics = errors.read()
            self.assertIn("deliberate dependency stdout noise", diagnostics)
            self.assertIn("deliberate dependency warning", diagnostics)
            self.assertIn("deliberate resource stdout noise", diagnostics)
            self.assertIn("deliberate resource warning", diagnostics)

    async def test_modern_protocol_discovery_over_real_stdio(self):
        script = (
            "from pathlib import Path\n"
            "from pandrator_mcp.context import build_runtime\n"
            "from pandrator_mcp.server import build_server\n"
            "from pandrator_mcp.settings import McpSettings\n"
            "runtime = build_runtime(McpSettings("
            "target_name='missing', configuration_path=Path('missing.json')))\n"
            "build_server(runtime).run()\n"
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with Client(parameters, mode="auto", raise_exceptions=True) as client:
            self.assertEqual("2026-07-28", client.protocol_version)
            self.assertEqual(__version__, client.server_info.version)
            listed = await client.list_tools()
            self.assertIn(
                "pandrator_create_dispatch_run",
                {tool.name for tool in listed.tools},
            )


if __name__ == "__main__":
    unittest.main()
