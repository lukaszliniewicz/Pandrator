import json
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from test_mcp_application_client import create_test_ca

from pandrator_mcp.compatibility import (
    REQUIRED_DISPATCH_OPERATION_IDS,
    REQUIRED_MANAGER_OPERATION_IDS,
    REQUIRED_READ_OPERATION_IDS,
)
from pandrator_mcp.credentials import (
    CredentialReference,
    CredentialResolver,
    EnvironmentCredentialBackend,
)
from pandrator_mcp.doctor import diagnose_target
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.targets import (
    TargetIdentityExpectation,
    TargetProfile,
    TargetStore,
)


class DoctorTests(unittest.TestCase):
    def test_layered_private_https_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca_path, certificate_path, key_path = create_test_ca(root)
            origin_holder: dict[str, str] = {}
            token = "doctor-test-secret"

            class Handler(BaseHTTPRequestHandler):
                def _send(self, payload: dict[str, Any], status: int = 200):
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/api/v1/health":
                        self._send(
                            {
                                "status": "ok",
                                "service": "pandrator",
                                "version": "0.8.16",
                                "migration": "ready",
                            }
                        )
                        return
                    if path == "/api/v1/openapi.json":
                        operations = sorted(
                            REQUIRED_READ_OPERATION_IDS
                            | REQUIRED_DISPATCH_OPERATION_IDS
                            | REQUIRED_MANAGER_OPERATION_IDS
                        )
                        self._send(
                            {
                                "openapi": "3.1.0",
                                "info": {"version": "1.0.0"},
                                "paths": {
                                    f"/contract/{index}": {
                                        "get": {"operationId": operation}
                                    }
                                    for index, operation in enumerate(operations)
                                },
                            }
                        )
                        return
                    if self.headers.get("Authorization") != f"Bearer {token}":
                        self._send({"error": "unauthorized"}, status=401)
                        return
                    if path == "/api/v1/system/identity":
                        self._send(
                            {
                                "schema_version": "1",
                                "service": "pandrator",
                                "instance_id": "application-id",
                                "application_version": "0.8.16",
                                "api_version": "v1",
                                "protocol_version": "v1",
                                "canonical_origin": origin_holder["origin"],
                                "managed": True,
                                "manager_instance_id": "manager-id",
                            }
                        )
                    elif path == "/api/v1/capabilities":
                        self._send({"snapshot": "available"})
                    elif path == "/api/v1/manager/status":
                        self._send({"available": True, "state": "ready"})
                    else:
                        self._send({"error": "missing"}, status=404)

                def log_message(self, _format: str, *_args: Any) -> None:
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(certificate_path, key_path)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"https://127.0.0.1:{server.server_address[1]}"
                origin_holder["origin"] = origin
                config = root / "targets.json"
                TargetStore(config).put(
                    TargetProfile(
                        name="private",
                        mode=TargetMode.PRIVATE_NETWORK,
                        application_origin=origin,
                        allowed_private_cidrs=("127.0.0.0/8",),
                        ca_bundle=str(ca_path),
                        application_credential=CredentialReference(
                            backend="environment",
                            reference="PANDRATOR_DOCTOR_TOKEN",
                            audience="application",
                        ),
                        expected_identity=TargetIdentityExpectation(
                            application_instance_id="application-id",
                            canonical_application_origin=origin,
                            manager_instance_id="manager-id",
                        ),
                    )
                )
                report = diagnose_target(
                    McpSettings(
                        target_name="private",
                        configuration_path=config,
                    ),
                    credentials=CredentialResolver(
                        (
                            EnvironmentCredentialBackend(
                                environment={"PANDRATOR_DOCTOR_TOKEN": token}
                            ),
                        )
                    ),
                )
                self.assertTrue(report.healthy)
                self.assertEqual(
                    [
                        "configuration",
                        "dns_route",
                        "tls",
                        "application",
                        "api",
                        "authentication",
                        "identity",
                        "compatibility",
                        "manager",
                        "worker",
                    ],
                    [check.layer for check in report.checks],
                )
                serialized = report.model_dump_json()
                self.assertNotIn(token, serialized)
                self.assertNotIn("PANDRATOR_DOCTOR_TOKEN", serialized)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
