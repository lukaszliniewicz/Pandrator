import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

try:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from starlette.testclient import TestClient
except ImportError:
    httpx2 = None
    ClientSession = None
    streamable_http_client = None
    TestClient = None

from pandrator_mcp.__main__ import main
from pandrator_mcp.context import (
    MANAGED_TARGET_NAME,
    build_managed_runtime,
    build_runtime,
)
from pandrator_mcp.host_config import render_http_host_config
from pandrator_mcp.http import build_http_app, read_bearer_token
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.targets import TargetStore


@unittest.skipIf(TestClient is None, "The standalone MCP HTTP dependencies are unavailable.")
class McpHttpTransportTests(unittest.IsolatedAsyncioTestCase):
    token = "t" * 43

    def _runtime(self, root: Path):
        return build_runtime(
            McpSettings(
                target_name="missing",
                configuration_path=root / "missing.json",
            )
        )

    async def test_health_authentication_and_july_protocol_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            app = build_http_app(self._runtime(Path(directory)), token=self.token)

            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(200, health.status_code)
                self.assertEqual("2026-07-28", health.json()["protocol_version"])
                unauthorized = client.post("/mcp", json={})
                self.assertEqual(401, unauthorized.status_code)
                self.assertEqual("Bearer", unauthorized.headers["www-authenticate"])

            protocol_app = build_http_app(
                self._runtime(Path(directory)),
                token=self.token,
            )
            transport = httpx2.ASGITransport(app=protocol_app)
            headers = {"Authorization": f"Bearer {self.token}"}
            async with (
                protocol_app.app.router.lifespan_context(protocol_app.app),
                httpx2.AsyncClient(
                    transport=transport,
                    base_url="http://127.0.0.1:8099",
                    headers=headers,
                ) as http_client,
                streamable_http_client(
                    "http://127.0.0.1:8099/mcp",
                    http_client=http_client,
                ) as streams,
                ClientSession(*streams) as session,
            ):
                initialized = await session.initialize()
                self.assertEqual("0.3.0", initialized.server_info.version)
                listed = await session.list_tools()
                self.assertIn(
                    "pandrator_create_dispatch_run",
                    {tool.name for tool in listed.tools},
                )

    def test_transport_rejects_wrong_host_origin_and_duplicate_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            app = build_http_app(self._runtime(Path(directory)), token=self.token)
            authorization = {"Authorization": f"Bearer {self.token}"}
            with TestClient(app) as client:
                wrong_host = client.post(
                    "/mcp",
                    json={},
                    headers={**authorization, "Host": "attacker.invalid"},
                )
                self.assertEqual(421, wrong_host.status_code)
                wrong_origin = client.post(
                    "/mcp",
                    json={},
                    headers={
                        **authorization,
                        "Host": "127.0.0.1:8099",
                        "Origin": "https://attacker.invalid",
                    },
                )
                self.assertEqual(403, wrong_origin.status_code)
                duplicate = client.post(
                    "/mcp",
                    json={},
                    headers=[
                        ("Host", "127.0.0.1:8099"),
                        ("Authorization", f"Bearer {self.token}"),
                        ("Authorization", f"Bearer {self.token}"),
                    ],
                )
                self.assertEqual(401, duplicate.status_code)

    def test_managed_runtime_persists_a_non_secret_workspace_target(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            configuration = Path(directory) / "mcp-targets.json"
            runtime = build_managed_runtime(
                workspace,
                configuration_path=configuration,
            )
            profile = TargetStore(configuration).load(missing_ok=False)[0]

        self.assertEqual(MANAGED_TARGET_NAME, runtime.settings.target_name)
        self.assertEqual(str(workspace.resolve()), profile.workspace)
        self.assertIsNone(profile.application_credential)
        self.assertIsNone(profile.manager_recovery_credential)


class ManagedHostConfigurationTests(unittest.TestCase):
    token = "c" * 43

    def test_all_http_templates_use_the_loopback_endpoint_and_bearer(self):
        endpoint = "http://127.0.0.1:8099/mcp"
        codex = tomllib.loads(
            render_http_host_config(
                "codex",
                endpoint=endpoint,
                bearer_token=self.token,
            ).content
        )["mcp_servers"]["pandrator"]
        self.assertEqual(endpoint, codex["url"])
        self.assertEqual(f"Bearer {self.token}", codex["http_headers"]["Authorization"])

        claude = json.loads(
            render_http_host_config(
                "claude-code",
                endpoint=endpoint,
                bearer_token=self.token,
            ).content
        )["mcpServers"]["pandrator"]
        self.assertEqual("http", claude["type"])

        opencode = json.loads(
            render_http_host_config(
                "opencode",
                endpoint=endpoint,
                bearer_token=self.token,
            ).content
        )["mcp"]["pandrator"]
        self.assertEqual("remote", opencode["type"])
        self.assertFalse(opencode["oauth"])

        antigravity = json.loads(
            render_http_host_config(
                "antigravity",
                endpoint=endpoint,
                bearer_token=self.token,
            ).content
        )["mcpServers"]["pandrator"]
        self.assertEqual(endpoint, antigravity["serverUrl"])

    def test_cli_requires_explicit_secret_output_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            credential = workspace / "Pandrator" / "state" / "mcp.secret"
            credential.parent.mkdir(parents=True)
            credential.write_text(self.token, encoding="utf-8")
            output = StringIO()
            error = StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main(
                    [
                        "managed-host-config",
                        "codex",
                        "--workspace",
                        str(workspace),
                    ]
                )
            self.assertEqual(2, result)
            self.assertNotIn(self.token, output.getvalue())

    def test_credential_reader_rejects_short_values(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "mcp.secret"
            credential.write_text("short", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                read_bearer_token(credential)


if __name__ == "__main__":
    unittest.main()
