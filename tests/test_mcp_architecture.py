import ast
import inspect
import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

from pydantic import ValidationError

from pandrator_mcp import __version__
from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import (
    _PASSTHROUGH_ERROR_CODES,
    ApplicationClient,
)
from pandrator_mcp.credentials import (
    APPROVED_CREDENTIAL_BACKENDS,
    CredentialReference,
    CredentialResolver,
    EnvironmentCredentialBackend,
)
from pandrator_mcp.errors import (
    CredentialResolutionError,
    FailureCode,
    PandratorMcpError,
    TargetResolutionError,
)
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.schemas import (
    TOOL_INPUT_MODELS,
    ManagerDesiredComponentInput,
    PlanWorkflowInput,
    SystemStatusInput,
    UpdateSessionSettingsInput,
)
from pandrator_mcp.targets import TargetProfile, TargetRegistry
from pandrator_mcp.tools.sessions import (
    _safe_setting_value,
    _workflow_projection,
)
from pandrator_mcp.tools.system import system_status

ROOT = Path(__file__).resolve().parents[1]
MCP_ROOT = ROOT / "pandrator_mcp"


class McpArchitectureTests(unittest.TestCase):
    def test_package_imports_without_pandrator_application_runtime(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import pandrator_mcp; "
                    "assert not any(name.startswith('pandrator.web') "
                    "for name in sys.modules)"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_package_has_no_forbidden_application_imports(self):
        forbidden = {
            "pandrator.web.database",
            "pandrator.web.models",
            "pandrator.web.jobs",
            "pandrator.web.application_services",
        }
        found = []
        for path in MCP_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {item.name for item in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {str(node.module or "")}
                else:
                    continue
                overlap = {
                    name
                    for name in names
                    if any(
                        name == blocked or name.startswith(f"{blocked}.")
                        for blocked in forbidden
                    )
                }
                if overlap:
                    found.append((path.relative_to(ROOT), sorted(overlap)))
        self.assertEqual([], found)

    def test_sdk_and_package_versions_are_exactly_pinned(self):
        payload = tomllib.loads(
            (MCP_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual("0.2.0", __version__)
        self.assertEqual(__version__, payload["project"]["version"])
        self.assertIn("mcp==2.1.1", payload["project"]["dependencies"])
        server_source = (MCP_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("from mcp.server import MCPServer", server_source)
        self.assertNotIn("FastMCP", server_source)

    def test_passthrough_downstream_failures_are_typed(self):
        self.assertEqual(
            set(),
            _PASSTHROUGH_ERROR_CODES - set(get_args(FailureCode)),
        )

    def test_setuptools_wheels_exclude_cached_bytecode(self):
        for package_root in (MCP_ROOT, ROOT / "pandrator_manager"):
            payload = tomllib.loads(
                (package_root / "pyproject.toml").read_text(encoding="utf-8")
            )
            excluded = payload["tool"]["setuptools"]["exclude-package-data"]["*"]
            self.assertIn("__pycache__/*", excluded)
            self.assertIn("*.py[cod]", excluded)

    def test_application_client_never_uses_raw_job_creation(self):
        source = inspect.getsource(ApplicationClient)
        self.assertNotIn("/api/v1/jobs", source)
        self.assertNotIn("session.post(", source)
        self.assertIn("/api/v1/work", source)

    def test_tool_inputs_cannot_supply_connections_or_credentials(self):
        forbidden_fields = {
            "application_origin",
            "base_url",
            "ca_bundle",
            "connection",
            "connection_target",
            "credential",
            "credential_reference",
            "host",
            "origin",
            "port",
            "proxy",
            "proxy_origin",
            "secret",
            "target",
            "target_name",
            "token",
            "url",
            "workspace",
        }
        for model in TOOL_INPUT_MODELS:
            self.assertTrue(
                forbidden_fields.isdisjoint(model.model_fields),
                f"{model.__name__}: {set(model.model_fields) & forbidden_fields}",
            )
        with self.assertRaises(ValidationError):
            PlanWorkflowInput(
                session_id="session",
                overrides={
                    "tts": {
                        "base_url": "https://untrusted.example",
                    }
                },
            )
        with self.assertRaises(ValidationError):
            UpdateSessionSettingsInput(
                session_id="session",
                section="tts",
                expected_revision=0,
                value={
                    "nested": {
                        "credential_reference": "keyring/account",
                    }
                },
                idempotency_key="settings:test",
            )
        with self.assertRaises(ValidationError):
            ManagerDesiredComponentInput(
                component_id="pandrator",
                options={
                    "nested": {
                        "command": "powershell -EncodedCommand ...",
                    }
                },
            )
        with self.assertRaises(ValidationError):
            PlanWorkflowInput(
                session_id="session",
                overrides={
                    "provider": {
                        "nested": {
                            "api_key": "must-not-enter-model-tools",
                        }
                    }
                },
            )

    def test_clients_accept_opaque_bindings_not_raw_endpoints(self):
        parameters = inspect.signature(ApplicationClient.__init__).parameters
        self.assertIn("binding", parameters)
        for forbidden in (
            "origin",
            "base_url",
            "url",
            "host",
            "proxy",
            "token",
        ):
            self.assertNotIn(forbidden, parameters)

    def test_optional_manager_context_degrades_without_losing_app_status(self):
        class Application:
            @staticmethod
            def health():
                return {"status": "ok", "version": "0.6.0"}

            @staticmethod
            def identity():
                return {
                    "application_version": "0.6.0",
                    "api_version": "v1",
                    "protocol_version": "v1",
                }

            @staticmethod
            def capabilities():
                return {"ffmpeg": {"available": True}}

            @staticmethod
            def openapi():
                return {"openapi": "3.1.0", "paths": {}}

        class Manager:
            @staticmethod
            def status():
                raise PandratorMcpError(
                    "scope_denied",
                    "The principal lacks manager.read.",
                )

        application = Application()
        runtime = SimpleNamespace(
            require_application=lambda: application,
            manager=Manager(),
            profile=SimpleNamespace(mode=TargetMode.PRIVATE_NETWORK),
        )
        outcome = system_status(
            runtime,
            SystemStatusInput(
                include_capabilities=True,
                include_manager=True,
            ),
        )
        self.assertEqual("ok", outcome.result["health"]["status"])
        self.assertFalse(outcome.result["manager"]["available"])
        self.assertEqual(
            "scope_denied",
            outcome.result["manager"]["error"]["code"],
        )
        self.assertEqual(["scope_denied"], [item.code for item in outcome.warnings])

    def test_session_tool_results_remove_paths_connections_and_metadata(self):
        workflow = _workflow_projection(
            {
                "session_id": "session",
                "workflow_kind": "audiobook",
                "revision": 3,
                "stages": [
                    {
                        "key": "prepare_text",
                        "status": "completed",
                        "detail": "raw downstream detail",
                        "artifact": {
                            "id": "artifact",
                            "relative_path": "sessions/private/file.txt",
                            "metadata_json": {
                                "token": "must-not-leak",
                            },
                            "kind": "text",
                        },
                        "artifacts": [
                            {
                                "id": "artifact",
                                "relative_path": "private/file.txt",
                                "metadata_json": {
                                    "api_key": "must-not-leak",
                                },
                            }
                        ],
                    }
                ],
            }
        )
        serialized = json.dumps(workflow)
        self.assertNotIn("relative_path", serialized)
        self.assertNotIn("metadata_json", serialized)
        self.assertNotIn("raw downstream detail", serialized)
        safe = _safe_setting_value(
            {
                "model": "voice-model",
                "nested": {
                    "api_base": "https://private.example",
                    "credential_reference": "keyring/account",
                    "temperature": 0.3,
                },
            }
        )
        self.assertEqual(
            {
                "model": "voice-model",
                "nested": {"temperature": 0.3},
            },
            safe,
        )

    def test_only_bounded_retry_safe_mutations_are_enabled(self):
        mutating = [
            action for action in ACTION_CATALOG.list() if action.risk != RiskClass.READ
        ]
        self.assertGreaterEqual(len(mutating), 5)
        self.assertEqual(
            {
                "pandrator_cancel_work",
                "pandrator_attach_existing_source",
                "pandrator_claim_dispatch_batch",
                "pandrator_claim_source_cleaning_dispatch_batch",
                "pandrator_control_runtime",
                "pandrator_create_dispatch_run",
                "pandrator_create_source_cleaning_dispatch_run",
                "pandrator_create_session",
                "pandrator_execute_component_plan",
                "pandrator_execute_workflow_plan",
                "pandrator_inspect_source_cleaning_dispatch_extraction",
                "pandrator_release_dispatch_batch",
                "pandrator_release_source_cleaning_dispatch_batch",
                "pandrator_renew_dispatch_batch",
                "pandrator_renew_source_cleaning_dispatch_batch",
                "pandrator_submit_dispatch_batch",
                "pandrator_submit_source_cleaning_dispatch_batch",
                "pandrator_update_session",
                "pandrator_update_session_settings",
            },
            {action.name for action in mutating if action.enabled},
        )
        self.assertTrue(all(action.requires_idempotency for action in mutating))
        self.assertTrue(
            ACTION_CATALOG.get(
                "pandrator_execute_workflow_plan"
            ).requires_confirmation
        )
        self.assertTrue(
            ACTION_CATALOG.get(
                "pandrator_plan_workflow"
            ).enabled
        )
        self.assertTrue(
            ACTION_CATALOG.get(
                "pandrator_plan_component_change"
            ).enabled
        )
        self.assertTrue(
            all(
                action.enabled
                for action in ACTION_CATALOG.list()
                if not action.mutating
            )
        )

    def test_manager_automation_contract_is_versioned_and_audience_bound(self):
        contract = json.loads(
            (MCP_ROOT / "contracts" / "manager-automation-v1.openapi.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("3.1.0", contract["openapi"])
        self.assertEqual(
            "1.0.0-contract",
            contract["info"]["version"],
        )
        operations = {
            operation["operationId"]
            for path in contract["paths"].values()
            for operation in path.values()
        }
        self.assertIn("createManagerAutomationEnrollmentGrant", operations)
        self.assertIn("exchangeManagerAutomationEnrollmentGrant", operations)
        token = contract["components"]["schemas"]["AutomationTokenResponse"]
        self.assertTrue(token["properties"]["access_token"]["writeOnly"])
        self.assertEqual(
            "pandrator-manager-recovery",
            token["properties"]["audience"]["const"],
        )


class McpTargetPolicyTests(unittest.TestCase):
    @staticmethod
    def policy(*addresses: str) -> NetworkPolicy:
        return NetworkPolicy(lambda _host, _port: tuple(addresses))

    def test_external_https_accepts_only_public_addresses(self):
        profile = TargetProfile(
            name="public",
            mode=TargetMode.EXTERNAL_HTTPS,
            application_origin="https://pandrator.example",
        )
        resolved = TargetRegistry(
            [profile],
            network_policy=self.policy("8.8.8.8"),
        ).resolve("public")
        self.assertEqual("public", resolved.application.zone.value)

        with self.assertRaises(TargetResolutionError):
            TargetRegistry(
                [profile],
                network_policy=self.policy("192.168.1.20"),
            ).resolve("public")
        with self.assertRaises(TargetResolutionError):
            TargetRegistry(
                [profile],
                network_policy=self.policy("8.8.8.8", "192.168.1.20"),
            ).resolve("public")

    def test_private_target_requires_cidr_and_explicit_http_consent(self):
        with self.assertRaises(ValidationError):
            TargetProfile(
                name="lan",
                mode=TargetMode.PRIVATE_NETWORK,
                application_origin="http://pandrator.home",
                allowed_private_cidrs=("192.168.0.0/16",),
            )
        profile = TargetProfile(
            name="lan",
            mode=TargetMode.PRIVATE_NETWORK,
            application_origin="http://pandrator.home",
            allowed_private_cidrs=("192.168.0.0/16",),
            allow_insecure_private_network=True,
        )
        resolved = TargetRegistry(
            [profile],
            network_policy=self.policy("192.168.10.22"),
        ).resolve("lan")
        self.assertEqual("private", resolved.application.zone.value)
        with self.assertRaises(TargetResolutionError):
            TargetRegistry(
                [profile],
                network_policy=self.policy("10.0.0.8"),
            ).resolve("lan")
        with self.assertRaises(ValidationError):
            TargetProfile(
                name="overbroad",
                mode=TargetMode.PRIVATE_NETWORK,
                application_origin="https://pandrator.home",
                allowed_private_cidrs=("0.0.0.0/0",),
            )

    def test_local_target_rejects_proxy_configuration(self):
        with self.assertRaises(ValidationError):
            TargetProfile(
                name="local-proxy",
                mode=TargetMode.LOCAL_MANAGED,
                workspace="C:/Pandrator",
                proxy_origin="http://127.0.0.1:3128",
            )

    def test_local_manager_discovery_must_stay_on_loopback(self):
        profile = TargetProfile(
            name="local",
            mode=TargetMode.LOCAL_MANAGED,
            workspace="C:/Pandrator",
        )
        resolved = TargetRegistry(
            [profile],
            network_policy=self.policy("127.0.0.1"),
            local_discovery=lambda _profile: (
                "http://127.0.0.1:8097",
                "manager-id",
            ),
        ).resolve("local")
        self.assertEqual("loopback", resolved.application.zone.value)
        with self.assertRaises(TargetResolutionError):
            TargetRegistry(
                [profile],
                network_policy=self.policy("192.168.1.20"),
                local_discovery=lambda _profile: (
                    "http://pandrator.home:8097",
                    "manager-id",
                ),
            ).resolve("local")

    def test_metadata_and_plain_http_recovery_are_rejected(self):
        profile = TargetProfile(
            name="external",
            mode=TargetMode.EXTERNAL_HTTPS,
            application_origin="https://pandrator.example",
        )
        with self.assertRaises(TargetResolutionError):
            TargetRegistry(
                [profile],
                network_policy=self.policy("169.254.169.254"),
            ).resolve("external")
        with self.assertRaises(ValidationError):
            TargetProfile(
                name="recovery",
                mode=TargetMode.PRIVATE_NETWORK,
                application_origin="https://pandrator.home",
                manager_recovery_origin="http://manager.home",
                allowed_private_cidrs=("192.168.0.0/16",),
            )

    def test_only_approved_credential_backends_resolve(self):
        resolver = CredentialResolver(
            (
                EnvironmentCredentialBackend(
                    environment={"PANDRATOR_TEST_TOKEN": "sensitive-value"}
                ),
            )
        )
        reference = CredentialReference(
            backend="environment",
            reference="PANDRATOR_TEST_TOKEN",
            audience="application",
        )
        secret = resolver.resolve(reference, audience="application")
        self.assertEqual("sensitive-value", secret.reveal())
        self.assertNotIn("sensitive-value", repr(secret))
        self.assertEqual({"environment", "keyring"}, APPROVED_CREDENTIAL_BACKENDS)

        with self.assertRaises(CredentialResolutionError):
            resolver.resolve(
                CredentialReference(
                    backend="file",
                    reference="C:/secrets/token",
                    audience="application",
                ),
                audience="application",
            )
        with self.assertRaises(CredentialResolutionError):
            resolver.resolve(reference, audience="manager_recovery")


if __name__ == "__main__":
    unittest.main()
