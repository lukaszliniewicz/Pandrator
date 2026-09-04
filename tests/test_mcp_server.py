import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

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
                        "pandrator_assemble_generation_run",
                        "pandrator_attach_existing_source",
                        "pandrator_browse_local_sources",
                        "pandrator_cancel_work",
                        "pandrator_claim_dispatch_batch",
                        "pandrator_claim_source_cleaning_dispatch_batch",
                        "pandrator_claim_speech_optimization_dispatch_batch",
                        "pandrator_configure_tts",
                        "pandrator_control_runtime",
                        "pandrator_create_dispatch_run",
                        "pandrator_create_session",
                        "pandrator_create_source_cleaning_dispatch_run",
                        "pandrator_create_speech_optimization_dispatch_run",
                        "pandrator_create_text_source",
                        "pandrator_describe_parameters",
                        "pandrator_download_artifact",
                        "pandrator_execute_component_plan",
                        "pandrator_execute_workflow_plan",
                        "pandrator_explain_system",
                        "pandrator_get_capabilities",
                        "pandrator_get_dispatch_run",
                        "pandrator_get_provider_status",
                        "pandrator_get_session",
                        "pandrator_get_session_settings",
                        "pandrator_get_source_cleaning_dispatch_run",
                        "pandrator_get_speech_optimization_dispatch_run",
                        "pandrator_get_system_status",
                        "pandrator_get_target_status",
                        "pandrator_get_tts_catalog",
                        "pandrator_get_voice_catalog",
                        "pandrator_get_work",
                        "pandrator_get_work_log",
                        "pandrator_get_workflow",
                        "pandrator_import_local_source",
                        "pandrator_import_subtitles",
                        "pandrator_inspect_source_cleaning_dispatch_extraction",
                        "pandrator_list_artifacts",
                        "pandrator_list_dispatch_runs",
                        "pandrator_list_generation_runs",
                        "pandrator_list_generation_segments",
                        "pandrator_list_sessions",
                        "pandrator_list_source_cleaning_dispatch_runs",
                        "pandrator_list_sources",
                        "pandrator_list_speech_optimization_dispatch_runs",
                        "pandrator_list_work",
                        "pandrator_manager_doctor",
                        "pandrator_manager_status",
                        "pandrator_patch_subtitle_cues",
                        "pandrator_plan_component_change",
                        "pandrator_plan_export_variant",
                        "pandrator_plan_orchestrated_workflow",
                        "pandrator_plan_workflow",
                        "pandrator_preview_subtitles",
                        "pandrator_recommend_next_steps",
                        "pandrator_regenerate_segments",
                        "pandrator_release_dispatch_batch",
                        "pandrator_release_source_cleaning_dispatch_batch",
                        "pandrator_release_speech_optimization_dispatch_batch",
                        "pandrator_renew_dispatch_batch",
                        "pandrator_renew_source_cleaning_dispatch_batch",
                        "pandrator_renew_speech_optimization_dispatch_batch",
                        "pandrator_replace_subtitle_text",
                        "pandrator_select_take",
                        "pandrator_submit_dispatch_batch",
                        "pandrator_submit_source_cleaning_dispatch_batch",
                        "pandrator_submit_speech_optimization_dispatch_batch",
                        "pandrator_update_generation_segment",
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
                tools_by_name = {tool.name: tool for tool in listed.tools}
                execution_contracts = {
                    "pandrator_create_dispatch_run": {
                        "session_id": "session-1",
                        "kind": "correction",
                        "idempotency_key": "dispatch-key",
                    },
                    "pandrator_create_speech_optimization_dispatch_run": {
                        "session_id": "session-1",
                        "idempotency_key": "speech-key",
                    },
                    "pandrator_plan_orchestrated_workflow": {
                        "session_id": "session-1",
                        "goal": "Create the final export",
                    },
                }
                for name, base in execution_contracts.items():
                    schema = tools_by_name[name].input_schema
                    validator = Draft202012Validator(schema)
                    self.assertIn("context_capsule", schema["properties"])
                    self.assertFalse(list(validator.iter_errors(base)))
                    self.assertFalse(
                        list(
                            validator.iter_errors(
                                {
                                    **base,
                                    "execution_mode": "parallel",
                                    "max_parallel_batches": 3,
                                }
                            )
                        )
                    )
                    self.assertTrue(
                        list(
                            validator.iter_errors(
                                {
                                    **base,
                                    "execution_mode": "serial",
                                    "max_parallel_batches": 2,
                                }
                            )
                        )
                    )
                    self.assertTrue(
                        list(
                            validator.iter_errors(
                                {
                                    **base,
                                    "execution_mode": "parallel",
                                    "max_parallel_batches": 1,
                                }
                            )
                        )
                    )
                dispatch_tools = {
                    tool.name: tool for tool in listed.tools if "dispatch" in tool.name
                }
                self.assertEqual(
                    {
                        "pandrator_claim_dispatch_batch",
                        "pandrator_claim_source_cleaning_dispatch_batch",
                        "pandrator_claim_speech_optimization_dispatch_batch",
                        "pandrator_create_dispatch_run",
                        "pandrator_create_source_cleaning_dispatch_run",
                        "pandrator_create_speech_optimization_dispatch_run",
                        "pandrator_get_dispatch_run",
                        "pandrator_get_source_cleaning_dispatch_run",
                        "pandrator_get_speech_optimization_dispatch_run",
                        "pandrator_inspect_source_cleaning_dispatch_extraction",
                        "pandrator_list_dispatch_runs",
                        "pandrator_list_source_cleaning_dispatch_runs",
                        "pandrator_list_speech_optimization_dispatch_runs",
                        "pandrator_release_dispatch_batch",
                        "pandrator_release_source_cleaning_dispatch_batch",
                        "pandrator_release_speech_optimization_dispatch_batch",
                        "pandrator_renew_dispatch_batch",
                        "pandrator_renew_source_cleaning_dispatch_batch",
                        "pandrator_renew_speech_optimization_dispatch_batch",
                        "pandrator_submit_dispatch_batch",
                        "pandrator_submit_source_cleaning_dispatch_batch",
                        "pandrator_submit_speech_optimization_dispatch_batch",
                    },
                    set(dispatch_tools),
                )
                for name in (
                    "pandrator_list_dispatch_runs",
                    "pandrator_get_dispatch_run",
                    "pandrator_list_source_cleaning_dispatch_runs",
                    "pandrator_get_source_cleaning_dispatch_run",
                    "pandrator_list_speech_optimization_dispatch_runs",
                    "pandrator_get_speech_optimization_dispatch_run",
                ):
                    self.assertTrue(dispatch_tools[name].annotations.read_only_hint)
                for name in set(dispatch_tools) - {
                    "pandrator_list_dispatch_runs",
                    "pandrator_get_dispatch_run",
                    "pandrator_list_source_cleaning_dispatch_runs",
                    "pandrator_get_source_cleaning_dispatch_run",
                    "pandrator_list_speech_optimization_dispatch_runs",
                    "pandrator_get_speech_optimization_dispatch_run",
                }:
                    self.assertFalse(dispatch_tools[name].annotations.read_only_hint)
                for name in (
                    "pandrator_renew_dispatch_batch",
                    "pandrator_release_dispatch_batch",
                    "pandrator_submit_dispatch_batch",
                    "pandrator_renew_source_cleaning_dispatch_batch",
                    "pandrator_release_source_cleaning_dispatch_batch",
                    "pandrator_submit_source_cleaning_dispatch_batch",
                    "pandrator_renew_speech_optimization_dispatch_batch",
                    "pandrator_release_speech_optimization_dispatch_batch",
                    "pandrator_submit_speech_optimization_dispatch_batch",
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
                failed = await client.call_tool(
                    "pandrator_list_sessions",
                    {},
                )
                self.assertTrue(failed.is_error)
                failure_text = failed.content[0].text
                failure = json.loads(failure_text[failure_text.index("{") :])
                self.assertIn(
                    failure["code"],
                    {"application_unavailable", "network_policy_denied"},
                )
                self.assertTrue(failure["request_id"])

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
                        "produce_voiceover_end_to_end",
                        "repair_pandrator_instance",
                        "run_passive_processing",
                        "start_audiobook",
                    ],
                    sorted(prompt.name for prompt in prompts.prompts),
                )
                prompt = await client.get_prompt(
                    "produce_voiceover_end_to_end",
                    {"goal": "Create translated course deliverables."},
                )
                prompt_text = "\n".join(
                    item.content.text
                    for item in prompt.messages
                    if hasattr(item.content, "text")
                )
                self.assertIn("pandrator_browse_local_sources", prompt_text)
                self.assertIn("pandrator_get_tts_catalog", prompt_text)
                self.assertIn("download", prompt_text.casefold())

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
