import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from pandrator_mcp.__main__ import main
from pandrator_mcp.credentials import CredentialReference
from pandrator_mcp.host_config import render_host_config
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.targets import TargetProfile, TargetStore


class HostConfigurationTests(unittest.TestCase):
    def test_all_host_templates_are_current_local_stdio_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration = (
                Path(directory) / "targets.json"
            ).resolve()
            expected_command = [
                "pandrator-mcp",
                "stdio",
                "--target",
                "production",
                "--config",
                str(configuration),
            ]

            codex = render_host_config(
                "codex",
                target="production",
                configuration_path=configuration,
            )
            codex_payload = tomllib.loads(codex.content)
            self.assertEqual(
                expected_command[0],
                codex_payload["mcp_servers"][
                    "pandrator-production"
                ]["command"],
            )
            self.assertEqual(
                expected_command[1:],
                codex_payload["mcp_servers"][
                    "pandrator-production"
                ]["args"],
            )
            self.assertEqual(
                "writes",
                codex_payload["mcp_servers"][
                    "pandrator-production"
                ]["default_tools_approval_mode"],
            )

            claude = json.loads(
                render_host_config(
                    "claude-code",
                    target="production",
                    configuration_path=configuration,
                ).content
            )
            claude_server = claude["mcpServers"][
                "pandrator-production"
            ]
            self.assertEqual("stdio", claude_server["type"])
            self.assertEqual(
                expected_command,
                [
                    claude_server["command"],
                    *claude_server["args"],
                ],
            )

            opencode = json.loads(
                render_host_config(
                    "opencode",
                    target="production",
                    configuration_path=configuration,
                ).content
            )
            opencode_server = opencode["mcp"]["servers"][
                "pandrator-production"
            ]
            self.assertEqual("local", opencode_server["type"])
            self.assertEqual(
                expected_command,
                opencode_server["command"],
            )
            self.assertNotIn("enabled", opencode_server)

            antigravity = json.loads(
                render_host_config(
                    "antigravity",
                    target="production",
                    configuration_path=configuration,
                ).content
            )
            antigravity_server = antigravity["mcpServers"][
                "pandrator-production"
            ]
            self.assertEqual(
                expected_command,
                [
                    antigravity_server["command"],
                    *antigravity_server["args"],
                ],
            )

    def test_cli_requires_existing_target_and_never_prints_references(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration = Path(directory) / "targets.json"
            TargetStore(configuration).put(
                TargetProfile(
                    name="production",
                    mode=TargetMode.EXTERNAL_HTTPS,
                    application_origin="https://pandrator.example",
                    application_credential=CredentialReference(
                        backend="environment",
                        reference="PANDRATOR_PRIVATE_TOKEN",
                        audience="application",
                    ),
                )
            )

            output = StringIO()
            error = StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main(
                    [
                        "host-config",
                        "antigravity",
                        "--target",
                        "production",
                        "--config",
                        str(configuration),
                    ]
                )
            self.assertEqual(0, result, error.getvalue())
            self.assertNotIn(
                "PANDRATOR_PRIVATE_TOKEN",
                output.getvalue(),
            )

            output = StringIO()
            error = StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main(
                    [
                        "print-config",
                        "--config",
                        str(configuration),
                    ]
                )
            self.assertEqual(0, result, error.getvalue())
            public = json.loads(output.getvalue())
            self.assertTrue(
                public["targets"][0][
                    "application_credential_configured"
                ]
            )
            self.assertNotIn(
                "PANDRATOR_PRIVATE_TOKEN",
                output.getvalue(),
            )

            output = StringIO()
            error = StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main(
                    [
                        "host-config",
                        "codex",
                        "--target",
                        "missing",
                        "--config",
                        str(configuration),
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn(
                "target does not exist",
                error.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
